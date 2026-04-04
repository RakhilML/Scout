"""
Comprehensive test suite for Scout.
Run: py test_suite.py
"""
import sys
import os
import json
import tempfile
import traceback
from pathlib import Path
from datetime import datetime

PASS = "PASS"
FAIL = "FAIL"
results = []

def check(name, fn):
    try:
        fn()
        results.append((PASS, name))
        print(f"  [PASS] {name}")
    except Exception as e:
        results.append((FAIL, name))
        print(f"  [FAIL] {name}")
        print(f"         {e}")
        traceback.print_exc()

# ── 1. Imports (no third-party modules needed) ────────────────────────────────
def test_imports():
    import config
    import models
    import exceptions
    import reporter

check("Core modules import cleanly (config, models, exceptions, reporter)", test_imports)

# ── 2. Config loads from env ──────────────────────────────────────────────────
def test_config():
    from config import load_config
    cfg = load_config()
    assert cfg.lm_studio_base_url, "base_url empty"
    assert cfg.lm_studio_model, "model empty"
    assert cfg.max_results > 0
    assert cfg.fetch_timeout > 0

check("load_config() reads .env correctly", test_config)

# ── 3. LM Studio reachable ────────────────────────────────────────────────────
def test_lm_studio():
    import httpx
    from config import load_config
    cfg = load_config()
    r = httpx.get(f"{cfg.lm_studio_base_url}/models", timeout=10)
    assert r.status_code == 200, f"Got {r.status_code}"
    data = r.json()
    assert "data" in data

check("LM Studio /models endpoint reachable", test_lm_studio)

# ── 4. ScoutReport.to_dict() enum serialization ───────────────────────────────
def test_to_dict_enum():
    from models import ScoutReport, PageContent, FetchStatus
    page = PageContent(
        url="https://example.com",
        title="Example",
        snippet="snippet text",
        full_text="hello world this is some real content to test",
        fetch_status=FetchStatus.SUCCESS,
    )
    report = ScoutReport(
        goal="test goal",
        insights="test insights",
        pages=[page],
        model_used="test-model",
        generated_at=datetime.now(),
        run_duration_seconds=1.23,
        pages_successful=1,
        pages_failed=0,
        structured_insights=None,
        extraction_method="raw",
        confidence="medium",
    )
    d = report.to_dict()
    assert d["pages"][0]["fetch_status"] == "success", f"Got: {d['pages'][0]['fetch_status']}"

check("ScoutReport.to_dict() serializes FetchStatus enum correctly", test_to_dict_enum)

# ── 5. ScoutInsights.to_markdown() ────────────────────────────────────────────
def test_to_markdown():
    from dspy_llm import ScoutInsights, KeyFinding
    si = ScoutInsights(
        summary="Prices are high.",
        key_findings=[KeyFinding(fact="RTX 4090 costs $1599", source_url="https://nvidia.com")],
        sources_used=["https://nvidia.com"],
        confidence="high",
    )
    md = si.to_markdown()
    assert "## Summary" in md
    assert "## Key Findings" in md
    assert "RTX 4090 costs $1599" in md
    assert "## Sources" in md
    assert "*Confidence: high*" in md

check("ScoutInsights.to_markdown() renders all sections", test_to_markdown)

# ── 6. structured_insights in to_dict() ──────────────────────────────────────
def test_structured_in_dict():
    from models import ScoutReport, PageContent, FetchStatus
    from dspy_llm import ScoutInsights, KeyFinding
    si = ScoutInsights(
        summary="Test summary",
        key_findings=[KeyFinding(fact="Fact one", source_url="")],
        sources_used=[],
        confidence="low",
    )
    report = ScoutReport(
        goal="goal",
        insights=si.to_markdown(),
        pages=[],
        model_used="m",
        generated_at=datetime.now(),
        run_duration_seconds=0.5,
        pages_successful=0,
        pages_failed=0,
        structured_insights=si,
        extraction_method="instructor",
        confidence="low",
    )
    d = report.to_dict()
    assert d["extraction_method"] == "instructor"
    assert d["confidence"] == "low"
    assert d.get("structured") is not None
    assert d["structured"]["summary"] == "Test summary"

check("ScoutReport.to_dict() includes structured_insights fields", test_structured_in_dict)

# ── 7. Fetcher handles unreachable URL gracefully ────────────────────────────
def test_fetcher_retry_count():
    from config import load_config
    from fetcher import fetch_page
    from models import FetchStatus
    cfg = load_config()
    result = fetch_page("http://192.0.2.1/nonexistent", cfg, retries=1)
    assert result.fetch_status in (FetchStatus.ERROR, FetchStatus.TIMEOUT, FetchStatus.BLOCKED)
    assert result.url == "http://192.0.2.1/nonexistent"

check("Fetcher handles unreachable URL gracefully", test_fetcher_retry_count)

# ── 8. Fetcher returns TIMEOUT/ERROR status on fast timeout ───────────────────
def test_fetcher_timeout():
    from config import load_config
    from fetcher import fetch_page
    from models import FetchStatus
    import dataclasses
    cfg = load_config()
    fast_cfg = dataclasses.replace(cfg, fetch_timeout=1, fetch_retries=0)
    result = fetch_page("http://10.255.255.1", fast_cfg, retries=0)
    assert result.fetch_status in (FetchStatus.TIMEOUT, FetchStatus.ERROR)

check("Fetcher returns TIMEOUT/ERROR status on unreachable host with short timeout", test_fetcher_timeout)

# ── 9. Blocked domain instant skip ───────────────────────────────────────────
def test_blocked_domain():
    from config import load_config
    from fetcher import fetch_page, BLOCKED_DOMAINS
    from models import FetchStatus
    cfg = load_config()
    blocked = next(iter(BLOCKED_DOMAINS))
    result = fetch_page(f"https://www.{blocked}/some/article", cfg)
    assert result.fetch_status == FetchStatus.BLOCKED, f"Expected BLOCKED, got {result.fetch_status}"

check("Blocked domain returns BLOCKED status instantly", test_blocked_domain)

# ── 10. parse_trigger formats ─────────────────────────────────────────────────
def test_parse_trigger():
    from scheduler import parse_trigger
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.triggers.cron import CronTrigger

    t = parse_trigger("30m")
    assert isinstance(t, IntervalTrigger), f"Expected IntervalTrigger, got {type(t)}"

    t = parse_trigger("2h")
    assert isinstance(t, IntervalTrigger)

    t = parse_trigger("1d")
    assert isinstance(t, IntervalTrigger)

    t = parse_trigger("0 9 * * *")
    assert isinstance(t, CronTrigger)

check("parse_trigger handles 30m / 2h / 1d / cron formats", test_parse_trigger)

# ── 11. Reporter saves md + json ──────────────────────────────────────────────
def test_reporter():
    from models import ScoutReport, PageContent, FetchStatus
    from reporter import save_report
    page = PageContent(
        url="https://example.com",
        title="Example Page",
        snippet="example snippet",
        full_text="Some longer content here to pass the 100 char threshold for has_real_content check in models",
        fetch_status=FetchStatus.SUCCESS,
    )
    report = ScoutReport(
        goal="test reporter output",
        insights="## Summary\n\nThis is a test.",
        pages=[page],
        model_used="test-model",
        generated_at=datetime(2025, 1, 15, 12, 0, 0),
        run_duration_seconds=2.5,
        pages_successful=1,
        pages_failed=0,
        structured_insights=None,
        extraction_method="raw",
        confidence="medium",
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        saved = save_report(report, tmpdir)
        assert saved.md_path.exists(), "Markdown file not created"
        assert saved.json_path.exists(), "JSON file not created"
        md_text = saved.md_path.read_text()
        assert "test reporter output" in md_text
        assert "Raw LLM" in md_text
        json_data = json.loads(saved.json_path.read_text())
        assert json_data["goal"] == "test reporter output"

check("Reporter saves .md and .json with correct content", test_reporter)

# ── 12. FetchStatus counting logic ───────────────────────────────────────────
def test_gather_stats():
    from models import PageContent, FetchStatus

    pages = [
        PageContent(url="https://a.com", title="A", snippet="", full_text="a" * 200, fetch_status=FetchStatus.SUCCESS),
        PageContent(url="https://b.com", title="B", snippet="snip", full_text="", fetch_status=FetchStatus.SNIPPET_ONLY),
        PageContent(url="https://c.com", title="C", snippet="", full_text="", fetch_status=FetchStatus.BLOCKED),
        PageContent(url="https://d.com", title="D", snippet="", full_text="", fetch_status=FetchStatus.TIMEOUT),
        PageContent(url="https://e.com", title="E", snippet="", full_text="", fetch_status=FetchStatus.ERROR),
    ]
    # has_real_content: SUCCESS + len > 100
    real = [p for p in pages if p.has_real_content]
    assert len(real) == 1, f"Expected 1 real content page, got {len(real)}"

    # pages_successful in ScoutReport counts has_real_content
    from models import ScoutReport
    report = ScoutReport(
        goal="g", insights="i", pages=pages, model_used="m",
        generated_at=datetime.now(), run_duration_seconds=1.0,
    )
    assert report.pages_successful == 1, f"pages_successful={report.pages_successful}"
    assert report.pages_failed == 4, f"pages_failed={report.pages_failed}"

check("FetchStatus counting: SUCCESS+len>100 = real, rest = failed", test_gather_stats)

# ── 13. Slug sanitization ─────────────────────────────────────────────────────
def test_slug():
    from reporter import _sanitize_slug
    s = _sanitize_slug("What is the price of RTX 4090??")
    assert " " not in s, "spaces in slug"
    assert "?" not in s, "? in slug"
    assert len(s) <= 55
    s2 = _sanitize_slug("   leading/trailing!!!   ")
    assert not s2.startswith("_")
    assert not s2.endswith("_")

check("_sanitize_slug removes special chars and spaces", test_slug)

# ── 14. _build_web_content ordering ──────────────────────────────────────────
def test_build_web_content():
    from models import PageContent, FetchStatus
    from dspy_llm import _build_web_content

    real = PageContent(
        url="https://real.com", title="Real", snippet="",
        full_text="full text here " * 20,  # >100 chars, has_real_content=True
        fetch_status=FetchStatus.SUCCESS,
    )
    snippet = PageContent(
        url="https://snip.com", title="Snip", snippet="snip text",
        full_text="",
        fetch_status=FetchStatus.SNIPPET_ONLY,
    )

    content = _build_web_content([snippet, real])
    real_pos = content.find("real.com")
    snip_pos = content.find("snip.com")
    assert real_pos < snip_pos, "Real content should come before snippet-only"
    assert "[snippet only]" in content

check("_build_web_content puts real content before snippets", test_build_web_content)

# ── 15. .gitignore coverage ───────────────────────────────────────────────────
def test_gitignore():
    gi = Path(__file__).parent / ".gitignore"
    assert gi.exists(), ".gitignore not found"
    text = gi.read_text()
    required = [".env", "reports/", "__pycache__", ".venv", "*.db"]
    for pat in required:
        assert pat in text, f"Missing pattern: {pat}"

check(".gitignore exists and covers .env / reports / __pycache__ / .venv / *.db", test_gitignore)

# ── Summary ───────────────────────────────────────────────────────────────────
print()
print("=" * 60)
passed = sum(1 for r in results if r[0] == PASS)
failed = sum(1 for r in results if r[0] == FAIL)
print(f"Results: {passed}/{len(results)} passed, {failed} failed")
print("=" * 60)

if failed:
    print("\nFailed tests:")
    for status, name in results:
        if status == FAIL:
            print(f"  - {name}")
    sys.exit(1)
else:
    print("\nAll tests passed.")
    sys.exit(0)
