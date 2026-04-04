"""
DSPy-based LLM programming for Scout.

Instead of hand-crafting prompts (fragile, model-specific), we define
typed signatures and let DSPy manage the prompt construction, chain-of-thought
reasoning, and structured output — making the LLM *programmed*, not just prompted.

Architecture:
  - ExtractInsights    : DSPy Signature (what inputs/outputs we want)
  - ScoutModule        : DSPy Module using ChainOfThought (reasoning before answering)
  - InstructorClient   : Instructor wrapper for guaranteed structured Pydantic output
  - extract_insights_dspy : top-level function that tries DSPy → Instructor → raw fallback

Why three layers?
  DSPy ChainOfThought  → best quality, self-reasoning, but output is free text fields
  Instructor MD_JSON   → forces Pydantic-typed structured output from same model
  Raw fallback (llm.py) → last resort if both fail (e.g. very small models)
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import dspy
import instructor
from openai import OpenAI
from pydantic import BaseModel, Field

from config import ScoutConfig
from exceptions import LLMError, LLMConnectionError, LLMEmptyResponseError
from models import PageContent

logger = logging.getLogger("scout.dspy_llm")

# ─── PYDANTIC OUTPUT MODELS ───────────────────────────────────────────────────

class KeyFinding(BaseModel):
    """A single key finding extracted from web content."""
    fact: str = Field(description="The key fact, stat, price, or finding. Be specific — include numbers, dates, names.")
    source_url: str = Field(default="", description="URL this finding came from, or empty string if unknown")


class ScoutInsights(BaseModel):
    """Structured output from a Scout research run."""
    summary: str = Field(description="One concise paragraph directly answering the research goal")
    key_findings: list[KeyFinding] = Field(
        description="List of the most important, specific findings. Max 8. Only facts from the sources.",
        max_length=8,
    )
    sources_used: list[str] = Field(
        description="List of source URLs that contributed useful information",
        max_length=10,
    )
    confidence: str = Field(
        default="medium",
        description="How confident: 'high' (multiple sources agree), 'medium' (some data), 'low' (snippets only)"
    )

    def to_markdown(self) -> str:
        """Render structured insights as clean markdown."""
        lines = [
            f"## Summary\n\n{self.summary}",
            "",
            "## Key Findings\n",
        ]
        for f in self.key_findings:
            source_note = f" ([source]({f.source_url}))" if f.source_url else ""
            lines.append(f"- {f.fact}{source_note}")

        lines += [
            "",
            "## Sources\n",
        ]
        for url in self.sources_used:
            lines.append(f"- {url}")

        lines += [
            "",
            f"*Confidence: {self.confidence}*",
        ]
        return "\n".join(lines)


# ─── DSPY SIGNATURES ─────────────────────────────────────────────────────────

class ExtractInsights(dspy.Signature):
    """
    You are a precise research analyst. Your job is to extract ONLY what is
    directly relevant to the user's research goal from the provided web content.

    Rules:
    - Only report facts present in the sources — do NOT hallucinate
    - Include specific numbers, prices, dates, names wherever available
    - Ignore ads, navigation, cookie banners, and unrelated content
    - Be concise and direct
    """
    goal: str = dspy.InputField(desc="The user's research goal")
    web_content: str = dspy.InputField(desc="Raw scraped web content from multiple sources, each labeled with its URL")
    summary: str = dspy.OutputField(desc="One paragraph directly answering the goal. Be specific.")
    key_findings: str = dspy.OutputField(desc="Bullet points of most important facts. Include numbers, prices, dates. Cite source URLs inline.")
    sources: str = dspy.OutputField(desc="Comma-separated list of source URLs that provided useful information")
    confidence: str = dspy.OutputField(desc="One word: high, medium, or low — based on how much real content was available")


class ExtractInsightsSimple(dspy.Signature):
    """
    Extract relevant information from web content based on a research goal.
    Be factual, concise, and only use information present in the sources.
    """
    goal: str = dspy.InputField()
    web_content: str = dspy.InputField()
    answer: str = dspy.OutputField(desc="Markdown response with Summary, Key Findings, and Sources sections")


# ─── DSPY MODULE ─────────────────────────────────────────────────────────────

class ScoutModule(dspy.Module):
    """
    Main DSPy module for Scout.

    Uses Predict (not ChainOfThought) because LM Studio's OpenAI-compatible
    API rejects the response_format field that DSPy's structured output mode
    sends. The models are already reasoning models (gpt-oss, gemma) so they
    reason internally without needing an explicit CoT wrapper.
    """

    def __init__(self):
        super().__init__()
        self.extract = dspy.Predict(ExtractInsights)

    def forward(self, goal: str, web_content: str) -> dspy.Prediction:
        return self.extract(goal=goal, web_content=web_content)


# ─── CONTENT BUILDER ─────────────────────────────────────────────────────────

def _build_web_content(pages: list[PageContent]) -> str:
    """
    Build the web_content string passed to DSPy.
    Real pages first, then snippet-only pages.
    Each section clearly labeled with its URL so the model can cite sources.
    """
    real = [p for p in pages if p.has_real_content]
    snippets = [p for p in pages if not p.has_real_content]

    parts = []
    for i, page in enumerate(real + snippets, 1):
        quality = "" if page.has_real_content else " [snippet only]"
        parts.append(
            f"--- Source {i}{quality}: {page.title} ---\n"
            f"URL: {page.url}\n\n"
            f"{page.display_text}\n"
        )

    return "\n".join(parts) if parts else "(No web content retrieved.)"


# ─── DSPy SETUP ───────────────────────────────────────────────────────────────

def _configure_dspy(cfg: ScoutConfig) -> None:
    """Configure DSPy with the LM Studio endpoint."""
    # DSPy's LM uses litellm under the hood.
    # For OpenAI-compatible endpoints: prefix model with "openai/"
    model_id = cfg.lm_studio_model
    if not model_id.startswith("openai/"):
        model_id = f"openai/{model_id}"

    lm = dspy.LM(
        model=model_id,
        api_base=cfg.lm_studio_base_url,
        api_key=cfg.lm_studio_api_key,
        max_tokens=-1,
        temperature=0.3,
        cache=False,
    )
    # Disable structured output mode — LM Studio rejects response_format from DSPy
    dspy.configure(lm=lm, experimental=False)
    logger.debug("DSPy configured with model: %s @ %s", model_id, cfg.lm_studio_base_url)


# ─── INSTRUCTOR STRUCTURED EXTRACTION ────────────────────────────────────────

def _extract_with_instructor(
    goal: str,
    pages: list[PageContent],
    cfg: ScoutConfig,
) -> ScoutInsights:
    """
    Use Instructor (MD_JSON mode) to extract guaranteed Pydantic-typed output.

    MD_JSON mode asks the model to output a ```json ... ``` block which
    Instructor parses and validates against ScoutInsights. Works with
    LM Studio's OpenAI-compatible API (doesn't need response_format support).
    """
    client = instructor.from_openai(
        OpenAI(
            base_url=cfg.lm_studio_base_url,
            api_key=cfg.lm_studio_api_key,
        ),
        mode=instructor.Mode.MD_JSON,
    )

    web_content = _build_web_content(pages)

    system = (
        "You are a research analyst. Extract structured information from web content "
        "based on the user's research goal. Only use facts present in the sources. "
        "Do not hallucinate. Be specific — include numbers, prices, dates, names."
    )

    user = (
        f"Research goal: {goal}\n\n"
        f"Web content:\n{web_content}"
    )

    result: ScoutInsights = client.chat.completions.create(
        model=cfg.lm_studio_model,
        response_model=ScoutInsights,
        max_tokens=-1,
        max_retries=2,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )

    logger.info("Instructor extracted %d findings", len(result.key_findings))
    return result


# ─── DSPY EXTRACTION ─────────────────────────────────────────────────────────

def _extract_with_dspy(
    goal: str,
    pages: list[PageContent],
    cfg: ScoutConfig,
) -> ScoutInsights:
    """
    Use DSPy ChainOfThought to extract insights, then parse into ScoutInsights.

    DSPy gives us the best *reasoning* (ChainOfThought makes the model think
    before answering), but output is free-text fields. We parse them into
    ScoutInsights after the fact.
    """
    _configure_dspy(cfg)
    module = ScoutModule(use_chain_of_thought=True)
    web_content = _build_web_content(pages)

    prediction = module(goal=goal, web_content=web_content)

    # Parse free-text fields into ScoutInsights
    findings = _parse_bullet_findings(prediction.key_findings, pages)
    sources = _parse_sources(prediction.sources, pages)
    confidence = prediction.confidence.strip().lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"

    return ScoutInsights(
        summary=prediction.summary.strip(),
        key_findings=findings,
        sources_used=sources,
        confidence=confidence,
    )


def _parse_bullet_findings(text: str, pages: list[PageContent]) -> list[KeyFinding]:
    """Parse bullet-point findings text into KeyFinding objects."""
    findings = []
    known_urls = {p.url for p in pages}

    for line in text.splitlines():
        line = line.strip().lstrip("-•*").strip()
        if not line:
            continue

        # Try to extract an inline URL from the line
        url_match = re.search(r'https?://[^\s\)]+', line)
        url = ""
        if url_match:
            candidate = url_match.group(0).rstrip(".,)")
            # Prefer known source URLs over any random URL in text
            url = candidate if candidate in known_urls else candidate

        # Clean the fact text
        fact = re.sub(r'\(?https?://[^\s\)]+\)?', '', line).strip().rstrip("()")
        if fact:
            findings.append(KeyFinding(fact=fact, source_url=url))

    return findings[:8]


def _parse_sources(text: str, pages: list[PageContent]) -> list[str]:
    """Extract URLs from sources text."""
    urls = re.findall(r'https?://[^\s,\)]+', text)
    # Clean trailing punctuation
    urls = [u.rstrip(".,)") for u in urls]
    # Deduplicate preserving order
    seen = set()
    result = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    # Fall back to all successful source URLs if none parsed
    if not result:
        result = [p.url for p in pages if p.has_real_content]
    return result[:10]


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def extract_insights_dspy(
    goal: str,
    pages: list[PageContent],
    cfg: ScoutConfig,
) -> ScoutInsights:
    """
    Extract insights using DSPy + Instructor pipeline.

    Tries in order:
      1. DSPy ChainOfThought  — best reasoning, structured parse
      2. Instructor MD_JSON   — guaranteed Pydantic output
      3. Raises LLMError      — caller falls back to raw llm.py

    Returns ScoutInsights (always structured, never raw string).
    """
    # Try DSPy first
    try:
        logger.info("Trying DSPy ChainOfThought extraction...")
        result = _extract_with_dspy(goal, pages, cfg)
        if result.summary and result.key_findings:
            logger.info("DSPy extraction succeeded: %d findings", len(result.key_findings))
            return result
        logger.warning("DSPy returned empty summary/findings, falling back to Instructor")
    except Exception as e:
        logger.warning("DSPy failed: %s — falling back to Instructor", e)

    # Try Instructor
    try:
        logger.info("Trying Instructor MD_JSON extraction...")
        result = _extract_with_instructor(goal, pages, cfg)
        if result.summary:
            logger.info("Instructor extraction succeeded: %d findings", len(result.key_findings))
            return result
        logger.warning("Instructor returned empty result")
    except Exception as e:
        logger.warning("Instructor failed: %s", e)
        raise LLMError(f"Both DSPy and Instructor failed: {e}") from e

    raise LLMEmptyResponseError("All structured extraction methods returned empty results")
