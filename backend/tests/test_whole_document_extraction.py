import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from benchmarks.vllm import replay
from datasight import config
from datasight.application import llm_extraction
from datasight.application.catalog_extraction import (
    CANDIDATE_PROMPT_VERSION, CATALOG_V4, CATALOG_V5, LEGACY_PROMPT_VERSION, PREVIOUS_CATALOG_V4, PROMPT_VERSION,
    make_chunk, run_catalog_extraction, scan_chunk,
)
from datasight.application.whole_document_extraction import WholeDocumentResponse, extract_whole_document
from datasight.infrastructure.llm.client import (
    MAX_PROMPT_CHARS, MAX_TOKENS, GatewayClient, RequestTooLarge, payload_for, prompt_character_count,
)
from datasight.infrastructure.llm.tokenizer import TOKENIZER_MODEL, TOKENIZER_REVISION


def snapshot(tmp_path, text):
    raw = text.encode("utf-8")
    path = tmp_path / "paper.md"
    path.write_bytes(raw)
    return {"paper_id": "W1", "source_path": str(path), "source_sha256": hashlib.sha256(raw).hexdigest()}


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://gateway.invalid")
    monkeypatch.setattr(config, "LLM_API_KEY", "test-secret")
    monkeypatch.setattr(config, "LLM_MODEL", TOKENIZER_MODEL)
    monkeypatch.setattr(config, "LLM_MODEL_REVISION", TOKENIZER_REVISION)
    client = GatewayClient()
    post = MagicMock()
    monkeypatch.setattr(client.session, "post", post)
    try:
        yield client, post
    finally:
        client.close()


def completion(content, finish="stop"):
    return SimpleNamespace(status_code=200, json=lambda: {
        "model": TOKENIZER_MODEL,
        "choices": [{"finish_reason": finish, "message": {"content": content}}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 50},
    })


def test_one_request_and_exact_offsets_for_separate_groups(gateway, tmp_path):
    client, post = gateway
    first = "Nous avons utilisé Alpha 📚."
    unnamed = "The study collected fresh samples."
    text = f"## Méthodes\r\n{first}\r\n{first}\r\n{unnamed}\r\n## References\r\nArchive Dataset"
    post.return_value = completion(json.dumps({"complete": True, "groups": [
        {"name": "Alpha", "identity_uncertain": False, "line_ids": [2, 2]},
        {"name": "Alpha", "identity_uncertain": True, "line_ids": [3]},
        {"name": None, "identity_uncertain": True, "line_ids": [4]},
        {"name": "Archive Dataset", "identity_uncertain": False, "line_ids": [6]},
    ]}))

    result = extract_whole_document(client, snapshot(tmp_path, text))

    post.assert_called_once()
    request = post.call_args.kwargs
    assert not request["allow_redirects"]
    payload = request["json"]
    assert payload["max_tokens"] == MAX_TOKENS
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["response_format"]["json_schema"]["strict"] is True
    supplied = json.loads(payload["messages"][1]["content"])["lines"]
    assert "".join(line for _, line in supplied) == text
    assert result["status"] == "complete"
    assert [group["name"] for group in result["groups"]] == ["Alpha", "Alpha", None, "Archive Dataset"]
    assert result["groups"][-1]["bibliography_only"]
    evidence = result["evidence"]
    assert [item["char_start"] for item in evidence[:2]] == [text.index(first), text.rindex(first)]
    assert [item["quote"] for item in evidence] == [first, first, unnamed, "Archive Dataset"]
    assert all(text[item["char_start"]:item["char_end"]] == item["quote"] for item in evidence)
    assert result["metrics"]["grounded_reference_count"] == result["metrics"]["distinct_identity_count"] == 4
    assert result["metrics"]["uncertain_identity_count"] == 2
    assert result["metrics"]["http_requests"] == 1
    assert result["metrics"]["prompt_tokens"] == 200
    assert result["metrics"]["completion_tokens"] == 50


@pytest.mark.parametrize("text", ["x" * 40_001, "a " * 14_000], ids=["characters", "tokens"])
def test_over_budget_sends_no_request(gateway, tmp_path, text):
    client, post = gateway
    result = extract_whole_document(client, snapshot(tmp_path, text))
    post.assert_not_called()
    assert result["status"] == "incomplete"
    assert result["reasons"] == ["over_budget"]
    assert result["metrics"]["http_requests"] == result["metrics"]["prompt_tokens"] == 0


def test_changed_snapshot_sends_no_request(gateway, tmp_path):
    client, post = gateway
    source = snapshot(tmp_path, "Original source")
    (tmp_path / "paper.md").write_bytes(b"Changed source")
    result = extract_whole_document(client, source)
    post.assert_not_called()
    assert result["reasons"] == ["source_hash_mismatch"]


def test_transient_gateway_failure_is_one_attempt_with_unknown_usage(gateway, tmp_path):
    client, post = gateway
    post.return_value = SimpleNamespace(status_code=500, headers={}, json=lambda: {})
    result = extract_whole_document(client, snapshot(tmp_path, "We collected weather data."))
    post.assert_called_once()
    assert result["reasons"] == ["gateway_failure"]
    assert result["metrics"]["http_requests"] == result["metrics"]["attempts_missing_usage"] == 1
    assert result["metrics"]["prompt_tokens"] is result["metrics"]["completion_tokens"] is None


def test_grounded_partial_response_is_retained_but_incomplete(gateway, tmp_path):
    client, post = gateway
    post.return_value = completion(json.dumps({"complete": False, "groups": [
        {"name": "Invented", "identity_uncertain": False, "line_ids": [1, 2, 99]},
        {"name": None, "identity_uncertain": True, "line_ids": [0]},
    ]}))
    result = extract_whole_document(client, snapshot(tmp_path, "The study collected survey responses.\n\n"))
    assert result["status"] == "incomplete"
    assert result["reasons"] == ["model_incomplete", "ungrounded_evidence"]
    assert len(result["groups"]) == 1 and result["groups"][0]["name"] is None
    assert result["groups"][0]["identity_uncertain"]
    assert result["metrics"]["grounding_failures"] == 4
    assert result["metrics"]["grounded_reference_count"] == 1


@pytest.mark.parametrize(("finish", "content", "reason", "truncations", "references"), [
    ("length", '{"complete":true,"groups":[{"name":null,"identity_uncertain":false,"line_ids":[1]}]}',
     "output_truncated", 1, 1),
    ("length", '{"complete":false,"groups":[', "output_truncated", 1, 0),
    ("stop", '{"complete":false,"groups":[]}', "model_incomplete", 0, 0),
    ("stop", '{"complete":true}', "invalid_completion", 0, 0),
    ("stop", '{"complete":true,"groups":[]}', None, 0, 0),
])
def test_finish_and_schema_both_matter(gateway, tmp_path, finish, content, reason, truncations, references):
    client, post = gateway
    post.return_value = completion(content, finish)
    result = extract_whole_document(client, snapshot(tmp_path, "The study collected survey responses."))
    assert result["status"] == ("incomplete" if reason else "complete")
    assert result["reasons"] == ([reason] if reason else [])
    assert result["metrics"]["output_truncations"] == truncations
    assert result["metrics"]["grounded_reference_count"] == references


def test_replay_exports_missing_snapshot_without_creating_pipeline_records(monkeypatch, tmp_path):
    repository = MagicMock()
    repo = repository.return_value.__enter__.return_value
    repo.get_pipeline_run.return_value = {"run_key": "run-frozen"}
    repo.extraction_publications.return_value = [{"paper_id": "W1"}]
    repo.cursor.fetchall.return_value = [{"paper_id": "W1", "source_path": None, "source_sha256": None}]
    gateway = MagicMock()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(replay, "PipelineRepository", repository)
    monkeypatch.setattr(replay, "GatewayClient", gateway)

    replay.replay_whole_document(42, paper_id="W1", code_revision="test-revision")

    repo.cursor.execute.assert_called_once()
    assert repo.cursor.execute.call_args.args[1] == (42, "W1", "W1")
    repo.create_pipeline_run.assert_not_called()
    repo.begin_publication_extraction.assert_not_called()
    gateway.return_value.complete.assert_not_called()
    report_path, = tmp_path.glob("storage/evaluation/stage-a-*/replay.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["source_run_key"] == "run-frozen"
    assert report["code_revision"] == "test-revision"
    assert report["metrics"]["http_requests"] == 0
    assert report["metrics"]["incomplete_papers"] == 1
    assert report["papers"][0]["reasons"] == ["source_missing"]


@pytest.mark.parametrize(("mode", "expected_status", "attempts"), [
    ("fits", "successful", 1),
    ("oversize", "failed", 0),
    ("unavailable", "interrupted", 1),
])
def test_frozen_stage_a_gui_run_still_uses_stage_a(gateway, tmp_path, monkeypatch, mode, expected_status, attempts):
    client, post = gateway
    monkeypatch.delenv("LLM_EXTRACTION_STRATEGY", raising=False)
    settings = llm_extraction.whole_document_configuration()
    assert settings["prompt_version"] == "whole-document-stage-a-v1"
    if mode == "unavailable":
        post.return_value = SimpleNamespace(status_code=503, json=lambda: {})
    else:
        post.return_value = completion(json.dumps({"complete": True, "groups": [
            {"name": "Alpha", "identity_uncertain": True, "line_ids": [1, 2]},
            {"name": None, "identity_uncertain": False, "line_ids": [2]},
        ]}))

    text = "x" * 40_001 if mode == "oversize" else "The Alpha cohort.\nWe collected additional samples."
    source = snapshot(tmp_path, text)
    repo = MagicMock()
    repo.get_pipeline_run.return_value = {"config": {"extraction": settings}}
    repo.latest_publication_extraction.return_value = None
    repo.get_extraction_request.return_value = None
    repo.begin_publication_extraction.return_value = {"id": 11, "status": "running"}
    repo.cursor.fetchone.return_value = {"id": 23}

    result = llm_extraction.extract_publication(repo, client, 1, {
        "paper_id": "W1", "publication_id": 2,
        "_prepared_source": (text, tmp_path / "paper.md", source["source_sha256"]),
    })

    assert result["status"] == expected_status
    assert post.call_count == repo.record_extraction_attempt.call_count == attempts
    if expected_status == "successful":
        repo.publish_extraction.assert_called_once()
        publication = repo.publish_extraction.call_args.args[1]
        assert publication.coverage["covered_characters"] == len(text)
        named, unnamed = publication.datasets
        assert named.identity.name.values[0].value == "Alpha"
        assert named.identity.name.values[0].evidence_ids == named.occurrences[0].evidence_ids
        assert publication.unresolved_mentions
        assert unnamed.identity.name.state == "not_reported"
    else:
        repo.publish_extraction.assert_not_called()
        repo.fail_publication_extraction.assert_called_once()
        if mode == "oversize":
            assert "full text exceeds the request budget" in result["error"]
        else:
            repo.interrupt_extraction_run.assert_called_once()


def checkpoint_repo(saved=None):
    repo = MagicMock()
    if saved is None:
        saved = {}
    repo.get_extraction_request.side_effect = lambda extraction_id, key: saved.get(key)

    def finish(extraction_id, key, result, error):
        saved[key] = {"status": "failed" if error else "successful", "result": result, "error": error}

    repo.finish_extraction_request.side_effect = finish
    return repo


def test_catalog_rename_preserves_saved_version_and_checkpoint(monkeypatch):
    monkeypatch.setenv("LLM_EXTRACTION_STRATEGY", "fast")
    assert llm_extraction.extraction_configuration()["prompt_version"] == PROMPT_VERSION
    previous = llm_extraction.catalog_configuration("pruned", PREVIOUS_CATALOG_V4)
    assert previous in llm_extraction.supported_extraction_configurations()

    source = "Alpha dataset."
    old_chunk = make_chunk(source, 0, len(source), MAX_PROMPT_CHARS, prompt_version=PREVIOUS_CATALOG_V4)
    new_chunk = make_chunk(source, 0, len(source), MAX_PROMPT_CHARS, prompt_version=CATALOG_V4)
    assert old_chunk.key == f"fast:0:{len(source)}:catalog-use"
    assert new_chunk.key == f"catalog:0:{len(source)}:catalog-use"
    assert old_chunk.payload == new_chunk.payload


@pytest.mark.parametrize("profile", ["pruned", "full_body"])
def test_catalog_run_uses_selected_render_and_continues_if_a_detected_file_disappears(gateway, tmp_path, monkeypatch, profile):
    client, post = gateway
    monkeypatch.delenv("LLM_EXTRACTION_STRATEGY", raising=False)
    monkeypatch.setattr(config, "EXTRACTIONS_DIR", tmp_path / "extractions")
    settings = llm_extraction.extraction_configuration(render_profile=profile)
    assert settings["prompt_version"] == PROMPT_VERSION
    assert llm_extraction.catalog_configuration(profile, LEGACY_PROMPT_VERSION) in llm_extraction.supported_extraction_configurations()
    assert llm_extraction.extraction_configuration()["render_profile"] == "pruned"
    text = "We used the Alpha cohort.\r\nData were archived."
    source = snapshot(tmp_path, text)
    post.return_value = completion('{"c":true,"g":[["Alpha",false,[1],[["role_used",1,"We used the Alpha cohort"]]]]}')

    repo = checkpoint_repo()
    repo.__enter__.return_value = repo
    repo.get_pipeline_run.return_value = {"status": "running", "config": {"extraction": settings}}
    repo.latest_publication_extraction.return_value = None
    repo.begin_publication_extraction.return_value = {"id": 11, "status": "running"}
    repo.cursor.fetchone.return_value = {"id": 23}
    repo.extraction_publications.return_value = [
        {"paper_id": "missing", "publication_id": 1, "markdown_path": str(tmp_path / "gone.md"),
         "markdown_sha256": "gone", "markdown_metadata": {
             "profile": profile, "producer_version": llm_extraction.RENDERER_VERSION,
         }},
        {"paper_id": "W1", "publication_id": 2, "markdown_path": source["source_path"],
         "markdown_sha256": source["source_sha256"], "markdown_metadata": {
             "profile": profile, "producer_version": llm_extraction.RENDERER_VERSION,
         }},
    ]
    monkeypatch.setattr(llm_extraction, "PipelineRepository", lambda: repo)
    monkeypatch.setattr(llm_extraction, "GatewayClient", lambda: client)

    result = llm_extraction.extract_run(9)

    assert result["status"] == "completed_with_errors"
    assert [item["status"] for item in result["payload"]["results"]] == ["failed", "successful"]
    repo.extraction_publications.assert_called_once_with(
        9, None, None, render_profile=profile, renderer_version=llm_extraction.RENDERER_VERSION,
    )
    post.assert_called_once()
    assert post.call_args.kwargs["json"]["response_format"]["json_schema"]["name"] == "CatalogUseChunkResponse"
    repo.publish_extraction.assert_called_once()
    published = repo.publish_extraction.call_args.args[1]
    assert published.coverage["validated_blocks"] == 1
    assert published.datasets[0].display_name == "Alpha"
    assert published.datasets[0].use.roles.values[0].value == "used"
    assert published.datasets[0].occurrences[0].use.claim_subject.values[0].value == "current_publication"
    assert (tmp_path / "extractions" / "sources" / profile / f"{published.source_sha256}.md").read_bytes() == text.encode("utf-8")


def test_catalog_source_does_not_substitute_tei_for_detected_markdown(tmp_path, monkeypatch):
    render = MagicMock()
    monkeypatch.setattr(llm_extraction, "extract_markdown", render)
    tei = tmp_path / "paper.xml"

    with pytest.raises(ValueError, match="No detected pruned Markdown"):
        llm_extraction.prepare_catalog_source({"xml_path": str(tei)}, "pruned")

    render.assert_not_called()


def test_catalog_catalog_fields_grounded_per_dataset_and_resumable(gateway):
    client, post = gateway
    text = ("Alpine (ARGA), released by A. Reed in 2020 on Dryad, measures valley rainfall.\n"
            "Alpine version 2, 2014 wave. Download https://example.org/a.csv. Dataset DOI 10.5555/alpine.\n"
            "Paper DOI 10.5555/article. GSE12345 is a subset of ParentStudy.\n"
            "Alpine was accessed on 2 May via https://example.org/alpine and hdl:1234/alpine.")
    post.return_value = completion(json.dumps({"c": True, "g": [
        ["Alpine", False, [1, 2, 4], [
            ["aliases", 1, "ARGA"], ["aliases", 1, "ARGA"], ["creators", 1, "A. Reed"],
            ["year", 1, "2020"], ["repository", 1, "Dryad"], ["description", 1, "valley rainfall"],
            ["versions", 2, "version 2"], ["subsets", 2, "2014 wave"], ["doi", 2, "10.5555/alpine"],
            ["download", 2, "https://example.org/a.csv"], ["doi", 2, "10.5555/alp"],
            ["doi", 2, "10.5555/article"], ["access_dates", 4, "2 May"],
            ["landing_page", 4, "https://example.org/alpine"], ["handle", 4, "hdl:1234/alpine"],
        ]],
        ["GSE12345", False, [3], [["aliases", 3, "ParentStudy"]]],
    ]}))
    repo = checkpoint_repo()
    result = run_catalog_extraction(repo, client, 1, text, "W1", "hash")
    assert run_catalog_extraction(repo, client, 1, text, "W1", "hash") == result
    post.assert_called_once()
    alpine, other = result.datasets
    identity = alpine.identity
    assert [v.value.literal for v in identity.identifiers.values] == ["10.5555/alpine", "hdl:1234/alpine"]
    assert [v.value.target for v in identity.urls.values] == ["download", "landing_page"]
    assert [v.value for v in identity.aliases.values] == ["ARGA"]
    assert identity.year.values[0].value == 2020
    assert all(observed.state == "reported" for field, observed in identity if field != "accessions")
    assert alpine.review_reasons == ["Review dataset doi evidence"]
    assert other.identity.accessions.values[0].value == "GSE12345"
    assert other.identity.aliases.state == other.identity.identifiers.state == "not_reported"
    quotes = {item.evidence_id: text[item.char_start:item.char_end] for item in result.evidence}
    assert quotes[identity.creators.values[0].evidence_ids[0]].startswith("Alpine (ARGA)")
    assert quotes[identity.identifiers.values[0].evidence_ids[0]].startswith("Alpine version 2")


def test_catalog_rejects_an_explicit_article_doi_on_the_same_line_as_the_dataset_doi(gateway):
    client, post = gateway
    text = "We used Alpha. Paper DOI: https://doi.org/10.5555/article. Dataset DOI: 10.5555/alpha."
    post.return_value = completion(json.dumps({"c": True, "g": [
        ["Alpha", False, [1], [["doi", 1, "10.5555/article"], ["landing_page", 1, "https://doi.org/10.5555/article"],
                               ["doi", 1, "10.5555/alpha"]]],
    ]}))
    data, = run_catalog_extraction(checkpoint_repo(), client, 1, text, "W1", "hash").datasets
    assert [item.value.literal for item in data.identity.identifiers.values] == ["10.5555/alpha"]
    assert data.identity.urls.state == "not_reported"


def test_catalog_roles_require_body_evidence_and_supported_text(gateway):
    client, post = gateway
    text = "We used Alpha.\n# References\n- **[1]** Beta [dataset]."
    post.return_value = completion(json.dumps({"c": True, "g": [
        ["Alpha", False, [1], [["role_created", 1, "We created Alpha"]]],
        ["Beta", False, [3], [["role_used", 3, "Beta"]]],
    ]}))
    result = run_catalog_extraction(checkpoint_repo(), client, 1, text, "W1", "hash", prompt_version=CATALOG_V5)
    assert len(result.datasets) == 2
    assert all(entry.use.roles.state == "not_reported" for entry in result.datasets)


def test_catalog_catalog_rejects_fact_citing_another_group(gateway):
    client, post = gateway
    post.return_value = completion('{"c":true,"g":[["Alpha",false,[1],[["repository",2,"Dryad"]]],["Beta",false,[2],[]]]}')
    with pytest.raises(ValueError, match="outside its dataset"):
        run_catalog_extraction(checkpoint_repo(), client, 1, "Alpha\nBeta on Dryad", "W1", "hash", prompt_version=CATALOG_V4)


def test_catalog_recovers_out_of_group_facts_without_losing_supported_identity(gateway):
    client, post = gateway
    text = "We used the Alpha cohort.\nAlpha was archived on Dryad.\nBeta on Zenodo."
    post.return_value = completion(json.dumps({"c": True, "g": [
        ["Alpha", False, [1, 2], [["role_used", 1, "We used the Alpha cohort"],
                                     ["repository", 2, "Dryad"], ["repository", 3, "Zenodo"]]],
        ["Beta", False, [3], [["repository", 3, "Zenodo"]]],
    ]}))
    result = run_catalog_extraction(checkpoint_repo(), client, 1, text, "W1", "hash")

    alpha, beta = result.datasets
    assert result.coverage["covered_characters"] == len(text)
    assert [value.value for value in alpha.identity.repository.values] == ["Dryad"]
    assert [value.value for value in beta.identity.repository.values] == ["Zenodo"]
    assert alpha.review_reasons == ["Review dataset repository evidence"]
    post.assert_called_once()


def test_catalog_links_deposit_to_the_right_data_collection(gateway):
    client, post = gateway
    text = ("Lung3 and Radiogenomics are two public lung datasets.\n"
            "## Data availability\n"
            "The datasets analyzed in this work (Lung3 and Radiogenomics) are publicly available "
            "from The Cancer Imaging Archive (TCIA) ["
            "https://wiki.cancerimagingarchive.net/display/Public/NSCLC-Radiomics-Genomics, "
            "https://wiki.cancerimagingarchive.net/display/Public/NSCLC+Radiogenomics].\n"
            "## References\n"
            "- **[Aerts, 2015]** {#b1} Aerts (2015). Data From NSCLC-Radiomics-Genomics. "
            "DOI: https://doi.org/10.7937/K9/TCIA.2015.L4FRET6Z\n"
            "- **[Bakr, 2018]** {#b2} Bakr (2018). A radiogenomic dataset of non-small cell lung cancer. "
            "DOI: https://doi.org/10.1038/sdata.2018.202")
    post.return_value = completion(json.dumps({"c": True, "g": [
        ["Lung3", False, [1], []],
        ["Radiogenomics", False, [1, 3, 5, 6], [
            ["aliases", 1, "two public lung datasets"],
            ["aliases", 5, "Data From NSCLC-Radiomics-Genomics"],
            ["landing_page", 3, "https://wiki.cancerimagingarchive.net/display/Public/NSCLC-Radiomics-Genomics"],
            ["doi", 6, "https://doi.org/10.1038/sdata.2018.202"],
        ]],
    ]}))
    result = run_catalog_extraction(checkpoint_repo(), client, 1, text, "W1", "hash")
    lung, radiogenomics = result.datasets
    assert [v.value.literal for v in lung.identity.identifiers.values] == ["https://doi.org/10.7937/K9/TCIA.2015.L4FRET6Z"]
    assert [v.value for v in lung.identity.creators.values] == ["Aerts"]
    assert [v.value for v in lung.identity.year.values] == [2015]
    assert lung.identity.urls.values[0].value.url.endswith("NSCLC-Radiomics-Genomics")
    assert radiogenomics.identity.urls.values[0].value.url.endswith("NSCLC+Radiogenomics")
    assert [v.value for v in radiogenomics.identity.aliases.values] == ["NSCLC+Radiogenomics"]
    assert radiogenomics.identity.identifiers.state == "not_reported"
    assert [v.value for v in radiogenomics.use.roles.values] == ["used"]
    assert "Review dataset aliases evidence" in radiogenomics.review_reasons
    by_id = {item.evidence_id: item for item in result.evidence}
    doi_evidence = by_id[lung.identity.identifiers.values[0].evidence_ids[0]]
    assert doi_evidence.placement == "bibliography"
    assert text[doi_evidence.char_start:doi_evidence.char_end] == doi_evidence.quote and "{#b1}" in doi_evidence.quote
    payload = json.loads(post.call_args.kwargs["json"]["messages"][1]["content"])
    assert payload["possible_data_deposit_lines"] == [5]


def test_exact_citation_title_keeps_only_its_own_zenodo_record(gateway):
    client, post = gateway
    url = "https://zenodo.org/records/8026660"
    text = ("## References\n"
            f"- **[1]** {{#b1}} Unknown (). CT Lung Lesion. URL: {url}\n"
            f"- **[2]** {{#b2}} Vale (2022). Duke Liver [90] 310 MRI Liver. URL: {url}")
    post.return_value = completion(json.dumps({"c": True, "g": [
        ["CT Lung Lesion", False, [2], [["download", 2, url]]],
        ["Duke Liver", False, [3], [["download", 3, url]]],
    ]}))
    lung, liver = run_catalog_extraction(checkpoint_repo(), client, 1, text, "W", "hash", prompt_version=CATALOG_V5).datasets
    link, = lung.identity.urls.values
    assert link.value.url == url and link.value.target == "landing_page"
    assert all(o.bibliography_only for o in lung.occurrences)
    assert lung.use.roles.state == "not_reported" and liver.identity.urls.state == "not_reported"
    assert liver.review_reasons


def test_same_title_with_two_zenodo_records_requires_review(gateway):
    client, post = gateway
    url = "https://zenodo.org/records/1"
    text = ("## References\n"
            f"- **[1]** {{#b1}} Unknown (). Atlas. URL: {url}\n"
            "- **[2]** {#b2} Unknown (). Atlas. URL: https://zenodo.org/records/2")
    post.return_value = completion(json.dumps({"c": True, "g": [["Atlas", False, [2], [["download", 2, url]]]]}))
    dataset, = run_catalog_extraction(checkpoint_repo(), client, 1, text, "W", "hash", prompt_version=CATALOG_V5).datasets
    assert dataset.identity.urls.state == "not_reported" and dataset.review_reasons


def test_numbered_tcia_links_recover_wrapped_host_and_slug_with_original_evidence(gateway):
    client, post = gateway
    base = "https://wiki.cancerimagingarchive.net/display/Public/"
    wrapped = base.replace("wiki.", "wiki. ")
    line = (f"The following are available: dataset 1-{wrapped}NSCLC-Radiomics; "
            f"dataset 6-{wrapped}NSCLC+ Radiogenomics# 123; dataset 7-{wrapped}Unknown-Imaging.")
    text = "We analyzed NSCLC-Radiomics and NSCLC Radiogenomics alongside controls.\n" + line
    post.return_value = completion(json.dumps({"c": True, "g": [
        [name, False, [1], []] for name in ("NSCLC-Radiomics", "NSCLC Radiogenomics", "controls")
    ]}))
    result = run_catalog_extraction(checkpoint_repo(), client, 1, text, "W", "hash")
    alpha, beta, controls = result.datasets
    for dataset, slug in ((alpha, "NSCLC-Radiomics"), (beta, "NSCLC+Radiogenomics")):
        link, = dataset.identity.urls.values
        assert link.value.url == base + slug
        cite, = [e for e in result.evidence if e.evidence_id in link.evidence_ids]
        assert cite.quote == line == text[cite.char_start:cite.char_end]
    assert controls.identity.urls.state == "not_reported"


def test_catalog_skips_unparsed_tables_and_ordinary_citations_and_requires_a_deposit_link(gateway, monkeypatch):
    client, post = gateway
    monkeypatch.setattr("datasight.application.catalog_extraction.CATALOG_TARGET_CHARS", 500)
    text = ("We used Alpha.\n\n## References\n"
            "- **[A]** {#method} Smith (2021). Cyclemix. DOI: https://doi.org/10.1234/article\n"
            "- **[B]** {#data} Reed (2020). Data From Atlas. DOI: https://doi.org/10.1234/atlas\n"
            "- **[C]** {#ambiguous} Reed (2020). Data From Beta. DOI: https://doi.org/10.1234/one "
            "DOI: https://doi.org/10.1234/two\n\n"
            "## Unparsed appendix table material\n\n"
            + ("> {#row} El Fragments. URL: https://zenodo.org/records/1234\n" * 40))
    post.return_value = completion(json.dumps({"c": True, "g": [
        ["Alpha", False, [1, 4], [["role_used", 1, "We used Alpha"], ["doi", 4, "https://doi.org/10.1234/article"]]],
        ["Cyclemix", False, [4], []],
        ["Atlas", False, [5], []],
        ["Beta", False, [6], []],
    ]}))
    result = run_catalog_extraction(checkpoint_repo(), client, 1, text, "W", "hash")
    post.assert_called_once()
    supplied = post.call_args.kwargs["json"]["messages"][1]["content"]
    assert "Cyclemix" not in supplied and "El Fragments" not in supplied
    alpha, atlas = result.datasets
    assert alpha.display_name == "Alpha" and not alpha.identity.identifiers.values
    assert [v.value for v in alpha.use.roles.values] == ["used"]
    assert atlas.display_name == "Atlas" and atlas.identity.identifiers.values[0].value.literal.endswith("/atlas")
    assert all(o.bibliography_only for o in atlas.occurrences)
    assert not atlas.use.roles.values
    assert result.coverage["covered_characters"] == len(text)
    assert result.coverage["validated_blocks"] == 1


def test_catalog_does_not_treat_prior_work_as_current_use(gateway):
    client, post = gateway
    text = ("A study comparing different methods used the Alpha cohort.\n"
            "We analyzed Beta data collected in a previous study.")
    post.return_value = completion(json.dumps({"c": True, "g": [
        ["Alpha", False, [1], [["role_used", 1, "used the Alpha cohort"]]],
        ["Beta", False, [2], [["role_used", 2, "analyzed Beta data"]]],
    ]}))
    alpha, beta = run_catalog_extraction(checkpoint_repo(), client, 1, text, "W1", "hash").datasets
    assert alpha.use.roles.state == "not_reported"
    assert "Review dataset role_used evidence" in alpha.review_reasons
    assert [item.value for item in beta.use.roles.values] == ["used"]


def test_catalog_follows_single_dataset_citations_in_a_multi_dataset_paragraph(gateway):
    client, post = gateway
    text = ("We used two cohorts. The first is the Alpha dataset [1]{#deposit}. "
            "The second is the Beta dataset [2]{#article}. Both Alpha and Beta were evaluated [3]{#shared}.\n"
            "## References\n"
            "- **[A, 2015]** {#deposit} A (2015). Data From Valley Imaging. DOI: https://doi.org/10.5555/valley\n"
            "- **[B, 2018]** {#article} B (2018). A study of Beta. DOI: https://doi.org/10.5555/article\n"
            "- **[C, 2020]** {#shared} C (2020). Data From Separate Collection. DOI: https://doi.org/10.5555/shared")
    post.return_value = completion(json.dumps({"c": True, "g": [
        ["Alpha", False, [1], []],
        ["Beta", False, [1], []],
    ]}))
    result = run_catalog_extraction(checkpoint_repo(), client, 1, text, "W1", "hash")
    alpha, beta = result.datasets
    identifier, = alpha.identity.identifiers.values
    assert identifier.value.literal == "https://doi.org/10.5555/valley"
    by_id = {item.evidence_id: item.quote for item in result.evidence}
    assert any("Alpha dataset [1]{#deposit}" in by_id[eid] for eid in identifier.evidence_ids)
    assert any("Data From Valley Imaging" in by_id[eid] for eid in identifier.evidence_ids)
    assert beta.identity.identifiers.state == "not_reported"


def test_catalog_keeps_repository_articles_and_neighboring_deposits_off_the_wrong_dataset(gateway):
    client, post = gateway
    text = ("We compared Alpha [1]{#article} [2]{#deposit} to earlier work.\n"
            "We also analyzed Valley Imaging.\n## References\n"
            "- **[A]** {#article} Reed (2020). Assessing coverage in GEO. DOI: 10.5555/article\n"
            "- **[B]** {#deposit} Vale (2021). Data From Valley Imaging. DOI: 10.5555/valley")
    post.return_value = completion(json.dumps({"c": True, "g": [
        ["Alpha", False, [1, 4, 5], [["doi", 4, "10.5555/article"], ["doi", 5, "10.5555/valley"]]],
        ["Valley Imaging", False, [2], []],
    ]}))
    alpha, valley = run_catalog_extraction(checkpoint_repo(), client, 1, text, "W1", "hash").datasets
    assert alpha.identity.identifiers.state == "not_reported"
    assert [item.value.literal for item in valley.identity.identifiers.values] == ["10.5555/valley"]


def test_deposit_with_two_different_dois_keeps_byline_and_requires_identifier_review(gateway):
    client, post = gateway
    text = ("We analyzed Alpine [1]{#b1}.\n## References\n"
            "- **[A]** {#b1} Vale (2021). Data From Alpine. "
            "DOI: https://doi.org/10.5555/a DOI: https://doi.org/10.5555/b")
    post.return_value = completion(json.dumps({"c": True, "g": [
        ["Alpine", False, [1, 3], [["doi", 3, "10.5555/a"]]],
    ]}))
    dataset, = run_catalog_extraction(checkpoint_repo(), client, 1, text, "W", "hash").datasets
    assert dataset.identity.identifiers.state == "not_reported"
    assert [item.value for item in dataset.identity.creators.values] == ["Vale"]
    assert "Review dataset identifiers evidence" in dataset.review_reasons


def test_catalog_merges_an_explicitly_declared_short_dataset_name(gateway):
    client, post = gateway
    text = ("The Logs were validated.\n"
            "We used the North Valley Logs dataset, which we will refer to as the Logs dataset.\n"
            "We observed the South Valley Logs.")
    post.return_value = completion(json.dumps({"c": True, "g": [
        ["Logs", False, [1], []],
        ["North Valley Logs", False, [2], [["aliases", 2, "Logs dataset"]]],
        ["South Valley Logs", False, [3], []],
    ]}))
    merged, separate = run_catalog_extraction(checkpoint_repo(), client, 1, text, "W1", "hash").datasets
    assert merged.display_name == "Logs"
    assert "North Valley Logs" in {item.value for item in merged.identity.aliases.values}
    assert len(merged.occurrences) == 2
    assert separate.display_name == "South Valley Logs"


def test_catalog_further_splits_failed_halves_and_reuses_successful_children(gateway):
    client, post = gateway
    text = "Alpha cohort.\n\nBeta samples.\n\nGamma corpus.\n\nDelta records."
    success = completion('{"c":true,"g":[]}')
    post.side_effect = [
        completion("", "length"), completion('{"c":false,"g":[]}'),
        success, success, success,
    ]
    repo = checkpoint_repo()
    result = run_catalog_extraction(repo, client, 11, text, "W1", "hash")
    assert result.coverage == {"source_characters": len(text), "covered_characters": len(text), "validated_blocks": 3}
    assert post.call_count == 5
    assert run_catalog_extraction(repo, client, 11, text, "W1", "hash") == result
    assert post.call_count == 5


def test_catalog_reports_a_terminal_failure_at_the_split_limit(gateway):
    client, post = gateway
    post.return_value = completion("", "length")
    with pytest.raises(ValueError, match="8 of 8 chunks failed"):
        run_catalog_extraction(checkpoint_repo(), client, 11, "Alpha data paragraph.\n\n" * 80, "W1", "hash")
    assert post.call_count == 15


def test_catalog_catalog_distinguishes_deposit_and_article_byline(gateway):
    client, post = gateway
    text = ("## References\n"
            "- **[Reed, 2020]** {#b1} Reed, Vale (2020). Alpine [Dataset]. Dryad. DOI: 10.5555/alpine\n"
            "- **[Lee, 2023]** {#b2} Lee (2023). Measuring Alpine. DOI: 10.5555/article")
    post.return_value = completion(json.dumps({"c": True, "g": [
        ["Alpine", False, [2, 3], [
            ["creators", 2, "Reed, Vale (2020)"], ["aliases", 2, "Dryad"], ["doi", 2, "10.5555/alpine"],
            ["creators", 3, "Lee"], ["year", 3, "2023"], ["doi", 3, "10.5555/article"],
        ]],
    ]}))
    result = run_catalog_extraction(checkpoint_repo(), client, 1, text, "W1", "hash", prompt_version=CATALOG_V5)
    data = json.loads(post.call_args.kwargs["json"]["messages"][1]["content"])
    assert data["possible_data_deposit_lines"] == [2]
    identity = result.datasets[0].identity
    assert identity.aliases.state == "not_reported"
    assert [v.value for v in identity.creators.values] == ["Reed", "Vale"]
    assert [v.value for v in identity.year.values] == [2020]
    assert [v.value for v in identity.repository.values] == ["Dryad"]
    assert [v.value.literal for v in identity.identifiers.values] == ["10.5555/alpine"]


def test_shared_acronym_does_not_merge_distinct_catalog_candidates(gateway):
    client, post = gateway
    text = "Alpha Survey (AS)\nAlpine Study (AS)"
    post.return_value = completion(json.dumps({"c": True, "g": [
        ["Alpha Survey", False, [1], [["aliases", 1, "AS"]]],
        ["Alpine Study", False, [2], [["aliases", 2, "AS"]]],
    ]}))
    result = run_catalog_extraction(checkpoint_repo(), client, 1, text, "W1", "hash")
    assert [dataset.display_name for dataset in result.datasets] == ["Alpha Survey", "Alpine Study"]


@pytest.mark.parametrize("long_line", ["x" * 40_001, "a " * 14_000], ids=["characters", "tokens"])
def test_catalog_preplanned_chunks_cover_long_lines_with_exact_offsets(gateway, long_line):
    client, post = gateway
    text = f"## Méthodes\r\n{long_line}😀\r\n## References\r\nArchive Dataset"
    supplied = []

    def scan(*args, **kwargs):
        lines = json.loads(kwargs["json"]["messages"][1]["content"])["lines"]
        supplied.extend(line for _, line in lines)
        return completion(json.dumps({"c": True, "g": [[None, True, [lines[-1][0]], []]]}))

    post.side_effect = scan
    result = run_catalog_extraction(checkpoint_repo(), client, 11, text, "W1", "hash", prompt_version=CATALOG_V5)

    assert post.call_count >= 2
    assert result.coverage["validated_blocks"] == post.call_count
    assert "".join(supplied) == text
    assert all(text[item.char_start:item.char_end] == item.quote for item in result.evidence)
    assert result.evidence[-1].placement == "bibliography"
    assert len({item.dataset_local_id for item in result.datasets}) == post.call_count


@pytest.mark.parametrize(("finish", "content"), [
    ("length", '{"c":false,"g":[]}'),
    ("stop", '{"c":true,"g":[[null,true,[99],[]]]}'),
    ("stop", '{"c":false,"g":[]}'),
])
def test_catalog_truncation_splits_once_and_resume_reuses_checkpoints(gateway, finish, content):
    client, post = gateway
    repo = checkpoint_repo()
    success = completion('{"c":true,"g":[]}')
    post.side_effect = [completion("", "length"), success, completion(content, finish)]
    text = "Alpha data.\n\nBeta samples."

    with pytest.raises(ValueError, match="1 of 2 chunks failed"):
        run_catalog_extraction(repo, client, 11, text, "W1", "hash", prompt_version=CATALOG_V4)

    assert post.call_count == 3
    assert repo.record_extraction_progress.call_args.args[1]["completed_chunks"] == 1
    post.side_effect = None
    post.return_value = success
    result = run_catalog_extraction(repo, client, 11, text, "W1", "hash", prompt_version=CATALOG_V4)

    expected_blocks = 3 if finish == "length" or not json.loads(content)["c"] else 2
    assert post.call_count == 3 + expected_blocks - 1
    assert result.datasets == []
    assert result.coverage["validated_blocks"] == expected_blocks
    progress = repo.record_extraction_progress.call_args.args[1]
    assert progress["planned_chunks"] == progress["completed_chunks"] == expected_blocks
    assert progress["attempts_this_invocation"] == expected_blocks - 1


@pytest.mark.parametrize("saved_response", [False, True])
def test_catalog_keeps_valid_evidence_when_optional_name_is_unsupported(gateway, saved_response):
    client, post = gateway
    text = "We collected data from the Alpha cohort."
    answer = completion(json.dumps({"complete": True, "groups": [
        {"name": "Alpha Cohort Dataset", "identity_uncertain": False, "line_ids": [1]},
    ]}))
    post.return_value = answer
    saved = {}
    if saved_response:
        saved[f"fast:0:{len(text)}"] = {
            "status": "failed", "result": None,
            "error": "Invalid completion: 1 invalid source references or names",
            "attempts": [{"http_status": 200, "response": answer.json(), "error": None}],
        }
    repo = checkpoint_repo(saved)

    result = run_catalog_extraction(repo, client, 11, text, "W1", "hash", prompt_version=LEGACY_PROMPT_VERSION)

    assert post.call_count == (0 if saved_response else 1)
    assert result.datasets[0].display_name == "Unnamed dataset"
    assert result.datasets[0].identity.name.state == "not_reported"
    assert result.datasets[0].review_reasons == ["Dataset identity is uncertain"]
    assert result.evidence[0].quote == text
    assert result.unresolved_mentions == [result.evidence[0].evidence_id]
    assert saved[f"fast:0:{len(text)}"]["status"] == "successful"


@pytest.mark.parametrize(("ids", "facts", "error"), [
    ([2], [], "invalid source references"),
    ([1, 2], [["repository", 2, "Dryad"]], "outside its dataset"),
    ([1, 4], [], "invalid source references"),
])
def test_catalog_rejects_unsupported_line_evidence(gateway, ids, facts, error):
    client, post = gateway
    post.return_value = completion(json.dumps({"c": True, "g": [["Alpha", False, ids, facts]]}))
    with pytest.raises(ValueError, match=error):
        run_catalog_extraction(checkpoint_repo(), client, 1, "Alpha\n\nDryad", "W1", "hash", prompt_version=CATALOG_V4)


@pytest.mark.parametrize("valid", [True, False])
def test_interrupted_catalog_regrounds_a_saved_http_response(gateway, valid):
    client, post = gateway
    text = "Alpha cohort."
    chunk = make_chunk(text, 0, len(text), MAX_PROMPT_CHARS, prompt_version=PROMPT_VERSION)
    answer = completion('{"c":true,"g":[["Alpha",false,[1],[]]]}')
    saved = answer if valid else completion('{"c":true,"g":[["Alpha",false,[9],[]]]}')
    repo = checkpoint_repo({chunk.key: {"status": "running", "request": chunk.payload,
        "attempts": [{"response": saved.json(), "http_status": 200, "error": None}]}})
    post.return_value = answer
    result = scan_chunk(repo, client, 1, text, chunk, 0, MAX_PROMPT_CHARS, MagicMock())
    assert post.call_count == (0 if valid else 1)
    assert result["evidence"][0]["quote"] == text


def test_catalog_recovers_saved_response_with_redundant_blank_line(gateway):
    client, post = gateway
    text = "Alpha cohort.\n\nData deposited on Dryad."
    chunk = make_chunk(text, 0, len(text), MAX_PROMPT_CHARS, prompt_version=PREVIOUS_CATALOG_V4)
    answer = completion('{"c":true,"g":[["Alpha",false,[1,2,3],[["repository",3,"Dryad"]]]]}')
    repo = checkpoint_repo({chunk.key: {
        "status": "failed", "error": "Invalid completion: 1 invalid source references",
        "attempts": [{"response": answer.json()}],
    }})
    result = scan_chunk(repo, client, 11, text, chunk, 0, MAX_PROMPT_CHARS, MagicMock())
    post.assert_not_called()
    assert [item["quote"] for item in result["evidence"]] == ["Alpha cohort.", "Data deposited on Dryad."]
    assert result["groups"][0]["identity"]["repository"]["values"][0]["evidence_ids"] == [result["evidence"][1]["evidence_id"]]


def test_catalog_resumes_a_terminal_truncation_with_compact_output(gateway):
    client, post = gateway
    text = "The study used the Alpha cohort."
    chunk = make_chunk(text, 0, len(text), MAX_PROMPT_CHARS)
    saved = {chunk.key: {"status": "failed", "result": {"reason": "output_truncated"}}}
    repo = checkpoint_repo(saved)
    post.return_value = completion('{"c":true,"g":[["Alpha",false,[1]]]}')

    result = scan_chunk(repo, client, 11, text, chunk, 2, MAX_PROMPT_CHARS, MagicMock())

    post.assert_called_once()
    assert post.call_args.kwargs["json"]["response_format"]["json_schema"]["name"] == "CompactChunkResponse"
    assert result["groups"][0]["name"] == "Alpha"
    assert result["evidence"][0]["quote"] == text
    assert saved[chunk.key + ":compact"]["status"] == "successful"
    assert scan_chunk(repo, client, 11, text, chunk, 2, MAX_PROMPT_CHARS, MagicMock()) == result
    post.assert_called_once()


def test_prompt_character_budget_includes_message_roles(gateway):
    data = {"lines": [[1, "Dataset"]]}
    payload = payload_for("Scan source", data, WholeDocumentResponse)
    limit = prompt_character_count(payload["messages"])
    assert limit == sum(len(message["content"]) for message in payload["messages"]) + 10
    assert payload_for("Scan source", data, WholeDocumentResponse, max_prompt_chars=limit) == payload
    with pytest.raises(RequestTooLarge):
        payload_for("Scan source", data, WholeDocumentResponse, max_prompt_chars=limit - 1)


def test_catalog_replans_remaining_chunks_when_gateway_reports_a_lower_cap(gateway):
    client, post = gateway
    repo = checkpoint_repo()
    cap = 20_000
    success = completion('{"c":true,"g":[]}')
    accepted = []

    def scan(*args, **kwargs):
        messages = kwargs["json"]["messages"]
        if prompt_character_count(messages) > cap:
            return SimpleNamespace(status_code=400, json=lambda: {
                "detail": {"error": {"code": "prompt_too_large"}, "max_prompt_chars": cap},
            })
        accepted.extend(line for _, line in json.loads(messages[1]["content"])["lines"])
        return success

    post.side_effect = scan
    text = "x" * 65_000
    result = run_catalog_extraction(repo, client, 11, text, "W1", "hash")

    assert "".join(accepted) == text
    assert post.call_count == result.coverage["validated_blocks"] + 1
    assert repo.record_extraction_progress.call_args.args[1]["max_prompt_characters"] == cap
    previous_calls = post.call_count
    resumed = run_catalog_extraction(repo, client, 11, text, "W1", "hash")
    assert resumed == result
    assert post.call_count == previous_calls


def test_catalog_compact_resolves_table_labels_and_only_merges_clear_duplicates(gateway, monkeypatch):
    client, post = gateway
    first = ("Prior work used D10.\n| Abbre. | Dataset |\n| --- | --- |\n"
             "| D10 Smith | HVSMR 2016 |\nAtlas version 1.\nWe used DeepLesion.\n")
    second = ("We used HVSMR 2016 and DeepLesion.\nAtlas version 2.\n| cohort Alpha | cohort Beta |\n"
              "| Abbre. | Dataset |\n| --- | --- |\n| D11 | Beta |\n\n"
              "| Abbre. | Dataset |\n| --- | --- |\n| D11 | Delta |\nD11 was also mentioned.")
    text = first + second
    chunks = [make_chunk(text, start, end, MAX_PROMPT_CHARS, prompt_version=CANDIDATE_PROMPT_VERSION)
              for start, end in [(0, len(first)), (len(first), len(text))]]
    monkeypatch.setattr("datasight.application.catalog_extraction.plan_chunks", lambda *args, **kwargs: chunks)
    post.side_effect = [
        completion('{"c":true,"g":[["D10",false,[1]],["Atlas",false,[5]],["DeepLesion",false,[6]]]}'),
        completion('{"c":true,"g":[["HVSMR 2016",false,[1]],["DeepLesion",false,[1]],'
                   '["Atlas",false,[2]],["Alpha",false,[3]],["Beta",false,[3]],["D11",false,[11]]]}'),
    ]

    result = run_catalog_extraction(checkpoint_repo(), client, 11, text, "W1", "hash", prompt_version=CANDIDATE_PROMPT_VERSION)

    assert result.prompt_version == CANDIDATE_PROMPT_VERSION
    names = [item.display_name for item in result.datasets]
    assert names.count("Atlas") == 2
    assert names.count("DeepLesion") == names.count("HVSMR 2016") == 1
    assert "D10" not in names and "D11" in names
    by_id = {item.evidence_id: item for item in result.evidence}
    hvsmr = next(item for item in result.datasets if item.display_name == "HVSMR 2016")
    assert {by_id[entry.evidence_ids[0]].quote for entry in hvsmr.occurrences} == {
        "Prior work used D10.", "| D10 Smith | HVSMR 2016 |", "We used HVSMR 2016 and DeepLesion.",
    }
    for name in ("Alpha", "Beta"):
        entry = next(item for item in result.datasets if item.display_name == name)
        assert by_id[entry.occurrences[0].evidence_ids[0]].quote == f"cohort {name}"
    assert all(text[item.char_start:item.char_end] == item.quote for item in result.evidence)
