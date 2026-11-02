"""Focused persistence methods."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import psycopg2.extras

from datasight.infrastructure.persistence.discovery import scope_to_preview_candidates
from datasight.infrastructure.pubfetcher.openalex import openalex_work_id


class PublicationRepositoryMixin:
    conn: Any
    cursor: Any

    def upsert_publications(self, papers: Iterable[dict[str, Any]], source: str) -> int:
        count = 0
        for paper in papers:
            paper_id = paper.get("paperId") or paper.get("id")
            if not paper_id:
                continue
            external_ids = paper.get("externalIds") or {}
            open_access = paper.get("openAccessPdf") or {}
            raw_value = paper.get("raw")
            raw: dict[str, Any] = dict(raw_value) if isinstance(raw_value, dict) else dict(paper)
            doi = paper.get("doi") or external_ids.get("DOI")
            open_access_url = paper.get("open_access_url") or (
                open_access.get("url") if isinstance(open_access, dict) else None
            )
            self.cursor.execute(
                """
                INSERT INTO publications (
                    paper_id, doi, title, abstract, year, source, source_url,
                    open_access_url, publication_date, language, publication_type,
                    oa_status, cited_by_count, is_retracted, has_fulltext,
                    primary_source_name, raw, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (paper_id) DO UPDATE SET
                    doi = EXCLUDED.doi,
                    title = EXCLUDED.title,
                    abstract = EXCLUDED.abstract,
                    year = EXCLUDED.year,
                    source = EXCLUDED.source,
                    source_url = EXCLUDED.source_url,
                    open_access_url = EXCLUDED.open_access_url,
                    publication_date = EXCLUDED.publication_date,
                    language = EXCLUDED.language,
                    publication_type = EXCLUDED.publication_type,
                    oa_status = EXCLUDED.oa_status,
                    cited_by_count = EXCLUDED.cited_by_count,
                    is_retracted = EXCLUDED.is_retracted,
                    has_fulltext = EXCLUDED.has_fulltext,
                    primary_source_name = EXCLUDED.primary_source_name,
                    raw = EXCLUDED.raw,
                    updated_at = now()
                RETURNING id
                """,
                (
                    paper_id,
                    doi,
                    paper.get("title"),
                    paper.get("abstract"),
                    paper.get("year"),
                    source,
                    paper.get("source_url") or paper.get("url"),
                    open_access_url,
                    paper.get("publication_date"),
                    paper.get("language"),
                    paper.get("publication_type"),
                    paper.get("oa_status"),
                    paper.get("cited_by_count"),
                    paper.get("is_retracted"),
                    paper.get("has_fulltext"),
                    paper.get("primary_source_name"),
                    psycopg2.extras.Json(raw),
                ),
            )
            publication = self.cursor.fetchone()
            if publication and source == "openalex":
                self._replace_openalex_child_rows(int(publication["id"]), raw)
            count += 1
        self.conn.commit()
        return count

    def get_papers_needing_download(
        self, limit: int | None = None, pipeline_run_id: int | None = None
    ) -> list[dict[str, Any]]:
        query = """
            SELECT p.paper_id AS "paperId", p.title, p.open_access_url AS url, p.raw
            FROM publications p
            WHERE p.open_access_url IS NOT NULL
              AND p.open_access_url != ''
        """
        params: list[Any] = []
        query, params = scope_to_preview_candidates(query, params, pipeline_run_id)
        query += " ORDER BY p.id"
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        self.cursor.execute(query, params)
        return [dict(row) for row in self.cursor.fetchall()]

    def _replace_openalex_child_rows(self, publication_id: int, raw: dict[str, Any]) -> None:
        for table in (
            "publication_openalex_affiliations",
            "publication_openalex_topics",
            "publication_openalex_keywords",
            "publication_openalex_concepts",
            "publication_openalex_related_works",
        ):
            self.cursor.execute(f"DELETE FROM {table} WHERE publication_id = %s", (publication_id,))
        self._insert_openalex_affiliations(publication_id, raw)
        self._insert_openalex_topics(publication_id, raw)
        self._insert_openalex_keywords(publication_id, raw)
        self._insert_openalex_concepts(publication_id, raw)
        self._insert_openalex_related_works(publication_id, raw)

    def _insert_openalex_affiliations(self, publication_id: int, raw: dict[str, Any]) -> None:
        for auth_index, authorship in enumerate(_rows(raw.get("authorships"))):
            author = _dict(authorship.get("author"))
            institutions = _rows(authorship.get("institutions")) or [{}]
            for institution in institutions:
                self.cursor.execute(
                    """
                    INSERT INTO publication_openalex_affiliations (
                        publication_id, auth_index, openalex_author_id, openalex_author,
                        openalex_orcid, openalex_is_corresponding, openalex_inst_id,
                        openalex_inst_name, openalex_inst_ror, openalex_inst_country_code,
                        openalex_inst_type, raw
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        publication_id,
                        auth_index,
                        author.get("id"),
                        author.get("display_name") or authorship.get("raw_author_name"),
                        author.get("orcid") or authorship.get("raw_orcid"),
                        authorship.get("is_corresponding"),
                        institution.get("id"),
                        institution.get("display_name"),
                        institution.get("ror"),
                        institution.get("country_code"),
                        institution.get("type"),
                        psycopg2.extras.Json({"authorship": authorship, "institution": institution}),
                    ),
                )

    def _insert_openalex_topics(self, publication_id: int, raw: dict[str, Any]) -> None:
        for index, topic in enumerate(_rows(raw.get("topics"))):
            domain = _dict(topic.get("domain"))
            field = _dict(topic.get("field"))
            subfield = _dict(topic.get("subfield"))
            self.cursor.execute(
                """
                INSERT INTO publication_openalex_topics (
                    publication_id, domain, domain_id, field, field_id, subfield,
                    subfield_id, topic, topic_id, score, topic_primary, topic_index, raw
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    publication_id,
                    domain.get("display_name") or topic.get("domain"),
                    _id_tail(domain.get("id") or topic.get("domain_id")),
                    field.get("display_name") or topic.get("field"),
                    _id_tail(field.get("id") or topic.get("field_id")),
                    subfield.get("display_name") or topic.get("subfield"),
                    _id_tail(subfield.get("id") or topic.get("subfield_id")),
                    topic.get("display_name") or topic.get("topic"),
                    _id_tail(topic.get("id") or topic.get("topic_id")),
                    _float(topic.get("score")),
                    bool(topic.get("primary_topic") or topic.get("topic_primary") or index == 0),
                    index,
                    psycopg2.extras.Json(topic),
                ),
            )

    def _insert_openalex_keywords(self, publication_id: int, raw: dict[str, Any]) -> None:
        for keyword in _rows(raw.get("keywords")):
            value = keyword.get("display_name") or keyword.get("keyword")
            if not value:
                continue
            self.cursor.execute(
                """
                INSERT INTO publication_openalex_keywords (publication_id, keyword, score, raw)
                VALUES (%s, %s, %s, %s)
                """,
                (publication_id, value, _float(keyword.get("score")), psycopg2.extras.Json(keyword)),
            )

    def _insert_openalex_concepts(self, publication_id: int, raw: dict[str, Any]) -> None:
        for concept in _rows(raw.get("concepts")):
            value = concept.get("display_name") or concept.get("concept")
            if not value:
                continue
            self.cursor.execute(
                """
                INSERT INTO publication_openalex_concepts (publication_id, level, concept, score, raw)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    publication_id,
                    str(concept.get("level")) if concept.get("level") is not None else None,
                    value,
                    _float(concept.get("score")),
                    psycopg2.extras.Json(concept),
                ),
            )

    def _insert_openalex_related_works(self, publication_id: int, raw: dict[str, Any]) -> None:
        for related in raw.get("related_works") or []:
            related_work_id = openalex_work_id(str(related))
            if not related_work_id:
                continue
            self.cursor.execute(
                """
                INSERT INTO publication_openalex_related_works (publication_id, related_work_id)
                VALUES (%s, %s)
                ON CONFLICT (publication_id, related_work_id) DO NOTHING
                """,
                (publication_id, related_work_id),
            )

    def get_papers_needing_conversion(
        self, limit: int | None = None, pipeline_run_id: int | None = None
    ) -> list[dict[str, Any]]:
        query = """
            SELECT p.paper_id AS "paperId", p.title, pdf.path AS pdf_path
            FROM publications p
            JOIN LATERAL (
                SELECT path
                FROM artifacts
                WHERE publication_id = p.id AND artifact_type = 'pdf'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            ) pdf ON TRUE
            WHERE TRUE
        """
        params: list[Any] = []
        query, params = scope_to_preview_candidates(query, params, pipeline_run_id)
        query += " ORDER BY p.id"
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        self.cursor.execute(query, params)
        return [dict(row) for row in self.cursor.fetchall()]

    def get_papers_needing_rendering(
        self, limit: int | None = None, pipeline_run_id: int | None = None
    ) -> list[dict[str, Any]]:
        query = """
            SELECT p.paper_id AS "paperId", p.title, xml.path AS xml_path
            FROM publications p
            JOIN LATERAL (
                SELECT path
                FROM artifacts
                WHERE publication_id = p.id AND artifact_type = 'tei_xml'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            ) xml ON TRUE
            WHERE TRUE
        """
        params: list[Any] = []
        query, params = scope_to_preview_candidates(query, params, pipeline_run_id)
        query += " ORDER BY p.id"
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        self.cursor.execute(query, params)
        return [dict(row) for row in self.cursor.fetchall()]


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _rows(value: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in value or [] if isinstance(row, dict)]


def _id_tail(value: Any) -> str | None:
    if not value:
        return None
    return str(value).rstrip("/").rsplit("/", 1)[-1]


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
