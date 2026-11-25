"""OpenAlex API client and work normalization helpers."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from datasight.config import OPENALEX_API_KEY, OPENALEX_API_URL, OPENALEX_MAILTO

logger = logging.getLogger(__name__)

OPENALEX_WORK_SELECT_FIELDS = [
    "id",
    "doi",
    "title",
    "display_name",
    "publication_year",
    "publication_date",
    "ids",
    "language",
    "type",
    "primary_location",
    "best_oa_location",
    "open_access",
    "authorships",
    "topics",
    "keywords",
    "concepts",
    "referenced_works",
    "datasets",
    "related_works",
    "abstract_inverted_index",
    "cited_by_count",
    "is_retracted",
    "has_fulltext",
    "updated_date",
    "created_date",
]


class OpenAlexApiError(RuntimeError):
    """Raised when OpenAlex returns a permanent error or retries are exhausted."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        kind: str = "provider_error",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind


class OpenAlexAuthenticationError(OpenAlexApiError):
    """The configured OpenAlex credential was rejected."""


class OpenAlexBudgetError(OpenAlexApiError):
    """The caller's OpenAlex cost ceiling cannot fund another request."""


class OpenAlexThrottlingError(OpenAlexApiError):
    """OpenAlex continued to throttle the client after retries."""


@dataclass(frozen=True)
class OpenAlexSearchResult:
    """Normalized OpenAlex results plus usage metadata needed by discovery previews."""

    works: list[dict[str, Any]]
    total_count: int
    cost_usd: float
    calls: int
    rate_limit: dict[str, str]
    truncated: bool = False
    next_cursor: str | None = None


class OpenAlexClient:
    """Small synchronous client for the OpenAlex REST API."""

    _REQUEST_DELAY = 0.15
    _MAX_PER_PAGE = 100

    def __init__(
        self,
        api_key: str | None = None,
        mailto: str | None = None,
        base_url: str = OPENALEX_API_URL,
        session: Any | None = None,
        request_delay: float = _REQUEST_DELAY,
    ) -> None:
        self.api_key = api_key if api_key is not None else OPENALEX_API_KEY
        self.mailto = mailto if mailto is not None else OPENALEX_MAILTO
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.request_delay = request_delay
        self.headers = {"User-Agent": "DataSight/0.1 OpenAlex client"}

    def search_works(
        self,
        query: str | None = None,
        limit: int = 100,
        filters: Mapping[str, str | int | bool | Sequence[str | int]] | None = None,
        select: Sequence[str] | None = None,
        sort: str | None = None,
        cursor: str = "*",
    ) -> list[dict[str, Any]]:
        """Search/list works and return DataSight-normalized publication dicts."""
        raw_works = self.list_entities(
            "works",
            query=query,
            limit=limit,
            filters=filters,
            select=select or OPENALEX_WORK_SELECT_FIELDS,
            sort=sort,
            cursor=cursor,
        )
        return [normalize_openalex_work(work) for work in raw_works]

    def search_works_with_meta(
        self,
        query: str | None = None,
        limit: int = 100,
        filters: Mapping[str, str | int | bool | Sequence[str | int]] | None = None,
        select: Sequence[str] | None = None,
        sort: str | None = None,
        cursor: str = "*",
        search_mode: str = "search",
        sample_size: int | None = None,
        sample_seed: int | None = None,
        max_cost_usd: float | None = None,
    ) -> OpenAlexSearchResult:
        """Search works while preserving count, request cost, and rate-limit metadata."""
        raw = self.list_entities_with_meta(
            "works",
            query=query,
            limit=limit,
            filters=filters,
            select=select or OPENALEX_WORK_SELECT_FIELDS,
            sort=sort,
            cursor=cursor,
            search_mode=search_mode,
            sample_size=sample_size,
            sample_seed=sample_seed,
            max_cost_usd=max_cost_usd,
        )
        return OpenAlexSearchResult(
            works=[normalize_openalex_work(work) for work in raw["results"]],
            total_count=int(raw["total_count"]),
            cost_usd=float(raw["cost_usd"]),
            calls=int(raw["calls"]),
            rate_limit=dict(raw["rate_limit"]),
            truncated=bool(raw["truncated"]),
            next_cursor=raw.get("next_cursor"),
        )

    def get_work(
        self,
        work_id: str,
        select: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        data = self.get_entity("works", work_id, select=select or OPENALEX_WORK_SELECT_FIELDS)
        return normalize_openalex_work(data)

    def bulk_get_works_by_doi(
        self,
        dois: Iterable[str],
        select: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        normalized_dois = [_normalize_doi_for_filter(doi) for doi in dois if doi]
        works: list[dict[str, Any]] = []
        for batch in _chunks(normalized_dois, 50):
            works.extend(
                self.search_works(
                    limit=len(batch),
                    filters={"doi": batch},
                    select=select or OPENALEX_WORK_SELECT_FIELDS,
                )
            )
        return works

    def get_entity(
        self,
        entity: str,
        entity_id: str,
        select: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if select:
            params["select"] = ",".join(select)
        return self._request_json("GET", f"/{entity}/{entity_id}", params)

    def list_entities(
        self,
        entity: str,
        query: str | None = None,
        limit: int = 100,
        filters: Mapping[str, str | int | bool | Sequence[str | int]] | None = None,
        select: Sequence[str] | None = None,
        sort: str | None = None,
        cursor: str = "*",
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []

        results: list[dict[str, Any]] = []
        next_cursor: str | None = cursor
        while next_cursor and len(results) < limit:
            per_page = min(self._MAX_PER_PAGE, limit - len(results))
            params: dict[str, Any] = {
                "per_page": per_page,
                "cursor": next_cursor,
            }
            if query:
                params["search"] = query
            if filters:
                params["filter"] = _encode_filters(filters)
            if select:
                params["select"] = ",".join(select)
            if sort:
                params["sort"] = sort

            data = self._request_json("GET", f"/{entity}", params)
            page_results = list(data.get("results") or [])
            results.extend(page_results)
            next_cursor = (data.get("meta") or {}).get("next_cursor")
            if not page_results:
                break
            if len(results) < limit and self.request_delay > 0:
                time.sleep(self.request_delay)

        return results[:limit]

    def list_entities_with_meta(
        self,
        entity: str,
        query: str | None = None,
        limit: int = 100,
        filters: Mapping[str, str | int | bool | Sequence[str | int]] | None = None,
        select: Sequence[str] | None = None,
        sort: str | None = None,
        cursor: str = "*",
        search_mode: str = "search",
        sample_size: int | None = None,
        sample_seed: int | None = None,
        max_cost_usd: float | None = None,
    ) -> dict[str, Any]:
        if limit <= 0:
            return {
                "results": [],
                "total_count": 0,
                "cost_usd": 0.0,
                "calls": 0,
                "rate_limit": {},
                "truncated": False,
                "next_cursor": None,
            }
        if search_mode not in {"search", "search.exact", "search.semantic"}:
            raise ValueError(f"Unsupported OpenAlex search mode: {search_mode}")
        if sample_size is not None and not 1 <= sample_size <= 10_000:
            raise ValueError("OpenAlex sample_size must be between 1 and 10,000.")
        if sample_size is not None and query:
            raise ValueError("OpenAlex random sampling cannot be combined with a search query.")

        results: list[dict[str, Any]] = []
        next_cursor: str | None = cursor
        total_count = 0
        cost_usd = 0.0
        calls = 0
        rate_limit: dict[str, str] = {}
        truncated = False
        sampling = sample_size is not None
        result_limit = min(limit, sample_size) if sample_size is not None else limit
        sample_call_index = 0
        sample_seen_ids: set[str] = set()
        empty_sample_calls = 0
        max_sample_calls = (result_limit + self._MAX_PER_PAGE - 1) // self._MAX_PER_PAGE + 10
        minimum_call_cost = _estimated_call_cost(search_mode if query else None)
        if max_cost_usd is not None and max_cost_usd < minimum_call_cost:
            raise OpenAlexBudgetError(
                "The OpenAlex cost ceiling is too low for this request.",
                kind="budget_exhausted",
            )
        while (sampling or next_cursor) and len(results) < result_limit:
            if sampling and sample_call_index >= max_sample_calls:
                truncated = True
                break
            if calls and max_cost_usd is not None:
                observed_call_cost = cost_usd / calls if calls else minimum_call_cost
                if cost_usd + max(minimum_call_cost, observed_call_cost) > max_cost_usd:
                    truncated = True
                    break
            per_page = min(self._MAX_PER_PAGE, result_limit - len(results))
            params: dict[str, Any] = {"per_page": per_page}
            if sampling:
                params.update(
                    {
                        "sample": per_page,
                        "seed": _derived_sample_seed(sample_seed, sample_call_index),
                    }
                )
                sample_call_index += 1
            else:
                params["cursor"] = next_cursor
            if query:
                params[search_mode] = query
            if filters:
                params["filter"] = _encode_filters(filters)
            if select:
                params["select"] = ",".join(select)
            if sort:
                params["sort"] = sort

            data, headers = self._request_json_with_headers("GET", f"/{entity}", params)
            calls += 1
            page_meta = data.get("meta") or {}
            total_count = max(total_count, int(page_meta.get("count") or 0))
            cost_usd += float(page_meta.get("cost_usd") or 0.0)
            rate_limit = _rate_limit_headers(headers)
            page_results = list(data.get("results") or [])
            if sampling:
                unique_page: list[dict[str, Any]] = []
                for item in page_results:
                    entity_id = str(item.get("id") or json.dumps(item, sort_keys=True))
                    if entity_id in sample_seen_ids:
                        continue
                    sample_seen_ids.add(entity_id)
                    unique_page.append(item)
                results.extend(unique_page)
                empty_sample_calls = empty_sample_calls + 1 if not unique_page else 0
                if empty_sample_calls >= 3:
                    truncated = len(results) < result_limit
                    break
            else:
                results.extend(page_results)
                next_cursor = page_meta.get("next_cursor") if page_results else None
            if max_cost_usd is not None and cost_usd >= max_cost_usd:
                truncated = len(results) < result_limit
                break
            if not page_results:
                if sampling and len(results) < result_limit:
                    truncated = True
                break
            if len(results) < limit and self.request_delay > 0:
                time.sleep(self.request_delay)

        return {
            "results": results[:result_limit],
            "total_count": result_limit if sampling and len(results) >= result_limit else total_count,
            "cost_usd": round(cost_usd, 6),
            "calls": calls,
            "rate_limit": rate_limit,
            "next_cursor": next_cursor if not sampling else None,
            "truncated": truncated or (
                not sampling and bool(next_cursor and len(results) >= limit)
            ),
        }

    def rate_limit_status(self) -> dict[str, Any]:
        """Return safe provider budget data without exposing the configured API key."""
        if not self.api_key:
            return {
                "status": "missing",
                "available": False,
                "remaining": None,
                "limit": None,
                "reset_seconds": None,
                "reset_at": None,
                "message": "Set OPENALEX_API_KEY to preview or launch UM discovery.",
            }
        try:
            data, headers = self._request_json_with_headers("GET", "/rate-limit", {})
        except OpenAlexApiError as exc:
            status = "invalid" if exc.status_code in {401, 403} else "unavailable"
            return {
                "status": status,
                "available": False,
                "remaining": None,
                "limit": None,
                "reset_seconds": None,
                "reset_at": None,
                "message": (
                    "The configured OpenAlex API key was rejected."
                    if status == "invalid"
                    else "OpenAlex readiness could not be checked."
                ),
            }
        rate_value = data.get("rate_limit")
        rate: Mapping[str, Any] = rate_value if isinstance(rate_value, Mapping) else data
        safe_headers = _rate_limit_headers(headers)
        reset_seconds = _number(
            _first_present(rate, "resets_in_seconds", "reset_seconds", "reset")
            if any(key in rate for key in ("resets_in_seconds", "reset_seconds", "reset"))
            else safe_headers.get("reset_seconds")
        )
        reset_at = rate.get("resets_at")
        return {
            "status": "ready",
            "available": True,
            "remaining": _number(
                _first_present(rate, "daily_remaining_usd", "credits_remaining", "remaining")
                if any(key in rate for key in ("daily_remaining_usd", "credits_remaining", "remaining"))
                else safe_headers.get("remaining")
            ),
            "limit": _number(
                _first_present(rate, "daily_budget_usd", "credits_limit", "limit")
                if any(key in rate for key in ("daily_budget_usd", "credits_limit", "limit"))
                else safe_headers.get("limit")
            ),
            "reset_seconds": reset_seconds,
            "reset_at": (
                str(reset_at)
                if reset_at
                else (datetime.now(UTC) + timedelta(seconds=reset_seconds)).isoformat()
                if reset_seconds is not None
                else None
            ),
            "message": "OpenAlex is ready.",
        }

    def _request_json(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        max_retries: int = 5,
    ) -> dict[str, Any]:
        data, _ = self._request_json_with_headers(method, path, params, max_retries)
        return data

    def _request_json_with_headers(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        max_retries: int = 5,
    ) -> tuple[dict[str, Any], Mapping[str, Any]]:
        url = f"{self.base_url}{path}"
        request_params = dict(params or {})
        if self.api_key:
            request_params["api_key"] = self.api_key
        if self.mailto:
            request_params["mailto"] = self.mailto

        backoff = 2.0
        for attempt in range(max_retries):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=request_params,
                    headers=self.headers,
                    timeout=30,
                )
            except requests.exceptions.RequestException as exc:
                if attempt == max_retries - 1:
                    raise OpenAlexApiError(f"OpenAlex network error: {exc}") from exc
                time.sleep(backoff * (2**attempt))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == max_retries - 1:
                    error_type = OpenAlexThrottlingError if response.status_code == 429 else OpenAlexApiError
                    raise error_type(
                        f"OpenAlex transient error {response.status_code}: {response.text[:200]}",
                        status_code=response.status_code,
                        kind="throttled" if response.status_code == 429 else "provider_error",
                    )
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else backoff * (2**attempt)
                logger.warning("OpenAlex returned %s; retrying in %.1fs", response.status_code, wait)
                time.sleep(wait)
                continue

            if 400 <= response.status_code < 500:
                kind = "authentication" if response.status_code in {401, 403} else "client_error"
                error_type = OpenAlexAuthenticationError if response.status_code in {401, 403} else OpenAlexApiError
                raise error_type(
                    f"OpenAlex client error {response.status_code}: {response.text[:300]}",
                    status_code=response.status_code,
                    kind=kind,
                )

            try:
                data = response.json()
            except json.JSONDecodeError as exc:
                raise OpenAlexApiError("OpenAlex returned invalid JSON") from exc
            if not isinstance(data, dict):
                raise OpenAlexApiError("OpenAlex returned a non-object JSON response")
            return data, response.headers

        raise OpenAlexApiError("OpenAlex retries exhausted")


def normalize_openalex_work(work: Mapping[str, Any]) -> dict[str, Any]:
    """Convert an OpenAlex work into the publication shape used by DataSight."""
    raw = dict(work)
    openalex_id = str(raw.get("id") or "")
    primary_location = _dict(raw.get("primary_location"))
    best_oa_location = _dict(raw.get("best_oa_location"))
    open_access = _dict(raw.get("open_access"))
    source = _dict(primary_location.get("source"))

    normalized = {
        "paperId": openalex_work_id(openalex_id),
        "id": openalex_id,
        "doi": _strip_doi_url(raw.get("doi")),
        "title": raw.get("display_name") or raw.get("title"),
        "abstract": reconstruct_abstract(raw.get("abstract_inverted_index")),
        "year": raw.get("publication_year"),
        "publication_date": raw.get("publication_date"),
        "language": raw.get("language"),
        "publication_type": raw.get("type"),
        "url": openalex_id,
        "source_url": openalex_id,
        "open_access_url": _best_pdf_url(best_oa_location, primary_location, open_access),
        "oa_status": open_access.get("oa_status"),
        "cited_by_count": raw.get("cited_by_count"),
        "is_retracted": raw.get("is_retracted"),
        "has_fulltext": raw.get("has_fulltext") or open_access.get("any_repository_has_fulltext"),
        "primary_source_name": source.get("display_name") or primary_location.get("raw_source_name"),
        "raw": raw,
    }
    return normalized


def reconstruct_abstract(abstract_inverted_index: Any) -> str | None:
    if not isinstance(abstract_inverted_index, Mapping) or not abstract_inverted_index:
        return None
    positions: dict[int, str] = {}
    for token, indexes in abstract_inverted_index.items():
        if not isinstance(indexes, list):
            continue
        for index in indexes:
            if isinstance(index, int):
                positions[index] = str(token)
    if not positions:
        return None
    return " ".join(positions[index] for index in sorted(positions))


def openalex_work_id(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).rstrip("/")
    return text.rsplit("/", 1)[-1]


def _encode_filters(filters: Mapping[str, str | int | bool | Sequence[str | int]]) -> str:
    parts = []
    for key, value in filters.items():
        if isinstance(value, bool):
            encoded_value = str(value).lower()
        elif isinstance(value, Sequence) and not isinstance(value, str):
            encoded_value = "|".join(str(item) for item in value if item is not None)
        else:
            encoded_value = str(value)
        if encoded_value:
            parts.append(f"{key}:{encoded_value}")
    return ",".join(parts)


def _best_pdf_url(
    best_oa_location: Mapping[str, Any],
    primary_location: Mapping[str, Any],
    open_access: Mapping[str, Any],
) -> str | None:
    for location in (best_oa_location, primary_location):
        pdf_url = location.get("pdf_url")
        if pdf_url:
            return str(pdf_url)
    oa_url = open_access.get("oa_url")
    if oa_url and str(oa_url).lower().endswith(".pdf"):
        return str(oa_url)
    return None


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _strip_doi_url(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if text.lower().startswith(prefix):
            return text[len(prefix) :]
    return text


def _normalize_doi_for_filter(value: str) -> str:
    return _strip_doi_url(value) or value


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _rate_limit_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    lowered = {str(key).casefold(): str(value) for key, value in headers.items()}
    return {
        key: value
        for key, header in {
            "limit": "x-ratelimit-limit",
            "remaining": "x-ratelimit-remaining",
            "credits_used": "x-ratelimit-credits-used",
            "reset_seconds": "x-ratelimit-reset",
        }.items()
        if (value := lowered.get(header)) is not None
    }


def _number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _estimated_call_cost(search_mode: str | None) -> float:
    if search_mode == "search.semantic":
        return 0.01
    if search_mode:
        return 0.001
    return 0.0001


def _derived_sample_seed(base_seed: int | None, batch_index: int) -> int | None:
    if base_seed is None:
        return None
    return ((base_seed - 1 + batch_index) % 2_147_483_647) + 1


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None
