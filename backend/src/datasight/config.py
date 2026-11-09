"""Global configuration for DataSight."""

import os
import hashlib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

# Load .env from the monorepo root.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
env_path = PROJECT_ROOT / ".env"
load_dotenv(dotenv_path=env_path)
load_dotenv()

try:
    APP_VERSION = version("datasight")
except PackageNotFoundError:
    APP_VERSION = "0.1.0"


def _source_code_version() -> str:
    digest = hashlib.sha256()
    source_root = PROJECT_ROOT / "backend" / "src" / "datasight"
    files = sorted(source_root.rglob("*.py")) if source_root.exists() else []
    if not files:
        return APP_VERSION
    for source in files:
        digest.update(source.relative_to(source_root).as_posix().encode("utf-8"))
        digest.update(source.read_bytes())
    return f"source-{digest.hexdigest()[:16]}"


CODE_VERSION = os.getenv("DATASIGHT_CODE_VERSION") or _source_code_version()

# LLM inference remains server-side; these values must never enter run configs.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "Qwen/Qwen3.8-27B")
LLM_MODEL_REVISION = os.getenv("LLM_MODEL_REVISION", "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0")
LLM_TIMEOUT_SEC = float(os.getenv("LLM_TIMEOUT_SEC", "195"))
LLM_INFERENCE_CONCURRENCY = max(1, int(os.getenv("LLM_INFERENCE_CONCURRENCY", "4")))
EXTRACTIONS_DIR = PROJECT_ROOT / "storage" / "extractions"

# ── OpenAlex ──────────────────────────────────────────────────────────────────
OPENALEX_API_KEY = os.getenv("OPENALEX_API_KEY", "")
OPENALEX_API_URL = os.getenv("OPENALEX_API_URL", "https://api.openalex.org")
OPENALEX_MAILTO = os.getenv("OPENALEX_MAILTO", "")

# ── PostgreSQL ─────────────────────────────────────────────────────────────────
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_DB = os.getenv("POSTGRES_DB", "datasight_pipeline")
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")

POSTGRES_DSN = (
    f"host={POSTGRES_HOST} "
    f"port={POSTGRES_PORT} "
    f"dbname={POSTGRES_DB} "
    f"user={POSTGRES_USER} "
    f"password={POSTGRES_PASSWORD}"
)

POSTGRES_URI = (
    "postgresql+psycopg2://"
    f"{quote(POSTGRES_USER, safe='')}:{quote(POSTGRES_PASSWORD, safe='')}"
    f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{quote(POSTGRES_DB, safe='')}"
)

# ── Queue Runtime (free/open-source: Celery + Redis) ──────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)
CELERY_TASK_ALWAYS_EAGER = os.getenv("CELERY_TASK_ALWAYS_EAGER", "false").lower() in {
    "1",
    "true",
    "yes",
}
HIGH_THROUGHPUT_STAGE_BATCH_SIZE = int(
    os.getenv("HIGH_THROUGHPUT_STAGE_BATCH_SIZE", "4")
)
HIGH_THROUGHPUT_MAX_BATCHES_PER_DISPATCH = int(
    os.getenv("HIGH_THROUGHPUT_MAX_BATCHES_PER_DISPATCH", "8")
)

# ── Paths ──────────────────────────────────────────────────────────────────────
STORAGE_DIR = PROJECT_ROOT / "storage"
DEFAULT_UM_DATASETS_PATH = os.getenv("UM_DATASETS_PATH", "data/um_dataset")

# Source/reference inputs live in data/. Generated runtime files live in storage/.
PDF_DIR = STORAGE_DIR / "pdf"
XML_DIR = STORAGE_DIR / "xml"
MARKDOWN_DIR = STORAGE_DIR / "markdown"
EXPORTS_DIR = STORAGE_DIR / "exports"
LOGS_DIR = STORAGE_DIR / "logs"

# ── Pipeline Settings ──────────────────────────────────────────────────────────

# PDF Downloader
DOWNLOAD_TIMEOUT_SEC = 60  # HTTP request timeout for downloading PDFs
DOWNLOAD_CHUNK_SIZE_BYTES = 8192  # Chunk size for streaming downloads
DOWNLOAD_DELAY_SEC = 0.5  # Delay between downloads to avoid rate limiting
DOWNLOAD_MAX_BYTES = int(os.getenv("DOWNLOAD_MAX_BYTES", str(100 * 1024 * 1024)))
DOWNLOAD_MAX_RETRIES = int(os.getenv("DOWNLOAD_MAX_RETRIES", "3"))
DOWNLOAD_BACKOFF_SEC = float(os.getenv("DOWNLOAD_BACKOFF_SEC", "1"))

# GROBID Converter
GROBID_BASE_URL = os.getenv("GROBID_BASE_URL", "http://localhost:8070")
GROBID_STARTUP_TIMEOUT_SEC = 30  # Wait time for GROBID server to start
GROBID_ALIVE_CHECK_TIMEOUT_SEC = 2  # Timeout for /api/isalive endpoint
GROBID_CONVERSION_TIMEOUT_SEC = 300  # Timeout for PDF→XML conversion
CONVERSION_DELAY_SEC = 0.1  # Delay between conversions

# Create generated runtime directories on import
for _dir in (PDF_DIR, XML_DIR, MARKDOWN_DIR, EXPORTS_DIR, LOGS_DIR):
    _dir.mkdir(parents=True, exist_ok=True)
