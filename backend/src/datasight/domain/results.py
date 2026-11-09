"""
Result dataclasses for pipeline operations.

These dataclasses represent the outcomes of pipeline operations (download, conversion, rendering)
without coupling to any specific storage backend. Components return these results, and the caller
decides how to persist them (database, DataFrame, JSON, etc.).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from datetime import datetime


@dataclass
class DownloadResult:
    """Result of a PDF download operation."""
    
    paper_id: str
    success: bool
    message: str
    filepath: Optional[Path] = None
    error: Optional[str] = None
    file_size_bytes: int = 0
    download_time: Optional[datetime] = None
    url: Optional[str] = None
    sha256: Optional[str] = None
    source_sha256: Optional[str] = None
    producer_version: str = "pdf-downloader-v2"
    failure_category: Optional[str] = None
    warnings: list[str] | None = None
    quality_metrics: dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Set download_time if not provided."""
        if self.download_time is None:
            self.download_time = datetime.now()


@dataclass
class ConversionResult:
    """Result of a PDF to XML conversion operation."""
    
    paper_id: str
    success: bool
    message: str
    xml_path: Optional[Path] = None
    pdf_path: Optional[Path] = None
    error: Optional[str] = None
    conversion_time: Optional[datetime] = None
    xml_size_bytes: int = 0
    sha256: Optional[str] = None
    source_sha256: Optional[str] = None
    producer_version: str = "grobid-tei-v2"
    failure_category: Optional[str] = None
    warnings: list[str] | None = None
    quality_metrics: dict[str, Any] | None = None
    
    def __post_init__(self):
        """Set conversion_time if not provided."""
        if self.conversion_time is None:
            self.conversion_time = datetime.now()


@dataclass
class RenderResult:
    """Result of a TEI XML to Markdown rendering operation."""
    
    paper_id: str
    xml_path: Path
    md_path: Path
    success: bool
    message: str
    error: Optional[str] = None
    render_time: Optional[datetime] = None
    sections_extracted: int = 0
    references_count: int = 0
    sha256: Optional[str] = None
    source_sha256: Optional[str] = None
    producer_version: str = "tei-renderer-v6"
    profile: str = "pruned"
    warnings: list[str] | None = None
    quality_metrics: dict[str, Any] | None = None
    
    def __post_init__(self):
        """Set render_time if not provided."""
        if self.render_time is None:
            self.render_time = datetime.now()
