from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datasight.infrastructure.pubfetcher.openalex import (
    OpenAlexClient,
    normalize_openalex_work,
    reconstruct_abstract,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, params=None, headers=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return self.responses.pop(0)


def test_reconstruct_abstract_from_inverted_index():
    assert reconstruct_abstract({"Data": [0], "reuse": [1], "matters": [2]}) == "Data reuse matters"


def test_normalize_openalex_work_prefers_best_oa_pdf_and_bare_work_id():
    publication = normalize_openalex_work(
        {
            "id": "https://openalex.org/W2527848138",
            "doi": "https://doi.org/10.123/example",
            "display_name": "Example work",
            "publication_year": 2024,
            "best_oa_location": {"pdf_url": "https://repo.example/work.pdf"},
            "primary_location": {
                "pdf_url": "https://publisher.example/work.pdf",
                "source": {"display_name": "Journal"},
            },
            "open_access": {"oa_status": "gold", "oa_url": "https://repo.example/work"},
            "abstract_inverted_index": {"Hello": [0], "world": [1]},
        }
    )

    assert publication["paperId"] == "W2527848138"
    assert publication["doi"] == "10.123/example"
    assert publication["open_access_url"] == "https://repo.example/work.pdf"
    assert publication["primary_source_name"] == "Journal"
    assert publication["abstract"] == "Hello world"


def test_search_works_uses_cursor_pagination_and_query_options():
    session = FakeSession(
        [
            FakeResponse(
                payload={
                    "meta": {"next_cursor": "next"},
                    "results": [{"id": "https://openalex.org/W1", "display_name": "One"}],
                }
            ),
            FakeResponse(
                payload={
                    "meta": {"next_cursor": None},
                    "results": [{"id": "https://openalex.org/W2", "display_name": "Two"}],
                }
            ),
        ]
    )
    client = OpenAlexClient(api_key="key", mailto="ops@example.org", session=session, request_delay=0)

    works = client.search_works(
        query="dataset reuse",
        limit=2,
        filters={"open_access.is_oa": True, "topics.id": ["T1", "T2"]},
        select=["id", "display_name"],
        sort="relevance_score:desc",
    )

    assert [work["paperId"] for work in works] == ["W1", "W2"]
    first_params = session.calls[0]["params"]
    assert first_params["search"] == "dataset reuse"
    assert first_params["filter"] == "open_access.is_oa:true,topics.id:T1|T2"
    assert first_params["select"] == "id,display_name"
    assert first_params["api_key"] == "key"
    assert first_params["mailto"] == "ops@example.org"
    assert session.calls[1]["params"]["cursor"] == "next"


def test_bulk_get_works_by_doi_uses_doi_filter():
    session = FakeSession(
        [
            FakeResponse(
                payload={
                    "meta": {"next_cursor": None},
                    "results": [{"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/a"}],
                }
            )
        ]
    )
    client = OpenAlexClient(api_key="", mailto="", session=session, request_delay=0)

    works = client.bulk_get_works_by_doi(["https://doi.org/10.1/a"])

    assert works[0]["paperId"] == "W1"
    assert session.calls[0]["params"]["filter"] == "doi:10.1/a"


def test_transient_errors_are_retried(monkeypatch):
    monkeypatch.setattr("datasight.infrastructure.pubfetcher.openalex.time.sleep", lambda _: None)
    session = FakeSession(
        [
            FakeResponse(status_code=429, text="rate limited"),
            FakeResponse(payload={"id": "https://openalex.org/W1"}),
        ]
    )
    client = OpenAlexClient(api_key="", mailto="", session=session, request_delay=0)

    work = client.get_work("W1")

    assert work["paperId"] == "W1"
    assert len(session.calls) == 2


def test_search_with_meta_cursor_paginates_and_reports_cost_and_rate_limit():
    session = FakeSession(
        [
            FakeResponse(
                payload={
                    "meta": {"count": 250, "cost_usd": 0.001, "next_cursor": "page-2"},
                    "results": [{"id": f"https://openalex.org/W{i}"} for i in range(100)],
                },
                headers={"X-RateLimit-Remaining": "975"},
            ),
            FakeResponse(
                payload={
                    "meta": {"count": 250, "cost_usd": 0.001, "next_cursor": None},
                    "results": [{"id": "https://openalex.org/W101"}],
                },
                headers={"X-RateLimit-Remaining": "974"},
            ),
        ]
    )
    client = OpenAlexClient(api_key="key", mailto="", session=session, request_delay=0)

    result = client.search_works_with_meta(
        query='"10.1234/example"',
        limit=101,
        search_mode="search.exact",
        max_cost_usd=0.01,
    )

    assert len(result.works) == 101
    assert result.total_count == 250
    assert result.calls == 2
    assert result.cost_usd == 0.002
    assert result.rate_limit["remaining"] == "974"
    assert session.calls[0]["params"]["search.exact"] == '"10.1234/example"'
    assert session.calls[1]["params"]["cursor"] == "page-2"


def test_seeded_random_sample_uses_deterministic_deduplicated_batches():
    session = FakeSession(
        [
            FakeResponse(
                payload={
                    "meta": {"count": 125, "cost_usd": 0.0001},
                    "results": [{"id": f"https://openalex.org/W{i}"} for i in range(100)],
                }
            ),
            FakeResponse(
                payload={
                    "meta": {"count": 125, "cost_usd": 0.0001},
                    "results": [{"id": f"https://openalex.org/W{i}"} for i in range(100, 125)],
                }
            ),
        ]
    )
    client = OpenAlexClient(api_key="key", mailto="", session=session, request_delay=0)

    result = client.search_works_with_meta(
        limit=125,
        filters={"type": ["article"]},
        sample_size=125,
        sample_seed=42,
        max_cost_usd=0.01,
    )

    assert len(result.works) == 125
    assert result.calls == 2
    assert result.truncated is False
    assert session.calls[0]["params"]["sample"] == 100
    assert session.calls[0]["params"]["seed"] == 42
    assert "cursor" not in session.calls[0]["params"]
    assert "page" not in session.calls[0]["params"]
    assert session.calls[1]["params"]["sample"] == 25
    assert session.calls[1]["params"]["seed"] == 43


def test_seeded_random_sample_refills_cross_batch_duplicates():
    session = FakeSession(
        [
            FakeResponse(payload={"meta": {"cost_usd": 0.0001}, "results": [{"id": f"https://openalex.org/W{i}"} for i in range(100)]}),
            FakeResponse(payload={"meta": {"cost_usd": 0.0001}, "results": [{"id": f"https://openalex.org/W{i}"} for i in range(20)]}),
            FakeResponse(payload={"meta": {"cost_usd": 0.0001}, "results": [{"id": f"https://openalex.org/W{i}"} for i in range(100, 125)]}),
        ]
    )
    client = OpenAlexClient(api_key="key", mailto="", session=session, request_delay=0)

    result = client.search_works_with_meta(
        limit=125,
        sample_size=125,
        sample_seed=42,
        max_cost_usd=0.01,
    )

    assert len(result.works) == 125
    assert result.calls == 3
    assert session.calls[2]["params"]["sample"] == 25
    assert session.calls[2]["params"]["seed"] == 44


def test_search_with_meta_stops_before_next_page_would_exceed_budget():
    session = FakeSession(
        [
            FakeResponse(
                payload={
                    "meta": {"count": 500, "cost_usd": 0.001, "next_cursor": "page-2"},
                    "results": [{"id": f"https://openalex.org/W{i}"} for i in range(100)],
                }
            )
        ]
    )
    client = OpenAlexClient(api_key="key", mailto="", session=session, request_delay=0)

    result = client.search_works_with_meta(query="reuse", limit=500, max_cost_usd=0.0015)

    assert result.calls == 1
    assert result.cost_usd == 0.001
    assert result.truncated is True
    assert result.next_cursor == "page-2"


def test_rate_limit_status_returns_safe_daily_budget_fields():
    session = FakeSession(
        [
            FakeResponse(
                payload={
                    "api_key": "secret-value",
                    "rate_limit": {
                        "daily_budget_usd": 1,
                        "daily_remaining_usd": 0.84,
                        "resets_at": "2026-08-05T00:00:00Z",
                        "resets_in_seconds": 3600,
                    },
                }
            )
        ]
    )
    client = OpenAlexClient(api_key="secret-value", mailto="", session=session, request_delay=0)

    status = client.rate_limit_status()

    assert status["status"] == "ready"
    assert status["remaining"] == 0.84
    assert status["limit"] == 1
    assert status["reset_at"] == "2026-08-05T00:00:00Z"
    assert "api_key" not in status


def test_rate_limit_status_marks_rejected_key_invalid():
    session = FakeSession([FakeResponse(status_code=401, text="Invalid API key")])
    client = OpenAlexClient(api_key="bad-key", mailto="", session=session, request_delay=0)

    status = client.rate_limit_status()

    assert status["status"] == "invalid"
    assert status["available"] is False
