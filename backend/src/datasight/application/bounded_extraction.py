"""Indexed scanning and sparse, grouped extraction with per-invocation budgets."""

import json
import re
import time
from typing import Literal

from pydantic import ConfigDict, Field, TypeAdapter, create_model

from datasight.application.extraction_engine import (
    Block, ExtractionEngine, INSTRUCTIONS,
    consolidate, evidence_for, merge_features, source_blocks, validate_feature_response,
)
from datasight.domain import extraction as c
from datasight.infrastructure.llm.client import ExtractionBudgetExceeded, InvalidCompletion, OutputTruncated, RequestTooLarge

GROUPS = {
    'identity': c.IdentityFeatures, 'use': c.UseFeatures, 'findability': c.FindabilityFeatures,
    'accessibility': c.AccessibilityFeatures, 'interoperability': c.InteroperabilityFeatures,
    'reusability': c.ReusabilityFeatures,
}


class SparseField(c.StrictModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    field: Literal[tuple(f'{group}.{field}' for group, model in
                        {**GROUPS, 'citation': c.CitationFeatures}.items() for field in model.model_fields)] = Field(alias='f')
    state: Literal['reported', 'unclear', 'conflicting'] = Field(alias='s')
    values_json: str = Field(alias='v')
    evidence_ids: list[str] = Field(alias='e')


class SparseFeatures(c.StrictModel):
    observations: list[SparseField]


class CompactUnits(c.StrictModel):
    selections: list[tuple[int, str | None, Literal['r', 's', 'a']]]


class OccurrenceAnswer(c.StrictModel):
    occurrence_id: str
    observations: list[SparseField]


class OccurrenceAnswers(c.StrictModel):
    occurrences: list[OccurrenceAnswer]


def decode(observations, models, evidence):
    values = {name: {} for name in models}
    seen = set()
    for observed in observations:
        group, separator, field = observed.field.partition('.')
        if not separator or group not in models or field not in models[group].model_fields or observed.field in seen:
            raise ValueError('Unknown or duplicate observation field')
        seen.add(observed.field)
        literals = json.loads(observed.values_json)
        if not isinstance(literals, list):
            raise ValueError('Observation values must be a JSON array')
        values[group][field] = {'state': observed.state, 'values': [
            {'value': value, 'evidence_ids': observed.evidence_ids} for value in literals]}
    output = {}
    for group, model in models.items():
        output[group] = model.model_validate(values[group])
        validate_feature_response(output[group], evidence)
    return output


def field_guide(models):
    return {f'{group}.{field}': TypeAdapter(definition.annotation.model_fields['values'].annotation.__args__[0].model_fields['value'].annotation).json_schema()
            for group, model in models.items() for field, definition in model.model_fields.items()}


def requested_features(models):
    fields = tuple(field_guide(models))
    observation = create_model('RequestedField', __base__=SparseField,
        field=(Literal[fields], Field(alias='f')))
    return create_model('RequestedFeatures', __base__=SparseFeatures,
        observations=(list[observation], Field(max_length=len(fields))))


def indexed_blocks(text):
    boundaries = [0]
    for match in re.finditer(r'.+?(?:[.!?](?=\s)|\n|$)', text, re.S):
        boundaries.extend(range(match.start()+1000, match.end(), 1000))
        boundaries.append(match.end())
    index = 0
    while index < len(boundaries)-1:
        end = index+1
        while end+1 < len(boundaries) and boundaries[end+1]-boundaries[index] <= 6000:
            end += 1
        yield Block(boundaries[index], boundaries[end])
        index = end-1 if end-index > 1 and end < len(boundaries)-1 else end


SPARSE = INSTRUCTIONS + """Extract only supported facts for the target dataset.
Do not attach evidence about a different dataset. An association requires explicit
names, identifiers, aliases or clear local context. Every factual string or number
must occur literally in its cited evidence; never infer a license from public access.
Return only fields with reported, unclear, or conflicting information.
Omitted fields mean not_reported. Emit each field ONCE, then stop. Never repeat fields.
Compact keys: f is a supplied group.field path, s is the state, v is a JSON array
of values encoded as a string (not Observed wrappers), e lists evidence IDs supporting
every value. String facts must be JSON strings, year an integer.
Identifier values have scheme, literal, target; URLs have url, target; licenses have
statement, name, url, scope. Use only the supplied allowed literal values.
Identifier target: dataset, data_paper, research_article, repository, other, unknown.
URL target: landing_page, download, documentation, code, publication, unknown.
License scope: dataset, code, article, unknown. License name and url may be null.
Populate every supported requested fact, especially the explicit dataset name and
whether the current publication used or created it. Do not omit supported facts.
"""


class BoundedExtractionEngine(ExtractionEngine):
    def __init__(self, *args):
        super().__init__(*args)
        self.started = time.monotonic()
        self.long = len(self.text) > 60000
        self.attempts = 0
        self.retry_attempts = 0
        self.repair_attempts = 0
        self.rejected_fields = 0
        self.phase = 'locator'
        self.counts = {}
        self.completed = {}
        self.client.before_attempt = self.before_attempt
        self.client.remaining_time = self.remaining

    def progress(self):
        if hasattr(self.repo, 'record_extraction_progress'):
            self.repo.record_extraction_progress(self.extraction_id, {
                'phase': self.phase, 'attempts_this_invocation': self.attempts,
                'calls_by_phase': self.counts, 'completed': self.completed,
                'retry_attempts': self.retry_attempts, 'repair_attempts': self.repair_attempts,
                'rejected_fields': self.rejected_fields,
                'elapsed_seconds': round(time.monotonic() - self.started, 3),
                'call_limit': 100 if self.long else 50, 'time_limit_seconds': 900 if self.long else 300,
            })

    def remaining(self):
        remaining = (900 if self.long else 300) - (time.monotonic() - self.started)
        if remaining <= 0:
            raise ExtractionBudgetExceeded('extraction_budget_exceeded: elapsed time limit; resume explicitly')
        return remaining

    def before_attempt(self):
        remaining = self.remaining()
        if self.attempts >= (100 if self.long else 50):
            raise ExtractionBudgetExceeded('extraction_budget_exceeded: HTTP attempt limit; resume explicitly')
        self.attempts += 1
        self.retry_attempts += bool(getattr(self.client, 'attempt_is_retry', False))
        self.repair_attempts += bool(getattr(self.client, 'request_is_repair', False))
        self.counts[self.phase] = self.counts.get(self.phase, 0) + 1
        self.progress()
        return remaining

    def scan(self, block, depth=0):
        # Keep exact source offsets while making long Markdown paragraphs selectable.
        raw = self.text[block.start:block.end]
        units = []
        for match in re.finditer(r'.+?(?:[.!?](?=\s)|\n|$)', raw, re.S):
            for start in range(match.start(), match.end(), 1000):
                end = min(match.end(), start + 1000)
                if raw[start:end].strip():
                    units.append((start, end))

        def selected(result):
            from datasight.application.extraction_engine import IndexedLine
            output = [IndexedLine(line_index=line, dataset_name=name,
                                  kind={'r': 'reference', 's': 'support', 'a': 'ambiguous'}[kind])
                      for line, name, kind in result.selections]
            for item in output:
                if not 0 <= item.line_index < len(units):
                    raise ValueError('Unknown source unit')
                start, end = units[item.line_index]
                if item.dataset_name is not None and item.dataset_name not in raw[start:end]:
                    # Keep the valid reference, but never publish an unsupported optional name.
                    item.dataset_name = None
            return output

        def validate(result):
            selected(result)

        def section_at(offset):
            headings = list(re.finditer(r'(?m)^#{1,6} (.+)$', self.text[:offset]))
            return headings[-1].group(1) if headings else None

        data = {'block_id': block.block_id, 'lines': [
            {'index': i, 'section': section_at(block.start+s), 'text': raw[s:e]}
            for i, (s, e) in enumerate(units)]}
        instruction = INSTRUCTIONS + '''Locate EVERY dataset reference and supporting dataset
identity, use, access, metadata, licensing, format, provenance or standards statement
in ALL supplied sections: abstract, body, tables, captions, footnotes, appendices,
and references. Select the numbered source units containing this evidence.
Include dataset abbreviations, named cohorts, unnamed collected data and statements
that the current study used, created, compared or analyzed data, even when the
statement also cites an article. Include stand-alone data availability statements.
For example, "We analyzed the Alpha cohort and Beta data" contains two dataset
references; return two selections of that unit with names Alpha and Beta.
Return compact selections [line_index, dataset_name, kind].
kind is r for reference, s for support, a for ambiguous. dataset_name is an exact
contiguous name copied from that unit. For unnamed data or an uncertain name use null.
Never select software or rhetorical data as a dataset. Include availability statements.
Research articles, methods and theories themselves are not datasets; their body
statements about actual datasets still count as references.
In References, select only entries explicitly describing a research dataset; a paper
title, author list, journal name or article DOI alone is insufficient.
Return no lines when there is no dataset evidence. Never output quotations.
'''

        try:
            result = self.request(instruction, data, CompactUnits, validate)
        except InvalidCompletion:
            try:
                result = self.request(instruction + '\nRepair index bounds; use null for uncertain names.', data, CompactUnits, validate)
            except (InvalidCompletion, OutputTruncated, RequestTooLarge):
                return self.split_scan(block, depth)
        except (OutputTruncated, RequestTooLarge):
            return self.split_scan(block, depth)
        output = []
        for item in selected(result):
            start, end = units[item.line_index]
            name = item.dataset_name
            child = Block(block.start + start, block.start + end)
            passage = c.LocatedPassage(block_id=child.block_id, quote=self.text[child.start:child.end],
                                      quote_occurrence=0, dataset_name=name, kind=item.kind)
            output.append((passage, evidence_for(self.text, child, passage)))
        self.scanned.append(block)
        self.completed['locator_blocks'] = len(self.scanned)
        self.progress()
        return output

    def split_scan(self, block, depth):
        if block.end - block.start < 256 or depth >= 12:
            raise ValueError('Indexed locator cannot validate source coverage')
        return [item for child in source_blocks(self.text, block.start, block.end, (block.end-block.start)//2)
                for item in self.scan(child, depth + 1)]

    def sparse(self, target, evidence, models, repair=False):
        data = {'target_dataset': target, 'fields': field_guide(models),
                'evidence': [{'evidence_id': item.evidence_id, 'quote': item.quote, 'placement': item.placement,
                              'section': item.section_heading} for item in evidence]}
        # A field repair must be unable to regenerate the other, already validated fields.
        response = requested_features(models)
        def validate(result):
            allowed = set(field_guide(models))
            paths = [item.field for item in result.observations]
            if len(paths) != len(set(paths)) or not set(paths) <= allowed:
                raise ValueError('Unknown or duplicate field paths')
        try:
            instruction = SPARSE + ('\nRepair invalid fields: use exact supported values and valid field types only.' if repair else '')
            result = self.request(instruction, data, response, validate)
            valid, invalid = [], []
            for observed in result.observations:
                try:
                    decode([observed], models, evidence)
                    valid.append(observed)
                except ValueError:
                    invalid.append(observed.field)
            if invalid and repair:
                raise InvalidCompletion('Field repair still failed evidence or type validation')
            output = decode(valid, models, evidence)
            if invalid:
                self.rejected_fields += len(invalid)
                partial = {group: create_model(model.__name__ + '_repair', __base__=c.StrictModel,
                           **{name: (definition.annotation, Field(default_factory=definition.annotation))
                              for name, definition in model.model_fields.items() if f'{group}.{name}' in invalid})
                           for group, model in models.items() if any(path.startswith(group+'.') for path in invalid)}
                fixed = self.sparse(target, evidence, partial, repair=True)
                for group, fields in fixed.items():
                    output[group] = models[group].model_validate({**output[group].model_dump(), **fields.model_dump()})
            return output
        except (InvalidCompletion, OutputTruncated, RequestTooLarge) as exc:
            if isinstance(exc, InvalidCompletion) and not repair:
                return self.sparse(target, evidence, models, repair=True)
            if len(evidence) > 1:
                middle = len(evidence)//2
                left = self.sparse(target, evidence[:middle], models, repair=repair)
                right = self.sparse(target, evidence[middle:], models, repair=repair)
                return {key: merge_features(left[key], right[key]) for key in models}
            if sum(len(model.model_fields) for model in models.values()) > 1:
                fields = [(group, name) for group, model in models.items() for name in model.model_fields]
                outputs = []
                for subset in (fields[:len(fields)//2], fields[len(fields)//2:]):
                    partial = {group: create_model(model.__name__ + '_subset', __base__=c.StrictModel,
                               **{name: (model.model_fields[name].annotation, Field(default_factory=model.model_fields[name].annotation)) for g, name in subset if g == group})
                               for group, model in models.items() if any(g == group for g, _ in subset)}
                    outputs.append(self.sparse(target, evidence, partial, repair=repair))
                return {group: model.model_validate({key: value for output in outputs if group in output
                                                     for key, value in output[group].model_dump().items()}) for group, model in models.items()}
            raise ValueError('Sparse feature cannot validate; extraction incomplete')

    def occurrence_batch(self, entries):
        models = {'use': c.UseFeatures, 'citation': c.CitationFeatures}
        observation = requested_features(models).model_fields['observations'].annotation
        answer = create_model('RequestedOccurrence', __base__=OccurrenceAnswer,
            occurrence_id=(Literal[tuple(oid for oid, _, _ in entries)], ...),
            observations=(observation, Field(max_length=len(field_guide(models)))))
        response = create_model('RequestedOccurrences', __base__=OccurrenceAnswers,
            occurrences=(list[answer], Field(min_length=len(entries), max_length=len(entries))))
        data = {'fields': field_guide(models), 'occurrences': [
            {'occurrence_id': oid, 'target_dataset': target,
             'evidence': [{'evidence_id': item.evidence_id, 'quote': item.quote, 'placement': item.placement,
                           'section': item.section_heading} for item in evidence]}
            for oid, target, evidence in entries]}
        def validate(result):
            ids = [item.occurrence_id for item in result.occurrences]
            if len(ids) != len(set(ids)) or set(ids) != {entry[0] for entry in entries}:
                raise ValueError('Missing, duplicate, or unknown occurrence ID')
            sources = {oid: evidence for oid, _, evidence in entries}
            for item in result.occurrences:
                decode(item.observations, models, sources[item.occurrence_id])
        try:
            try:
                result = self.request(SPARSE + '\nReturn every supplied occurrence ID exactly once.', data, response, validate)
            except InvalidCompletion:
                result = self.request(SPARSE + '\nRepair the batch: return every occurrence ID exactly once, with supported typed values.', data, response, validate)
            sources = {oid: evidence for oid, _, evidence in entries}
            return {item.occurrence_id: decode(item.observations, models, sources[item.occurrence_id]) for item in result.occurrences}
        except (InvalidCompletion, OutputTruncated, RequestTooLarge):
            if len(entries) > 1:
                middle = len(entries)//2
                return {**self.occurrence_batch(entries[:middle]), **self.occurrence_batch(entries[middle:])}
            oid, target, evidence = entries[0]
            return {oid: self.sparse(target, evidence, models)}

    def run(self, paper_id, source_sha256):
        try:
            passages = [item for block in indexed_blocks(self.text) for item in self.scan(block)]
            anchors = {(e.char_start, e.char_end, p.dataset_name, p.kind): (p, e) for p, e in passages}
            for _, evidence in anchors.values():
                self.evidence[evidence.evidence_id] = evidence
            refs = [(p, e) for p, e in anchors.values() if p.kind == 'reference']
            self.long = self.long or len(refs) > 30
            groups = {}
            for passage, evidence in refs:
                # Exact names only; unnamed references remain independent.
                identifiers = tuple(sorted(set(re.findall(r'\b10\.\d{4,9}/[^\s<>]+', evidence.quote))))
                key = ('named', passage.dataset_name, identifiers) if passage.dataset_name else ('unnamed', evidence.evidence_id)
                groups.setdefault(key, []).append((passage, evidence))
            bibliography = {}
            for match in re.finditer(r'(?m)^- \*\*\[[^\n]+$', self.text):
                block = Block(match.start(), match.end())
                passage = c.LocatedPassage(block_id=block.block_id, quote=match.group(), quote_occurrence=0, dataset_name=None, kind='support')
                for identifier in re.findall(r'\{#([^}]+)\}', match.group()):
                    bibliography[identifier] = evidence_for(self.text, block, passage)
            entries, work, occurrences = [], [], {}
            self.phase = 'shared_features'
            for index, members in enumerate(groups.values(), 1):
                target = members[0][0].dataset_name or members[0][1].quote
                evidence = [e for _, e in members]
                evidence += [e for p, e in anchors.values() if p.kind == 'support' and p.dataset_name is not None and p.dataset_name == members[0][0].dataset_name]
                evidence += [bibliography[ref] for _, e in members for ref in e.bibliography_reference_ids if ref in bibliography]
                context = sorted({e.evidence_id: e for e in evidence}.values(), key=lambda e: (e.char_start, e.char_end))
                self.evidence.update({e.evidence_id: e for e in context})
                values = self.sparse(target, context, GROUPS)
                if members[0][0].dataset_name and values['identity'].name.state == 'not_reported':
                    values['identity'].name = c.Observed[str](state='reported', values=[
                        c.EvidenceValue[str](value=target, evidence_ids=[members[0][1].evidence_id])])
                entry = c.DatasetEntry(dataset_local_id=f'd{index}', display_name=target, **values)
                for number, (_, evidence) in enumerate(members, 1):
                    oid = f'd{index}-o{number}'
                    linked = [bibliography[ref] for ref in evidence.bibliography_reference_ids if ref in bibliography]
                    occurrence = c.Occurrence(occurrence_id=oid, evidence_ids=[evidence.evidence_id], bibliography_only=evidence.placement == 'bibliography')
                    for marker, ref in re.findall(r'\[([^\]]+)\]\{#([^}]+)\}', evidence.quote):
                        source = bibliography.get(ref)
                        occurrence.citation_links.append(c.CitationLink(marker=marker, reference_id=ref, raw_reference=source.quote if source else None, status='linked' if source else 'unresolved'))
                    for marker in re.findall(r'\[(\d+(?:[,;– -]\d+)*)\](?!\{#)', evidence.quote):
                        occurrence.citation_links.append(c.CitationLink(marker=marker, reference_id=None, raw_reference=None, status='unresolved'))
                    entry.occurrences.append(occurrence)
                    occurrences[oid] = occurrence
                    work.append((oid, target, [evidence, *linked]))
                if members[0][0].dataset_name is None:
                    entry.review_reasons.append('Unnamed dataset; grouping requires explicit identity evidence')
                for group in GROUPS:
                    for field, observed in getattr(entry, group).model_dump().items():
                        if observed['state'] in {'unclear', 'conflicting'}:
                            entry.review_reasons.append(f"{group}.{field}: {observed['state']}")
                entries.append(entry)
                self.completed['dataset_groups'] = index
                self.progress()
            self.phase = 'occurrences'
            for start in range(0, len(work), 8):
                for oid, values in self.occurrence_batch(work[start:start+8]).items():
                    occurrences[oid].use = values['use']
                    occurrences[oid].citation = values['citation']
                self.completed['occurrences'] = min(len(work), start+8)
                self.progress()
            for entry in entries:
                for occurrence in entry.occurrences:
                    entry.use = merge_features(entry.use, occurrence.use)
            covered = 0
            for block in sorted(self.scanned, key=lambda item: item.start):
                if block.start > covered:
                    raise ValueError('Source coverage has a gap')
                covered = max(covered, block.end)
            if covered != len(self.text):
                raise ValueError('Source coverage incomplete')
            self.remaining()
            self.phase = 'publication'
            self.progress()
            return c.PublicationExtraction(paper_id=paper_id, source_sha256=source_sha256,
                evidence=sorted(self.evidence.values(), key=lambda e: (e.char_start, e.char_end)), datasets=consolidate(entries),
                unresolved_mentions=sorted({e.evidence_id for p, e in anchors.values() if p.kind in {'support', 'ambiguous'}}),
                coverage={'source_characters': len(self.text), 'covered_characters': covered, 'validated_blocks': len(self.scanned)})
        finally:
            try:
                self.progress()
            finally:
                self.client.before_attempt = None
                self.client.remaining_time = None
