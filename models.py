"""
Shared data models used across all Scout modules.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


class FetchStatus(str, Enum):
    SUCCESS = "success"
    BLOCKED = "blocked"          # 4xx/5xx from server
    TIMEOUT = "timeout"          # request timed out
    ERROR = "error"              # generic error
    SNIPPET_ONLY = "snippet_only"  # fell back to DDG snippet


@dataclass
class SearchResult:
    """Raw result from DuckDuckGo search."""
    title: str
    url: str
    snippet: str
    rank: int = 0


@dataclass
class PageContent:
    """Fetched and extracted page content."""
    title: str
    url: str
    snippet: str
    full_text: str
    fetch_status: FetchStatus = FetchStatus.SUCCESS
    fetch_error: Optional[str] = None
    char_count: int = 0

    def __post_init__(self):
        self.char_count = len(self.full_text)

    @property
    def display_text(self) -> str:
        """Return full_text if available, else snippet."""
        return self.full_text if self.full_text.strip() else self.snippet

    @property
    def has_real_content(self) -> bool:
        return self.fetch_status == FetchStatus.SUCCESS and len(self.full_text) > 100


@dataclass
class ScoutReport:
    """Final report produced by a scout run."""
    goal: str
    insights: str                        # always set — markdown string
    pages: list[PageContent]
    model_used: str
    generated_at: datetime = field(default_factory=datetime.now)
    run_duration_seconds: float = 0.0
    pages_successful: int = 0
    pages_failed: int = 0
    # Structured insights from DSPy/Instructor — set when structured pipeline succeeds
    structured_insights: Optional[object] = field(default=None, repr=False)  # ScoutInsights | None
    extraction_method: str = "raw"       # "dspy", "instructor", or "raw"
    confidence: str = "medium"           # "high", "medium", "low"

    def __post_init__(self):
        self.pages_successful = sum(1 for p in self.pages if p.has_real_content)
        self.pages_failed = len(self.pages) - self.pages_successful

    def to_dict(self) -> dict:
        d = {
            "goal": self.goal,
            "insights": self.insights,
            "model_used": self.model_used,
            "extraction_method": self.extraction_method,
            "confidence": self.confidence,
            "generated_at": self.generated_at.isoformat(),
            "run_duration_seconds": self.run_duration_seconds,
            "pages_successful": self.pages_successful,
            "pages_failed": self.pages_failed,
            "pages": [
                {
                    "title": p.title,
                    "url": p.url,
                    "fetch_status": p.fetch_status.value,
                    "char_count": p.char_count,
                }
                for p in self.pages
            ],
        }
        # Include structured fields if available
        si = self.structured_insights
        if si is not None:
            d["structured"] = {
                "summary": getattr(si, "summary", ""),
                "key_findings": [
                    {"fact": f.fact, "source_url": f.source_url}
                    for f in getattr(si, "key_findings", [])
                ],
                "sources_used": getattr(si, "sources_used", []),
            }
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


@dataclass
class ScheduledJob:
    """Metadata for a scheduled scout job."""
    job_id: str
    goal: str
    trigger_spec: str       # e.g. "every 6h" or cron string
    output_dir: str
    created_at: str
    next_run: str
    run_count: int = 0
