"""Indexed scanning and sparse, grouped extraction with per-invocation budgets."""

import json
import re
import time
from bisect import bisect_left, bisect_right
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from contextlib import ExitStack
from functools import cached_property
from threading import Event
from typing import Any, Literal, cast

from pydantic import ConfigDict, Field, TypeAdapter, create_model

from datasight import config
from datasight.application.extraction_engine import (
    Block, ExtractionEngine, INSTRUCTIONS,
    consolidate, evidence_for, locator_blocks, merge_features, source_blocks, specific_name, validate_feature_response,
)
from datasight.domain import extraction as c
from datasight.domain.candidate_detection import TRIGGER_PATTERNS
from datasight.infrastructure.llm.client import MAX_TOKENS, ExtractionBudgetExceeded, InvalidCompletion, OutputTruncated, RequestTooLarge
from datasight.infrastructure.llm.tokenizer import count_tokens

PROMPT_VERSION = 'indexed-sparse-v16'
ACCESSION = next(pattern for kind, pattern, _ in TRIGGER_PATTERNS if kind == 'accession')

GROUPS = {'identity': c.IdentityFeatures, 'use': c.UseFeatures}
SUPPORTED_FIELDS = tuple(f'{group}.{field}' for group, model in
                         {**GROUPS, 'citation': c.CitationFeatures}.items() for field in model.model_fields)


class SparseField(c.StrictModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    # Pydantic expands this runtime tuple into a closed schema; Pyright cannot evaluate it.
    field: Literal[SUPPORTED_FIELDS] = Field(alias='f')  # pyright: ignore[reportInvalidTypeForm]
    state: Literal['reported', 'unclear', 'conflicting'] = Field(alias='s')
    values_json: str = Field(alias='v')
    evidence_ids: list[str] = Field(alias='e')


class SparseFeatures(c.StrictModel):
    observations: list[SparseField]


class CompactUnits(c.StrictModel):
    selections: list[tuple[int, str | None, Literal['r', 's', 'a']]]


class CompactFields(c.StrictModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)


class OccurrenceAnswer(c.StrictModel):
    occurrence_id: str
    fields: CompactFields


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


def requested_features(models, evidence):
    fields = tuple(field_guide(models))
    observation = create_model('RequestedField', __base__=SparseField,
        field=(Literal[fields], Field(alias='f')),
        evidence_ids=(list[Literal[tuple(item.evidence_id for item in evidence)]], Field(alias='e')))
    return cast(type[SparseFeatures], create_model('RequestedFeatures', __base__=SparseFeatures,
        observations=(list[observation], Field(max_length=len(fields)))))


def subset_models(models: dict[str, type[c.StrictModel]], paths: set[str], suffix: str) -> dict[str, type[c.StrictModel]]:
    # Preserve model names because they are part of persisted request fingerprints.
    selected = {}
    for group, model in models.items():
        definitions: dict[str, Any] = {}
        for name, field in model.model_fields.items():
            if f'{group}.{name}' not in paths:
                continue
            annotation = field.annotation
            if annotation is None:
                raise TypeError(f'{group}.{name} has no field type')
            definitions[name] = (annotation, Field(default_factory=annotation))
        if definitions:
            selected[group] = create_model(model.__name__ + suffix, __base__=c.StrictModel, **definitions)
    return selected


def compact_occurrence_fields(models, evidence_count):
    fields, guide = {}, {}
    for group, model in models.items():
        for name, definition in model.model_fields.items():
            value = definition.annotation.model_fields['values'].annotation.__args__[0].model_fields['value'].annotation
            index = str(len(fields))
            path = f'{group}.{name}'
            fields[path] = (tuple[Literal['r', 'u', 'c'], list[value], list[Literal[tuple(range(evidence_count))]]] | None,
                            Field(default=None, alias=index))
            guide[index] = path
    return create_model('CompactOccurrenceFields', __base__=CompactFields, **fields), guide


def compact_observations(fields, evidence):
    return [SparseField(f=path, s=cast(Literal['reported', 'unclear', 'conflicting'],
                                              {'r': 'reported', 'u': 'unclear', 'c': 'conflicting'}[value[0]]),
                               v=json.dumps(value[1]), e=[evidence[i].evidence_id if i < len(evidence) else f'invalid:{i}' for i in value[2]])
                    for path, value in fields.model_dump(mode='json').items() if value is not None]


def decode_compact(fields, models, evidence):
    return decode(compact_observations(fields, evidence), models, evidence)


indexed_blocks = locator_blocks


def occurrence_batches(work):
    batch, cost = [], 0
    for entry in work:
        # shortcut: estimate typical output; subdivide if the actual completion reaches the cap.
        estimate = 200 + sum(count_tokens(item.quote) for item in entry[2])
        if batch and (len(batch) == 8 or cost + estimate > MAX_TOKENS - 128):
            yield batch
            batch, cost = [], 0
        batch.append(entry)
        cost += estimate
    if batch:
        yield batch


SPARSE = INSTRUCTIONS + """Extract only supported facts for the target dataset.
Do not attach evidence about a different dataset. An association requires explicit
names, identifiers, aliases or clear local context. Every factual string or number
must occur literally in its cited evidence.
Return only fields with reported, unclear, or conflicting information.
Omitted fields mean not_reported. Emit each field ONCE, then stop. Never repeat fields.
Compact keys: f is a supplied group.field path, s is the state, v is a JSON array
of values encoded as a string (not Observed wrappers), e lists evidence IDs supporting
every value. String facts must be JSON strings, year an integer.
Identifier values have scheme, literal, target; URLs have url, target.
Use only the supplied allowed literal values.
Identifier target: dataset, data_paper, research_article, repository, other, unknown.
URL target: landing_page, download, documentation, code, publication, unknown.
Populate every supported requested fact, especially the explicit dataset name and
whether the current publication used or created it. Do not omit supported facts.
When citation fields are requested, classify the local mention and citation for
this occurrence. Separate use by this publication from claims about prior work.
Emit minified JSON.
"""

COMPACT_OCCURRENCES = INSTRUCTIONS + """Extract only the requested use/citation facts for each occurrence.
The fields guide maps numeric JSON keys to field names. Return every numeric key:
null if not reported, otherwise [state, [typed values], [local evidence indices]].
State r=reported, u=unclear, c=conflicting. Use only the schema's allowed values.
Every factual string/number must occur literally in its cited quote. Do not paraphrase.
Classify use by the current publication separately from claims about prior work.
Do not report identity fields. Return each supplied occurrence ID exactly once.
Emit MINIFIED JSON on ONE line: no indentation, newlines or spaces outside strings.
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
        self.partial_responses = 0
        self.phase = 'locator'
        self.counts = {}
        self.completed = {}
        self.cancelled = Event()
        self.client.before_attempt = self.before_attempt
        self.client.remaining_time = self.remaining

    def progress(self):
        if hasattr(self.repo, 'record_extraction_progress'):
            with self.repo_lock:
                self.repo.record_extraction_progress(self.extraction_id, {
                    'phase': self.phase, 'attempts_this_invocation': self.attempts,
                    'calls_by_phase': dict(self.counts), 'completed': dict(self.completed),
                    'retry_attempts': self.retry_attempts, 'repair_attempts': self.repair_attempts,
                    'rejected_fields': self.rejected_fields, 'partial_responses': self.partial_responses,
                    'elapsed_seconds': round(time.monotonic() - self.started, 3),
                    'call_limit': 100 if self.long else 50, 'time_limit_seconds': 900 if self.long else 300,
                })

    def remaining(self):
        if self.cancelled.is_set():
            raise ExtractionBudgetExceeded('Extraction stopped after another request failed')
        remaining = (900 if self.long else 300) - (time.monotonic() - self.started)
        if remaining <= 0:
            raise ExtractionBudgetExceeded('extraction_budget_exceeded: elapsed time limit; resume explicitly')
        return remaining

    def before_attempt(self):
        with self.repo_lock:
            remaining = self.remaining()
            if self.attempts >= (100 if self.long else 50):
                raise ExtractionBudgetExceeded('extraction_budget_exceeded: HTTP attempt limit; resume explicitly')
            self.attempts += 1
            self.retry_attempts += bool(getattr(self.client, 'attempt_is_retry', False))
            self.repair_attempts += bool(getattr(self.client, 'request_is_repair', False))
            self.counts[self.phase] = self.counts.get(self.phase, 0) + 1
            self.progress()
            return remaining

    @cached_property
    def source_units(self):
        return [(start, min(start + 1000, match.end()))
                for match in re.finditer(r'.+?(?:[.!?](?=\s)|\n|$)', self.text, re.S)
                for start in range(match.start(), match.end(), 1000)]

    def scan(self, block, depth=0):
        # Keep exact source offsets while making long Markdown paragraphs selectable.
        first = bisect_right(self.source_units, block.start, key=lambda unit: unit[1])
        last = bisect_left(self.source_units, block.end, key=lambda unit: unit[0])
        units = []
        for start, end in self.source_units[first:last]:
            # Whole units get identical anchors in adjacent blocks. Deep splits can still clip a long unit.
            if end - start > block.end - block.start:
                start, end = max(start, block.start), min(end, block.end)
            if self.text[start:end].strip():
                units.append((start, end))
        # shortcut: budget for typical indexed selections; dense results can still be subdivided.
        if len(units) * 14 > MAX_TOKENS - 128:
            return self.split_scan(block, depth)

        def selected(result):
            from datasight.application.extraction_engine import IndexedLine
            output = [IndexedLine(line_index=line, dataset_name=name,
                                  kind=cast(Literal['reference', 'support', 'ambiguous'],
                                            {'r': 'reference', 's': 'support', 'a': 'ambiguous'}[kind]))
                      for line, name, kind in result.selections]
            valid = [item for item in output if 0 <= item.line_index < len(units)]
            for item in valid:
                start, end = units[item.line_index]
                if item.dataset_name is not None and item.dataset_name not in self.text[start:end]:
                    # Keep the valid reference, but never publish an unsupported optional name.
                    item.dataset_name = None
            return valid, len(valid) != len(output)

        def section_at(offset):
            headings = list(re.finditer(r'(?m)^#{1,6} (.+)$', self.text[:offset]))
            return headings[-1].group(1) if headings else None

        data = {'block_id': block.block_id, 'lines': [
            {'index': i, 'section': section_at(s), 'text': self.text[s:e]}
            for i, (s, e) in enumerate(units)]}
        instruction = INSTRUCTIONS + '''Locate EVERY dataset reference and supporting dataset
identity or use statement
in ALL supplied sections: abstract, body, tables, captions, footnotes, appendices,
and references. Select the numbered source units containing this evidence.
Include dataset abbreviations, named cohorts, unnamed collected data and statements
that the current study used, created, compared or analyzed data, even when the
 statement also cites an article.
For example, "We analyzed the Alpha cohort and Beta data" contains two dataset
references; return two selections of that unit with names Alpha and Beta.
Return compact selections [line_index, dataset_name, kind].
kind is r for reference, s for support, a for ambiguous. dataset_name is an exact
contiguous name copied from that unit. For unnamed data or an uncertain name use null.
Never select software or rhetorical data as a dataset.
Research articles, methods and theories themselves are not datasets; their body
statements about actual datasets still count as references.
In References, select only entries explicitly describing a research dataset; a paper
title, author list, journal name or article DOI alone is insufficient.
Return no lines when there is no dataset evidence. Never output quotations.
'''

        selections, split = [], False
        if units:
            for prompt in (instruction, instruction + '\nRepair index bounds; use null for uncertain names.'):
                try:
                    result = self.request(prompt, data, CompactUnits, lambda _: None)
                except (OutputTruncated, RequestTooLarge):
                    split = True
                    break
                except InvalidCompletion:
                    split = True
                    continue
                valid, split = selected(result)
                selections.extend(valid)
                if not split:
                    break
                with self.repo_lock:
                    self.partial_responses += 1
        output = []
        for item in selections:
            start, end = units[item.line_index]
            name = item.dataset_name
            child = Block(start, end)
            passage = c.LocatedPassage(block_id=child.block_id, quote=self.text[child.start:child.end],
                                      quote_occurrence=0, dataset_name=name, kind=item.kind)
            output.append((passage, evidence_for(self.text, child, passage)))
        if split:
            return output + self.split_scan(block, depth)
        with self.repo_lock:
            self.scanned.append(block)
            self.completed['locator_blocks'] = len(self.scanned)
            self.progress()
        return output

    def split_scan(self, block, depth):
        if block.end - block.start < 256 or depth >= 12:
            raise ValueError('Indexed locator cannot validate source coverage')
        return [item for child in source_blocks(self.text, block.start, block.end, (block.end-block.start)//2)
                for item in self.scan(child, depth + 1)]

    def partial_fields(self, observations, models, evidence):
        output = {group: model() for group, model in models.items()}
        invalid = set()
        evidence_ids = {item.evidence_id for item in evidence}
        for observed in observations:
            try:
                decoded = decode([observed], models, evidence)
            except ValueError:
                invalid.add(observed.field)
                # Ground each value separately so one bad value does not erase good ones.
                try:
                    literals = json.loads(observed.values_json)
                except ValueError:
                    continue
                if not isinstance(literals, list):
                    continue
                for literal in literals:
                    try:
                        decoded = decode([observed.model_copy(update={
                            'state': 'unclear' if observed.state != 'reported' else 'reported',
                            'values_json': json.dumps([literal]),
                            'evidence_ids': [eid for eid in observed.evidence_ids if eid in evidence_ids],
                        })], models, evidence)
                    except ValueError:
                        continue
                    output = {group: merge_features(output[group], value) for group, value in decoded.items()}
            else:
                output = {group: merge_features(output[group], value) for group, value in decoded.items()}
        if invalid:
            with self.repo_lock:
                self.rejected_fields += len(invalid)
                self.partial_responses += 1
        return output, invalid

    def repair_fields(self, target, evidence, models, output, invalid, repair=False):
        if invalid:
            partial = subset_models(models, invalid, '_repair')
            fixed = (self.split_sparse(target, evidence, partial, repair=True) if repair else
                     self.sparse(target, evidence, partial, repair=True))
            for group, fields in fixed.items():
                previous = fields.__class__.model_validate({
                    key: getattr(output[group], key) for key in fields.__class__.model_fields})
                output[group] = models[group].model_validate({
                    **output[group].model_dump(), **merge_features(previous, fields).model_dump()})
        return output

    def sparse(self, target, evidence, models, repair=False):
        if len(evidence) > 1 and sum(count_tokens(item.quote) for item in evidence) > MAX_TOKENS:
            return self.split_sparse(target, evidence, models, repair)
        data = {'target_dataset': target, 'fields': field_guide(models),
                'evidence': [{'evidence_id': item.evidence_id, 'quote': item.quote, 'placement': item.placement,
                              'section': item.section_heading} for item in evidence]}
        # A field repair must be unable to regenerate the other, already validated fields.
        response = requested_features(models, evidence)
        def validate(result: SparseFeatures):
            allowed = set(field_guide(models))
            paths = [item.field for item in result.observations]
            if not set(paths) <= allowed:
                raise ValueError('Unknown field paths')
        try:
            instruction = SPARSE + ('\nRepair invalid fields: use exact supported values and valid field types only.' if repair else '')
            result = self.request(instruction, data, response, validate)
        except (InvalidCompletion, OutputTruncated, RequestTooLarge) as exc:
            if isinstance(exc, InvalidCompletion) and not repair:
                return self.sparse(target, evidence, models, repair=True)
            return self.split_sparse(target, evidence, models, repair)
        output, invalid = self.partial_fields(result.observations, models, evidence)
        return self.repair_fields(target, evidence, models, output, invalid, repair)

    def split_sparse(self, target, evidence, models, repair):
        if len(evidence) > 1:
            middle = len(evidence)//2
            left = self.sparse(target, evidence[:middle], models, repair=repair)
            right = self.sparse(target, evidence[middle:], models, repair=repair)
            return {key: merge_features(left[key], right[key]) for key in models}
        fields = list(field_guide(models))
        if len(fields) > 1:
            middle = len(fields)//2
            outputs = [self.sparse(target, evidence, subset_models(models, set(subset), '_subset'), repair=repair)
                       for subset in (fields[:middle], fields[middle:])]
            return {group: model.model_validate({key: value for output in outputs if group in output
                                                 for key, value in output[group].model_dump().items()}) for group, model in models.items()}
        if len(evidence) == 1 and len(evidence[0].quote) > 256:
            original = evidence[0]
            result = {group: model() for group, model in models.items()}
            for block in source_blocks(self.text, original.char_start, original.char_end, len(original.quote)//2):
                passage = c.LocatedPassage(block_id=block.block_id, quote=self.text[block.start:block.end],
                                           quote_occurrence=0, dataset_name=None, kind='support')
                child = evidence_for(self.text, block, passage)
                with self.repo_lock:
                    self.evidence[child.evidence_id] = child
                values = self.sparse(target, [child], models, repair=repair)
                result = {key: merge_features(result[key], values[key]) for key in models}
            return result
        raise ValueError('Sparse feature cannot validate; extraction incomplete')

    def occurrence_batch(self, entries):
        models = {'use': c.UseFeatures, 'citation': c.CitationFeatures}
        fields, guide = compact_occurrence_fields(models, max(len(evidence) for _, _, evidence in entries))
        answer = create_model('RequestedOccurrence', __base__=OccurrenceAnswer,
            occurrence_id=(Literal[tuple(oid for oid, _, _ in entries)], ...),
            fields=(fields, ...))
        response = cast(type[OccurrenceAnswers], create_model('RequestedOccurrences', __base__=OccurrenceAnswers,
            occurrences=(list[answer], Field(min_length=len(entries), max_length=len(entries)))))
        data = {'fields': guide, 'occurrences': [
            {'occurrence_id': oid, 'target_dataset': target,
             'evidence': [{'index': i, 'quote': item.quote, 'placement': item.placement,
                           'section': item.section_heading} for i, item in enumerate(evidence)]}
            for oid, target, evidence in entries]}
        def validate(result: OccurrenceAnswers):
            if not result.occurrences or not {item.occurrence_id for item in result.occurrences} <= {entry[0] for entry in entries}:
                raise ValueError('Missing or unknown occurrence ID')
        try:
            try:
                result = self.request(COMPACT_OCCURRENCES, data, response, validate)
            except InvalidCompletion:
                result = self.request(COMPACT_OCCURRENCES + '\nRepair the batch: use only supported typed values and local evidence indices.', data, response, validate)
        except (InvalidCompletion, OutputTruncated, RequestTooLarge):
            if len(entries) > 1:
                middle = len(entries)//2
                return {**self.occurrence_batch(entries[:middle]), **self.occurrence_batch(entries[middle:])}
            oid, target, evidence = entries[0]
            return {oid: self.sparse(target, evidence, models)}
        answers = {}
        for item in result.occurrences:
            answers.setdefault(item.occurrence_id, []).append(item.fields)
        output = {}
        missing = []
        for oid, target, evidence in entries:
            if oid not in answers:
                missing.append((oid, target, evidence))
                continue
            observations = [observed for fields in answers[oid] for observed in compact_observations(fields, evidence)]
            values, invalid = self.partial_fields(observations, models, evidence)
            output[oid] = self.repair_fields(target, evidence, models, values, invalid)
        if missing:
            with self.repo_lock:
                self.partial_responses += 1
            output.update(self.occurrence_batch(missing))
        return output

    def parallel(self, pool, function, items):
        futures = []
        failures = []

        def dispatch(item):
            self.remaining()
            try:
                return function(item)
            except BaseException as exc:
                with self.repo_lock:
                    if not failures:
                        failures.append(exc)
                        self.cancelled.set()
                raise

        try:
            for item in items:
                futures.append(pool.submit(dispatch, item))
            wait(futures, return_when=FIRST_EXCEPTION)
            if failures:
                raise failures[0]
            return [future.result() for future in futures]
        except BaseException:
            self.cancelled.set()
            for future in futures:
                future.cancel()
            raise

    def run(self, paper_id, source_sha256):
        self.client.cancel_inference = self.cancelled
        try:
            with ExitStack() as sessions:
                def initialize():
                    if worker_session := getattr(self.client, 'worker_session', None):
                        session = worker_session()
                        with self.repo_lock:
                            sessions.callback(session.close)
                with ThreadPoolExecutor(max_workers=config.LLM_INFERENCE_CONCURRENCY, initializer=initialize) as pool:
                    return self.run_stages(pool, paper_id, source_sha256)
        finally:
            try:
                self.progress()
            finally:
                self.client.before_attempt = None
                self.client.remaining_time = None
                self.client.cancel_inference = None

    def run_stages(self, pool, paper_id, source_sha256):
        _ = self.source_units  # Build once before dispatching overlapping blocks.
        scanned = self.parallel(pool, self.scan, indexed_blocks(self.text))
        anchors = {(e.char_start, e.char_end, p.dataset_name, p.kind): (p, e)
                   for block in scanned for p, e in block}
        passages = sorted(anchors.values(), key=lambda item: (item[1].char_start, item[1].char_end))
        self.evidence.update({e.evidence_id: e for _, e in passages})
        refs = [(p, e) for p, e in passages if p.kind == 'reference']
        self.long = self.long or len(refs) > 30

        def signature(e):
            # Raw DOI mentions can signal ambiguity but cannot establish a dataset identity.
            ids = re.findall(r'\b10\.\d{4,9}/[^\s<>\]{}]+|' + ACCESSION, e.quote, re.I)
            return tuple(sorted({item.casefold().rstrip('.,;)') for item in ids}))

        signatures: dict[str, set[tuple[str, ...]]] = {}
        for passage, evidence in refs:
            if (name := specific_name(passage.dataset_name or '')) and (ids := signature(evidence)):
                signatures.setdefault(name, set()).add(ids)
        ambiguous = {name for name, identifiers in signatures.items() if len(identifiers) > 1}
        groups = {}
        for passage, evidence in refs:
            name = specific_name(passage.dataset_name or '')
            ids = signature(evidence)
            if name and not ids and len(signatures.get(name, ())) == 1:
                ids = next(iter(signatures[name]))
            key = (name, ids) if name else (passage.dataset_name, evidence.evidence_id)
            groups.setdefault(key, []).append((passage, evidence))

        bibliography = {}
        for match in re.finditer(r"(?m)^- \*\*\[[^\n]+$", self.text):
            block = Block(match.start(), match.end())
            passage = c.LocatedPassage(block_id=block.block_id, quote=match.group(), quote_occurrence=0, dataset_name=None, kind='support')
            for identifier in re.findall(r'\{#([^}]+)\}', match.group()):
                bibliography[identifier] = evidence_for(self.text, block, passage)

        entries, work, occurrences, jobs = [], [], {}, []
        for index, members in enumerate(groups.values(), 1):
            first, first_evidence = members[0]
            target = first.dataset_name or first_evidence.quote
            name = specific_name(first.dataset_name or '')
            entry = c.DatasetEntry(dataset_local_id=f'd{index}', display_name=target)
            local = {}
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
                local[oid] = list({item.evidence_id: item for item in [evidence, *linked]}.values())
            context = {e.evidence_id: e for sources in local.values() for e in sources}
            local_ids = set(context)
            if name and name not in ambiguous:
                context.update({e.evidence_id: e for p, e in passages
                                if p.kind == 'support' and specific_name(p.dataset_name or '') == name})
            self.evidence.update(context)
            # A single occurrence with identical evidence can use the same grounded answer.
            fused = len(local) == 1 and local_ids == set(context)
            models = {**GROUPS, 'citation': c.CitationFeatures} if fused else GROUPS
            jobs.append((target, sorted(context.values(), key=lambda e: (e.char_start, e.char_end)), models))
            if not fused:
                work.extend((oid, target, sources) for oid, sources in local.items())
            if first.dataset_name is None:
                entry.review_reasons.append('Unnamed dataset; grouping requires explicit identity evidence')
            if name in ambiguous:
                entry.review_reasons.append('Same name has different identifier mentions; identity unresolved')
            entries.append(entry)

        self.phase = 'shared_features'
        answers = self.parallel(pool, lambda job: self.sparse(*job), jobs)
        for entry, members, values in zip(entries, groups.values(), answers):
            first, evidence = members[0]
            entry.identity, entry.use = values['identity'], values['use']
            if first.dataset_name and entry.identity.name.state == 'not_reported':
                entry.identity.name = c.Observed[str](state='reported', values=[
                    c.EvidenceValue[str](value=first.dataset_name, evidence_ids=[evidence.evidence_id])])
            if 'citation' in values:
                entry.occurrences[0].use, entry.occurrences[0].citation = values['use'], values['citation']
        self.completed['dataset_groups'] = len(entries)

        self.phase = 'occurrences'
        for batch in self.parallel(pool, self.occurrence_batch, occurrence_batches(work)):
            for oid, values in batch.items():
                occurrences[oid].use, occurrences[oid].citation = values['use'], values['citation']
        self.completed['occurrences'] = len(occurrences)
        for entry in entries:
            for occurrence in entry.occurrences:
                entry.use = merge_features(entry.use, occurrence.use)
            for group in GROUPS:
                for field, observed in getattr(entry, group).model_dump().items():
                    if observed['state'] in {'unclear', 'conflicting'}:
                        entry.review_reasons.append(f"{group}.{field}: {observed['state']}")
        covered = 0
        for block in sorted(self.scanned, key=lambda item: item.start):
            if block.start > covered:
                raise ValueError('Source coverage has a gap')
            covered = max(covered, block.end)
        if covered != len(self.text):
            raise ValueError('Source coverage incomplete')
        self.remaining()
        self.phase = 'publication'
        return c.PublicationExtraction(paper_id=paper_id, source_sha256=source_sha256, prompt_version=PROMPT_VERSION,
            evidence=sorted(self.evidence.values(), key=lambda e: (e.char_start, e.char_end)), datasets=consolidate(entries, ambiguous),
            unresolved_mentions=sorted({e.evidence_id for p, e in passages if p.kind in {'support', 'ambiguous'}}),
            coverage={'source_characters': len(self.text), 'covered_characters': covered, 'validated_blocks': len(self.scanned)})
