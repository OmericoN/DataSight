"""Pinned tokenizer for source blocks and request budgeting."""

from functools import lru_cache
import hashlib
from pathlib import Path

from tokenizers import Tokenizer

from datasight import config

TOKENIZER_MODEL = "Qwen/Qwen3.8-27B"
TOKENIZER_REVISION = "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"
TOKENIZER_SHA256 = "0997f410c57a1f4e53b09e4be8f4a172d90edd9564368fb0847030937229b9f3"
TOKENIZER_PATH = Path(__file__).resolve().parents[2] / "assets" / "tokenizer.json"


@lru_cache(maxsize=1)
def _load() -> Tokenizer:
    try:
        if hashlib.sha256(TOKENIZER_PATH.read_bytes()).hexdigest() != TOKENIZER_SHA256:
            raise ValueError("Bundled LLM tokenizer checksum mismatch")
    except FileNotFoundError as exc:
        raise ValueError("Bundled LLM tokenizer is missing") from exc
    return Tokenizer.from_file(str(TOKENIZER_PATH))


def count_tokens(text: str) -> int:
    if config.LLM_MODEL != TOKENIZER_MODEL or config.LLM_MODEL_REVISION != TOKENIZER_REVISION:
        raise ValueError("LLM model or revision differs from the bundled tokenizer")
    return len(_load().encode(text, add_special_tokens=False).ids)
