from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from datasight import config
from datasight.application.extraction_engine import Block, ExtractionEngine, FEATURES_REPAIR, IndexedLine, IndexedLines, LOCATE_REPAIR, LOCATOR_BLOCK_TOKENS, anchor_passage, consolidate, locator_blocks, source_blocks, validate_observations
from datasight.application.extraction_engine import validate_feature_response
from datasight.application import llm_extraction, pipeline_services
from datasight.application.llm_extraction import extraction_configuration, fingerprint, prepare_source
from datasight.domain.extraction import DatasetEntry, Evidence, EvidenceValue, IdentityFeatures, LocatedPassage, LocatedPassages, Observed, UseFeatures
from datasight.infrastructure.llm.client import GatewayClient, InferenceError, InferenceUnavailable, InvalidCompletion, OutputTruncated, RequestTooLarge, payload_for, strict_schema
from datasight.infrastructure.llm.tokenizer import count_tokens


class MemoryRequests:
    def __init__(self):
        self.requests: dict[str, dict[str, Any]] = {}

    def get_extraction_request(self, extraction_id, key):
        return self.requests.get(key)

    def begin_extraction_request(self, extraction_id, key, payload):
        self.requests[key] = {"status": "running", "request": payload}

    def record_extraction_attempt(self, extraction_id, key, attempt):
        self.requests[key]["attempt"] = attempt

    def finish_extraction_request(self, extraction_id, key, result, error):
        self.requests[key].update(status="failed" if error else "successful", result=result, error=error)


class GroundedClient(GatewayClient):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def close(self):
        pass

    def complete(self, payload, model, record_attempt, validate):
        self.calls += 1
        data = json.loads(payload["messages"][1]["content"])
        if model is LocatedPassages:
            quote = "We used the Alpha cohort."
            passages = []
            if quote in data["text"]:
                passages.append({"block_id": data["block_id"], "quote": quote, "quote_occurrence": 0, "dataset_name": "Alpha cohort", "kind": "reference"})
            result = model.model_validate({"passages": passages})
        else:
            fields = {name: {"state": "not_reported", "values": []} for name in model.model_fields}
            for name, value in (("name", "Alpha cohort"), ("roles", "used"), ("claim_subject", "current_publication")):
                if name in fields:
                    fields[name] = {"state": "reported", "values": [{"value": value, "evidence_ids": [data["evidence"][0]["evidence_id"]]}]}
            result = model.model_validate(fields)
        validate(result)
        record_attempt({"response": {}, "error": None, "elapsed_seconds": 0.01})
        return result


def test_observed_requires_evidence_and_distinguishes_negative_from_missing():
    from datasight.application.bounded_extraction import GROUPS
    assert set(GROUPS) == {"identity", "use"}
    assert extraction_configuration()["schema_version"] == "dataset-evidence-v2"
    assert Observed[str]().state == "not_reported"
    assert Observed[str](state="reported", values=[EvidenceValue(value="Unavailable", evidence_ids=["e1"])]).values[0].value == "Unavailable"
    for invalid in ({"state": "reported", "values": []}, {"state": "not_reported", "values": [{"value": "x", "evidence_ids": ["e"]}]}, {"state": "reported", "values": [{"value": "x", "evidence_ids": []}]}):
        with pytest.raises(ValidationError):
            Observed[str].model_validate(invalid)
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate({"dataset_local_id": "d1", "display_name": "data", "fair_score": 1})
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate({"dataset_local_id": "d1", "display_name": "data", "accessibility": {}})


def test_exact_quotes_and_repeated_occurrences_are_anchored_locally():
    text = "same. same."
    passage = LocatedPassage(block_id="b0_11", quote="same.", quote_occurrence=1, dataset_name=None, kind="reference")
    assert anchor_passage(text, Block(0, len(text)), passage) == (6, 11)
    with pytest.raises(ValueError):
        anchor_passage(text, Block(0, len(text)), passage.model_copy(update={"quote": "invented"}))
    with pytest.raises(ValueError):
        validate_observations({"state": "reported", "values": [{"value": "x", "evidence_ids": ["unknown"]}]}, {"e1"})


def test_all_source_is_scanned_including_no_dataset_documents():
    text = "# A paper\n\n" + "Unrelated research prose. " * 400
    repo, client = MemoryRequests(), GroundedClient()
    result = ExtractionEngine(repo, client, 1, text).run("W1", "hash")
    assert result.datasets == []
    assert result.coverage["covered_characters"] == len(text)
    assert len(source_blocks(text)) > 1
    for left, right in zip(source_blocks(text), source_blocks(text)[1:]):
        assert right.start < left.end


def test_locator_blocks_use_model_tokens_and_preserve_source_offsets():
    text = ("Alpha cohort evidence. " * 1200 + "\n\n") * 5
    blocks = locator_blocks(text)
    assert len(blocks) > 1
    assert max(count_tokens(text[block.start:block.end]) for block in blocks) >= 4500
    assert blocks[0].start == 0 and blocks[-1].end == len(text)
    for index, block in enumerate(blocks):
        assert count_tokens(text[block.start:block.end]) <= LOCATOR_BLOCK_TOKENS
        if index:
            previous = blocks[index - 1]
            assert block.start == previous.end - 300
            assert text[block.start:previous.end] == text[previous.end - 300:previous.end]
        if block.end < len(text):
            assert text[block.end - 2:block.end] == "\n\n"
    passage = LocatedPassage(block_id=blocks[-1].block_id, quote="Alpha cohort evidence.",
                             quote_occurrence=0, dataset_name="Alpha cohort", kind="reference")
    start, end = anchor_passage(text, blocks[-1], passage)
    assert text[start:end] == passage.quote


def test_token_budget_allows_large_text_but_rejects_context_and_character_overflow():
    assert payload_for("extract", "alpha " * 6000, LocatedPassages)
    with pytest.raises(RequestTooLarge):
        payload_for("extract", "a " * 14000, LocatedPassages)
    with pytest.raises(RequestTooLarge):
        payload_for("extract", "a" * 40001, LocatedPassages)


def test_tokenizer_revision_mismatch_fails_explicitly(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODEL_REVISION", "different")
    with pytest.raises(ValueError, match="differs from the bundled tokenizer"):
        count_tokens("Alpha cohort")


def test_missing_or_changed_tokenizer_fails_explicitly(monkeypatch, tmp_path):
    from datasight.infrastructure.llm import tokenizer
    tokenizer._load.cache_clear()
    monkeypatch.setattr(tokenizer, "TOKENIZER_PATH", tmp_path / "missing.json")
    with pytest.raises(ValueError, match="is missing"):
        count_tokens("Alpha cohort")
    tokenizer.TOKENIZER_PATH.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch"):
        count_tokens("Alpha cohort")
    tokenizer._load.cache_clear()


def test_oversized_locator_block_is_subdivided_without_losing_coverage():
    text = "a" * 41000
    result = ExtractionEngine(MemoryRequests(), GroundedClient(), 1, text).run("W1", "hash")
    assert result.coverage["covered_characters"] == len(text)
    assert result.coverage["validated_blocks"] > 1


def test_two_pass_features_are_grounded_and_completed_requests_are_reused():
    repo, client = MemoryRequests(), GroundedClient()
    text = "# Paper\n\n## Methods\n\nWe used the Alpha cohort.\n"
    result = ExtractionEngine(repo, client, 1, text).run("W1", "hash")
    assert len(result.datasets) == 1
    dataset = result.datasets[0]
    assert dataset.use.roles.values[0].value == "used"
    assert set(dataset.model_dump()) == {"dataset_local_id", "display_name", "identity", "use", "occurrences", "review_reasons"}
    for evidence in result.evidence:
        assert text[evidence.char_start:evidence.char_end] == evidence.quote
    calls = client.calls
    rerun = ExtractionEngine(repo, client, 1, text).run("W1", "hash")
    assert rerun == result and client.calls == calls


def test_repeated_named_references_reuse_identical_feature_context():
    class Repeated(GroundedClient):
        def complete(self, payload, model, record_attempt, validate):
            if model is LocatedPassages:
                self.calls += 1
                data = json.loads(payload["messages"][1]["content"])
                result = LocatedPassages(passages=[LocatedPassage(
                    block_id=data["block_id"], quote="We used the Alpha cohort.", quote_occurrence=index,
                    dataset_name="Alpha cohort", kind="reference") for index in (0, 1)])
                validate(result)
                return result
            return super().complete(payload, model, record_attempt, validate)

    client = Repeated()
    result = ExtractionEngine(MemoryRequests(), client, 1,
                              "We used the Alpha cohort.\n\nWe used the Alpha cohort.").run("W1", "hash")
    assert len(result.datasets) == 1
    assert len(result.datasets[0].occurrences) == 2
    assert client.calls == 7  # one locator, two shared groups, two fields per occurrence


@pytest.mark.parametrize("failure", [OutputTruncated, InvalidCompletion])
def test_invalid_or_truncated_locator_is_split_without_losing_coverage(failure):
    class Truncating(GroundedClient):
        def complete(self, payload, model, record_attempt, validate):
            if model is LocatedPassages and len(json.loads(payload["messages"][1]["content"])["text"]) > 700:
                raise failure("retry smaller block")
            return super().complete(payload, model, record_attempt, validate)

    repo, client = MemoryRequests(), Truncating()
    text = "Plain prose. " * 300
    result = ExtractionEngine(repo, client, 1, text).run("W1", "hash")
    calls = client.calls
    assert ExtractionEngine(repo, client, 1, text).run("W1", "hash") == result
    assert client.calls == calls
    assert result.coverage["covered_characters"] == len(text)
    assert result.coverage["validated_blocks"] > 2


def test_invalid_locator_gets_one_stricter_grounding_attempt():
    class Repairing(GroundedClient):
        def complete(self, payload, model, record_attempt, validate):
            if model is LocatedPassages and LOCATE_REPAIR not in payload["messages"][0]["content"]:
                raise InvalidCompletion("invalid quote")
            return super().complete(payload, model, record_attempt, validate)

    repo = MemoryRequests()
    text = "We used the Alpha cohort."
    result = ExtractionEngine(repo, Repairing(), 1, text).run("W1", "hash")
    assert result.coverage["validated_blocks"] == 1
    assert len(result.datasets) == 1
    assert len(repo.requests) > 2  # locator repair plus feature requests


def test_minimum_block_uses_indexed_lines_and_keeps_exact_source_quote():
    class Indexed(GroundedClient):
        def complete(self, payload, model, record_attempt, validate):
            if model is LocatedPassages:
                raise InvalidCompletion("invented quote")
            if model is IndexedLines:
                result = IndexedLines(lines=[IndexedLine(line_index=1, dataset_name="Alpha cohort", kind="reference")])
                validate(result)
                return result
            return super().complete(payload, model, record_attempt, validate)

    text = "Unrelated introduction.\nWe used the Alpha cohort.\n" + "x" * 170
    result = ExtractionEngine(MemoryRequests(), Indexed(), 1, text).run("W1", "hash")
    assert result.coverage["covered_characters"] == len(text)
    assert any(evidence.quote == "We used the Alpha cohort." for evidence in result.evidence)
    assert len(result.datasets) == 1


def test_invalid_feature_gets_one_grounding_repair_before_subdivision():
    class Repairing(GroundedClient):
        def complete(self, payload, model, record_attempt, validate):
            if model is not LocatedPassages and model.__name__.startswith("IdentityFeatures") and FEATURES_REPAIR not in payload["messages"][0]["content"]:
                raise InvalidCompletion("invented alias")
            return super().complete(payload, model, record_attempt, validate)

    repo = MemoryRequests()
    result = ExtractionEngine(repo, Repairing(), 1, "We used the Alpha cohort.").run("W1", "hash")
    assert len(result.datasets) == 1
    assert any(row["status"] == "failed" for row in repo.requests.values())
    assert any(row["status"] == "successful" and FEATURES_REPAIR in row["request"]["messages"][0]["content"]
               for row in repo.requests.values())


def test_unsupported_response_is_not_checkpointed_as_success():
    repo = MemoryRequests()

    class Inventing(GroundedClient):
        def complete(self, payload, model, record_attempt, validate):
            result = LocatedPassages(passages=[LocatedPassage(block_id="wrong", quote="invented", quote_occurrence=0, dataset_name=None, kind="reference")])
            validate(result)
            return result

    with pytest.raises(ValueError):
        ExtractionEngine(repo, Inventing(), 1, "Original text").run("W1", "hash")
    assert all(row["status"] == "failed" for row in repo.requests.values())


def test_conservative_grouping_never_uses_article_identifiers_as_dataset_identity():
    def entry(name, target="dataset", literal="10.1/data"):
        return DatasetEntry(dataset_local_id="pending", display_name=name, identity=IdentityFeatures.model_validate({
            "name": {"state": "reported", "values": [{"value": name, "evidence_ids": ["e1"]}]},
            "identifiers": {"state": "reported", "values": [{"value": {"scheme": "doi", "literal": literal, "target": target}, "evidence_ids": ["e1"]}]},
        }))
    assert len(consolidate([entry("Alpha"), entry("An alias")])) == 1
    assert len(consolidate([entry("Alpha", "research_article"), entry("Beta", "research_article")])) == 2
    assert len(consolidate([entry("Alpha"), entry("Alpha", literal="10.1/other")])) == 2


def test_duplicate_anchor_consolidation_retains_both_occurrence_feature_sets():
    from datasight.domain.extraction import CitationFeatures, Occurrence
    identity = IdentityFeatures(name=Observed[str](state='reported', values=[EvidenceValue(value='Alpha', evidence_ids=['e1'])]))
    used = Occurrence(occurrence_id='o1', evidence_ids=['e1'], bibliography_only=False, use=UseFeatures.model_validate({
        'roles': {'state': 'reported', 'values': [{'value': 'used', 'evidence_ids': ['e1']}]}}))
    cited = Occurrence(occurrence_id='o2', evidence_ids=['e1'], bibliography_only=False, citation=CitationFeatures.model_validate({
        'version': {'state': 'reported', 'values': [{'value': 'v2', 'evidence_ids': ['e1']}]}}))
    entries = [DatasetEntry(dataset_local_id=f'd{i}', display_name='Alpha', identity=identity,
                            occurrences=[occurrence]) for i, occurrence in enumerate((used, cited))]
    result = consolidate(entries)
    assert len(result) == len(result[0].occurrences) == 1
    assert result[0].occurrences[0].use.roles.values[0].value == 'used'
    assert result[0].occurrences[0].citation.version.values[0].value == 'v2'


def test_source_snapshot_is_immutable_and_does_not_modify_pruned_input(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXTRACTIONS_DIR", tmp_path / "extractions")
    xml = tmp_path / "paper.xml"
    xml.write_text('<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body><div><head>Discussion</head><p>We used data.</p></div></body><back><div><head>Appendix</head><p>Extra data.</p></div></back></text></TEI>')
    pruned = tmp_path / "paper.md"
    pruned.write_text("pruned")
    row = {"xml_path": str(xml), "markdown_path": str(pruned)}
    text, path, digest = prepare_source(row)
    assert "Discussion" in text and "Appendix" in text
    assert pruned.read_text() == "pruned"
    assert prepare_source(row)[1] == path
    path.write_text("changed")
    with pytest.raises(ValueError, match="Immutable"):
        prepare_source(row)
    assert fingerprint(digest, extraction_configuration()) != fingerprint("changed", extraction_configuration())


def test_strict_schema_and_token_budget():
    schema = strict_schema(LocatedPassages)
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["LocatedPassage"]["required"] == list(schema["$defs"]["LocatedPassage"]["properties"])
    with pytest.raises(RequestTooLarge):
        payload_for("extract", "a " * 14000, LocatedPassages)


def test_gateway_auth_unavailable_truncation_and_retry_limits(monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://gateway.invalid")
    monkeypatch.setattr(config, "LLM_API_KEY", "test-secret")
    monkeypatch.setattr("datasight.infrastructure.llm.client.time.sleep", lambda value: None)
    calls, attempts = [], []

    def post(url, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=503, json=lambda: {}, headers={})

    client = GatewayClient()
    monkeypatch.setattr(client.session, "post", post)
    with pytest.raises(InferenceUnavailable):
        client.complete({}, LocatedPassages, attempts.append, lambda result: None)
    assert len(calls) == len(attempts) == 1
    assert calls[0]["headers"]["Authorization"] == "Bearer test-secret"
    assert "test-secret" not in json.dumps(attempts)
    monkeypatch.setattr(client.session, "post", lambda *args, **kwargs: SimpleNamespace(status_code=500, json=lambda: {}, headers={}))
    attempts.clear()
    with pytest.raises(InferenceError):
        client.complete({}, LocatedPassages, attempts.append, lambda result: None)
    assert len(attempts) == 3
    monkeypatch.setattr(client.session, "post", lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: {"model": config.LLM_MODEL, "choices": [{"finish_reason": "length"}]}, headers={}))
    with pytest.raises(OutputTruncated):
        client.complete({}, LocatedPassages, attempts.append, lambda result: None)
    client.close()


def test_gateway_prompt_limit_uses_request_subdivision_error(monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://gateway.invalid")
    monkeypatch.setattr(config, "LLM_API_KEY", "test-secret")
    response = {"detail": {"error": {"code": "prompt_too_large"}, "max_prompt_chars": 20000}}
    client = GatewayClient()
    monkeypatch.setattr(client.session, "post", lambda *args, **kwargs: SimpleNamespace(status_code=400, json=lambda: response))
    attempts = []
    try:
        with pytest.raises(RequestTooLarge, match="prompt size"):
            client.complete({}, LocatedPassages, attempts.append, lambda result: None)
        assert len(attempts) == 1
        assert attempts[0]["http_status"] == 400
    finally:
        client.close()


def test_bibliography_does_not_establish_reuse_or_allow_invented_identity():
    evidence = Evidence(evidence_id="e1", block_id="b1", quote="Alpha cohort", char_start=0, char_end=12, section_heading="References", standardized_section="other", placement="bibliography")
    use = UseFeatures.model_validate({"roles": {"state": "reported", "values": [{"value": "used", "evidence_ids": ["e1"]}]}})
    with pytest.raises(ValueError, match="Bibliography"):
        validate_feature_response(use, [evidence])
    identity = IdentityFeatures.model_validate({"name": {"state": "reported", "values": [{"value": "Invented cohort", "evidence_ids": ["e1"]}]}})
    with pytest.raises(ValueError, match="not stated"):
        validate_feature_response(identity, [evidence])


def test_shared_run_boundary_includes_zero_detector_candidates_and_stops_on_outage(monkeypatch):
    class Repo:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extraction_lock(self, run_id, publication_id=None):
            return True
        def get_pipeline_run(self, run_id):
            return {"status": "running", "config": {}}
        def record_stage_start(self, stage, run_id):
            assert (stage, run_id) == ("extract_features", 1)
        def extraction_publications(self, run_id, limit, publication_id):
            return [{"paper_id": name, 'publication_id': name} for name in ("zero-rules", "gpu-off", "not-dispatched", 'bad-source')]
        def latest_publication_extraction(self, run_id, publication_id):
            return None if publication_id == 'bad-source' else {'source_path': 'cached'}
        def record_stage_result(self, *args):
            pass
    visited = []
    def extract(repo, client, run_id, row):
        visited.append(row["paper_id"])
        return {"status": "interrupted" if row["paper_id"] == "gpu-off" else 'failed' if row['paper_id'] == 'bad-source' else "successful", "datasets": 0}
    monkeypatch.setattr(llm_extraction, 'prepare_source', lambda row: (_ for _ in ()).throw(ValueError('No PDF')))
    monkeypatch.setattr(llm_extraction, "PipelineRepository", Repo)
    monkeypatch.setattr(llm_extraction, "GatewayClient", GroundedClient)
    monkeypatch.setattr(llm_extraction, "extract_publication", extract)
    result = pipeline_services.extract_features_from_candidates(pipeline_run_id=1)
    assert visited == ['bad-source', "zero-rules", "gpu-off"]
    assert result["status"] == "interrupted"
    assert result['payload']['failed'] == 1


def test_item_boundary_uses_same_whole_publication_service(monkeypatch):
    class Repo:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def start_item_stage(self, *args):
            return True
        def get_pipeline_item_context(self, item_id):
            return {"pipeline_run_id": 9, "publication_row_id": 10}
        def finish_item_stage(self, item_id, stage, status, result):
            assert status == "successful"
    calls = []
    def extract(run_id, limit=None, publication_id=None):
        calls.append((run_id, publication_id))
        return {"stage": "extract_features", "status": "successful", "count": 0}
    monkeypatch.setattr(pipeline_services, "PipelineRepository", Repo)
    monkeypatch.setattr(llm_extraction, "extract_run", extract)
    assert pipeline_services.extract_features_pipeline_item(20)["status"] == "successful"
    assert calls == [(9, 10)]


def test_dense_single_field_passages_are_subdivided_with_new_source_anchors():
    class Dense(GroundedClient):
        def complete(self, payload, model, record_attempt, validate):
            data = json.loads(payload["messages"][1]["content"])
            if len(data["evidence"][0]["text"]) > 256:
                raise OutputTruncated("dense passage")
            result = model.model_validate({"creators": {"state": "reported", "values": [{"value": "Ada Smith", "evidence_ids": [data["evidence"][0]["evidence_id"]]}]}})
            validate(result)
            return result
    text = "Ada Smith. " * 100
    evidence = Evidence(evidence_id="e1", block_id="b1", quote=text, char_start=0, char_end=len(text),
                        section_heading=None, standardized_section="other", placement="body")
    engine = ExtractionEngine(MemoryRequests(), Dense(), 1, text)
    fields = IdentityFeatures.model_validate(engine.features("Alpha", [evidence], IdentityFeatures, ["creators"]).model_dump())
    assert fields.creators.values[0].value == "Ada Smith"
    assert len(fields.creators.values[0].evidence_ids) > 1
    assert set(fields.creators.values[0].evidence_ids) <= engine.evidence.keys()
