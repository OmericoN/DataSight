from __future__ import annotations

from pathlib import Path

import pytest
import requests

from datasight.infrastructure.ingestion.downloader import PDFDownloader, PDFLinks, pdf_locations


VALID_PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


class FakeResponse:
    def __init__(
        self,
        content: bytes = VALID_PDF,
        status_code: int = 200,
        content_type: str = "application/pdf",
        stream_error: Exception | None = None,
    ):
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(content))}
        self._stream_error = stream_error
        self.closed = False

    def iter_content(self, chunk_size: int):
        midpoint = max(1, len(self.content) // 2)
        yield self.content[:midpoint]
        if self._stream_error:
            raise self._stream_error
        yield self.content[midpoint:]

    def close(self):
        self.closed = True


def test_accepts_mislabeled_valid_pdf_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    response = FakeResponse(content_type="text/plain")
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response)

    result = PDFDownloader(tmp_path, max_retries=1).download_paper("W1", "https://example.test/file")

    assert result.success
    assert result.sha256
    assert result.warnings and "Mislabeled" in result.warnings[0]
    assert result.filepath and result.filepath.read_bytes() == VALID_PDF
    assert list(tmp_path.glob("*.part")) == []
    assert response.closed


@pytest.mark.parametrize(
    ("content", "category"),
    [
        (b"<html><body>error</body></html>", "html_body"),
        (b"%PDF-1.7\nincomplete", "truncated_pdf"),
    ],
)
def test_rejects_html_and_truncated_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
    category: str,
):
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: FakeResponse(content))
    result = PDFDownloader(tmp_path, max_retries=1).download_paper("W2", "https://example.test/file")
    assert not result.success
    assert result.failure_category == category
    assert not (tmp_path / "W2.pdf").exists()
    assert list(tmp_path.glob("*.part")) == []


def test_rejects_oversized_stream_and_cleans_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    response = FakeResponse(VALID_PDF)
    response.headers.pop("Content-Length")
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response)
    result = PDFDownloader(tmp_path, max_bytes=10, max_retries=1).download_paper("W3", "https://example.test/file")
    assert result.failure_category == "oversized"
    assert list(tmp_path.iterdir()) == []


def test_retries_timeout_and_retryable_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    events: list[object] = [requests.Timeout("slow"), FakeResponse(status_code=429), FakeResponse()]

    def fake_get(*args, **kwargs):
        event = events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event

    monkeypatch.setattr(requests, "get", fake_get)
    result = PDFDownloader(tmp_path, max_retries=3, backoff=0).download_paper("W4", "https://example.test/file")
    assert result.success
    assert result.quality_metrics and result.quality_metrics["attempts"] == 3


def test_interrupted_stream_does_not_publish_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(stream_error=requests.ConnectionError("cut")),
    )
    result = PDFDownloader(tmp_path, max_retries=1).download_paper("W5", "https://example.test/file")
    assert not result.success
    assert result.failure_category == "timeout_or_connection"
    assert list(tmp_path.iterdir()) == []


def test_corrupt_cache_is_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cached = tmp_path / "W6.pdf"
    cached.write_bytes(b"not a pdf")
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: FakeResponse())
    result = PDFDownloader(tmp_path, max_retries=1).download_paper("W6", "https://example.test/file")
    assert result.success
    assert cached.read_bytes() == VALID_PDF


def test_alternative_pdf_after_bibliography_is_not_retried(tmp_path, monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return FakeResponse(b'@article{citation}', content_type='text/plain') if url.endswith('.bib') else FakeResponse()
    monkeypatch.setattr(requests, 'get', get)
    result = PDFDownloader(tmp_path).download_paper('W1', 'https://example.test/a.bib',
               alternative_urls=['https://example.test/a.bib', 'https://example.test/a.pdf'])
    assert result.success
    assert calls == ['https://example.test/a.bib', 'https://example.test/a.pdf']
    assert result.quality_metrics['attempts'] == 2


def test_access_block_is_explicit_and_not_retried(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(requests, 'get', lambda *args, **kwargs: (calls.append(1) or FakeResponse(status_code=403)))
    result = PDFDownloader(tmp_path).download_paper('W1', 'https://example.test/a')
    assert result.failure_category == 'access_blocked' and len(calls) == 1


def test_landing_uses_explicit_pdf_link(tmp_path, monkeypatch):
    def get(url, **kwargs):
        return FakeResponse() if url.endswith('.pdf') else FakeResponse(b'<html><meta name="citation_pdf_url" content="/a.pdf"></html>')
    monkeypatch.setattr(requests, 'get', get)
    assert PDFDownloader(tmp_path).download_paper('W1', 'https://example.test/page').success


def test_long_retry_after_is_deferred(tmp_path, monkeypatch):
    response = FakeResponse(status_code=429)
    response.headers['Retry-After'] = '120'
    monkeypatch.setattr(requests, 'get', lambda *args, **kwargs: response)
    monkeypatch.setattr('time.sleep', lambda seconds: pytest.fail('Must not wait'))
    result = PDFDownloader(tmp_path).download_paper('W1', 'https://example.test/a.pdf')
    assert result.failure_category == 'retry_deferred'
    assert result.quality_metrics['attempts'] == 1


def test_access_challenge_does_not_follow_links(tmp_path, monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return FakeResponse(b'<html>Verify you are human<a href="/a.pdf">PDF</a></html>')
    monkeypatch.setattr(requests, 'get', get)
    result = PDFDownloader(tmp_path).download_paper('W1', 'https://example.test/a')
    assert result.failure_category == 'access_blocked' and len(calls) == 1


def test_locations_keep_preferred_then_pdf_then_landing_order():
    assert pdf_locations('https://x/a.pdf', {'best_oa_location': {'pdf_url': 'https://x/a.pdf', 'landing_page_url': 'https://x/page'},
                         'locations': [{'pdf_url': 'https://x/b.pdf'}]}) == ['https://x/a.pdf','https://x/b.pdf','https://x/page']


def test_locations_ignore_malformed_provider_values():
    assert pdf_locations(None, {'best_oa_location': {'pdf_url': 123},
                                'locations': [{'pdf_url': 'https://x/a.pdf'}, {'landing_page_url': None}]}) == ['https://x/a.pdf']
    assert pdf_locations('https://x/a.pdf', {'locations': 123}) == ['https://x/a.pdf']
    links = PDFLinks()
    links.feed('<meta name><a href><meta name="citation_pdf_url" content="https://x/a.pdf">')
    assert links.urls == ['https://x/a.pdf']


def test_truncated_pdf_is_not_retried_and_attempts_are_capped(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(requests, 'get', lambda *args, **kwargs: (calls.append(1) or FakeResponse(b'%PDF-1.7\nincomplete')))
    downloader = PDFDownloader(tmp_path, max_retries=99)
    assert downloader.max_retries == 3
    result = downloader.download_paper('W1', 'https://example.test/a.pdf')
    assert result.failure_category == 'truncated_pdf'
    assert len(calls) == result.quality_metrics['attempts'] == 1


def test_failed_explicit_links_count_every_attempt(tmp_path, monkeypatch):
    def get(url, **kwargs):
        return FakeResponse(status_code=403) if url.endswith('.pdf') else FakeResponse(b'<html><a href="/a.pdf">PDF</a><a href="/b.pdf">PDF</a></html>')
    monkeypatch.setattr(requests, 'get', get)
    result = PDFDownloader(tmp_path).download_paper('W1', 'https://example.test/page')
    assert result.quality_metrics['attempts'] == 3
