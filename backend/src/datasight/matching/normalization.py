"""Normalization helpers shared by candidate detection and UM matching."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote, urlsplit


_DOI_PREFIX = re.compile(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", re.I)
_DOI = re.compile(r"10\.\d+/\S+", re.I)
_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s./:-]+", re.UNICODE)


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = _PUNCT_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def normalize_identifier(value: str | None) -> str:
    if not value:
        return ""
    identifier = value.strip()
    identifier = _DOI_PREFIX.sub("", identifier).strip()
    return identifier.rstrip(".").casefold()


def catalog_link_key(value: str | None) -> str:
    """Compare resolver links with bare identifiers and preserve case-sensitive URL components."""
    if not value or not (literal := value.strip()):
        return ""
    try:
        parts = urlsplit(literal)
        host = parts.hostname
    except ValueError:
        return ""
    if parts.scheme.lower() in {"http", "https"}:
        if not host or parts.username is not None:
            return ""
        if parts.netloc.lower() in {"doi.org", "dx.doi.org"}:
            doi = unquote(parts.path.removeprefix("/"))
            return "doi:" + normalize_identifier(doi) if _DOI.fullmatch(doi) else ""
        if parts.netloc.lower() == "hdl.handle.net":
            literal = parts.path.lstrip("/")
        else:
            if parts.path in {"", "/"} and not parts.query:
                return ""  # A repository homepage cannot identify a dataset.
            return parts._replace(netloc=parts.netloc.lower()).geturl()
    doi = normalize_identifier(literal)
    if _DOI.fullmatch(doi):
        return "doi:" + doi
    handle = re.fullmatch(r"(?:hdl:\s*)?(\d[\d.]*/\S+)", literal, re.I)
    return "hdl:" + handle[1] if handle else ""
