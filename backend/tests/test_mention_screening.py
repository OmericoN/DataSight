import json
from hashlib import sha256

import pytest

from datasight.application import mention_screening
from datasight.infrastructure.ingestion.renderer import RENDERER_VERSION
from datasight.infrastructure.llm.client import InferenceUnavailable, RequestTooLarge


class ScreeningClient:
    def __init__(self, decide):
        self.decide = decide
        self.excerpts = []

    def complete(self, payload, response_model, record_attempt, validate, *, single_attempt):
        assert single_attempt
        assert payload["max_tokens"] == 32
        excerpt = json.loads(payload["messages"][-1]["content"])["publication_excerpt"]
        self.excerpts.append(excerpt)
        record_attempt({})
        return response_model(decision=self.decide(excerpt))


def screen(tmp_path, client, text, metadata=None, xml_path=None):
    path = tmp_path / "paper.md"
    raw = text.encode()
    path.write_bytes(raw)
    if metadata is None:
        metadata = {"profile": "full_body", "producer_version": RENDERER_VERSION}
    return mention_screening.screen_publication(client, path, sha256(raw).hexdigest(), metadata, xml_path)


@pytest.mark.parametrize("later_decision", ["likely_use", "uncertain", "no_use"])
def test_rejection_requires_all_body_chunks_to_be_negative(tmp_path, monkeypatch, later_decision):
    monkeypatch.setattr(mention_screening, "CHUNK_CHARACTERS", 1_000)
    text = ("This essay examines policy concepts. " * 60) + "\n\nWe interviewed people."
    client = ScreeningClient(lambda excerpt: later_decision if "We interviewed" in excerpt else "no_use")
    decision = screen(tmp_path, client, text + mention_screening.REFERENCES_HEADING + "Other authors' surveys")
    assert decision["decision"] == later_decision
    assert len(client.excerpts) > 1
    assert all("Other authors" not in excerpt for excerpt in client.excerpts)


def test_screen_uses_verified_full_body_for_pruned_markdown(tmp_path):
    xml = tmp_path / "paper.xml"
    raw = b'<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body><div><head>Results</head><p>We interviewed people.</p></div></body></text></TEI>'
    xml.write_bytes(raw)
    client = ScreeningClient(lambda excerpt: "likely_use" if "We interviewed" in excerpt else "no_use")
    metrics = screen(tmp_path, client, "A policy review", {
        "profile": "pruned", "source_sha256": sha256(raw).hexdigest(),
    }, xml)
    assert metrics["decision"] == "likely_use"
    assert metrics["source_profile"] == "full_body"


@pytest.mark.parametrize("decision", ["likely_use", "no_use"])
def test_prioritized_screening_stops_on_positive_and_covers_all_blocks_on_negative(tmp_path, monkeypatch, decision):
    monkeypatch.setattr(mention_screening, "CHUNK_CHARACTERS", 1_000)
    text = ("Background about policy. " * 150) + "\n\n## Data availability\n\nWe deposited our interviews."
    client = ScreeningClient(lambda excerpt: decision if "deposited" in excerpt else "no_use")
    assert screen(tmp_path, client, text)["decision"] == decision
    assert "deposited" in client.excerpts[0]
    if decision == "likely_use":
        assert len(client.excerpts) == 1
    else:
        blocks = mention_screening.source_blocks(text, size=1_000)
        assert sorted(client.excerpts) == sorted(text[b.start:b.end] for b in blocks)


def test_screening_reserves_only_its_own_output_budget(tmp_path, monkeypatch):
    from datasight.infrastructure.llm import client as llm
    monkeypatch.setattr(llm, "count_tokens", lambda _: 7_600)
    monkeypatch.setattr(llm, "_system_tokens", llm.count_tokens)
    client = ScreeningClient(lambda _: "no_use")
    assert screen(tmp_path, client, "Policy essay")["decision"] == "no_use"
    assert client.excerpts == ["Policy essay"]


def test_clear_negative_is_uncertain_when_full_body_is_missing(tmp_path):
    metrics = screen(tmp_path, ScreeningClient(lambda _: "no_use"), "Conceptual framework", {"profile": "pruned"})
    assert metrics["decision"] == "uncertain"
    assert metrics["decision_reason"] == "incomplete_source"


def test_screen_fails_open_on_gateway_error(tmp_path):
    def unavailable(_):
        raise InferenceUnavailable("GPU is unavailable")

    metrics = screen(tmp_path, ScreeningClient(unavailable), "We interviewed people.")
    assert metrics["decision"] == "uncertain"
    assert metrics["decision_reason"] == "InferenceUnavailable"
    assert metrics["http_attempts"] == 1


def test_oversize_request_is_split_without_losing_late_evidence(tmp_path):
    def decide(excerpt):
        if len(excerpt) > 1_600:
            raise RequestTooLarge("Gateway rejected request size")
        return "likely_use" if "We interviewed people." in excerpt else "no_use"

    client = ScreeningClient(decide)
    metrics = screen(tmp_path, client, ("Conceptual framework. " * 120) + "We interviewed people.")
    assert metrics["decision"] == "likely_use"
    assert metrics["http_attempts"] > 1
