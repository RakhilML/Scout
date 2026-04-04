"""
LM Studio LLM client for Scout.

Extraction pipeline (tries in order):
  1. DSPy ChainOfThought + Instructor  — structured, typed, self-reasoning
  2. Raw OpenAI call                   — fallback for models that don't cooperate

Handles:
- Reasoning models (max_tokens=-1, <think> stripping, reasoning field fallback)
- Retry logic with exponential backoff
- Token budget management
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

import httpx
from openai import OpenAI, APIConnectionError, APITimeoutError, APIStatusError

from config import ScoutConfig
from exceptions import LLMError, LLMConnectionError, LLMEmptyResponseError
from models import PageContent

logger = logging.getLogger("scout.llm")

# ─── PROMPTS ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert research analyst. Your job is to extract precise, useful insights from web content based on a user's specific research goal.

Rules:
- Only report information that directly answers or is relevant to the user's goal
- Ignore navigation menus, cookie banners, ads, unrelated articles, and boilerplate
- Do NOT hallucinate or add information not present in the sources
- Be direct and concise — no filler, no summaries of summaries
- Format your answer in clean markdown

Output structure:
## Summary
One short paragraph directly answering the goal.

## Key Findings
- Bullet points of the most important facts
- Include numbers, dates, prices, names when present
- Each bullet from a different source if possible

## Sources
- [Title](URL) — one-line note on what this source contributed
"""

# Compact prompt for smaller context windows
SYSTEM_PROMPT_SHORT = """You are a research assistant. Extract only what is relevant to the user's goal from the web content provided. Be concise. Format as markdown with: Summary, Key Findings (bullets), Sources."""


def _build_user_message(goal: str, pages: list[PageContent]) -> str:
    """
    Build the user message containing the goal + all scraped content.
    Only uses pages that have real content (not just snippets when possible).
    Falls back to snippet if full_text is unavailable.
    """
    parts = [f"## Research Goal\n{goal}\n"]

    real_pages = [p for p in pages if p.has_real_content]
    fallback_pages = [p for p in pages if not p.has_real_content]

    # Prefer pages with real content
    all_pages = real_pages + fallback_pages
    if not all_pages:
        return f"## Research Goal\n{goal}\n\n(No web content was retrieved.)"

    parts.append("## Web Content\n")
    for i, page in enumerate(all_pages, 1):
        text = page.display_text
        quality = "" if page.has_real_content else " *(snippet only)*"
        parts.append(
            f"### Source {i}: {page.title}{quality}\n"
            f"URL: {page.url}\n\n"
            f"{text}\n\n"
            f"---\n"
        )

    return "\n".join(parts)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(text) // 4


# ─── RESPONSE PARSING ────────────────────────────────────────────────────────

def _extract_content(response) -> str:
    """
    Extract text from LM Studio response.

    LM Studio reasoning models (like gpt-oss-120b, gemma-3) put their chain-of-thought
    in a `reasoning` field and the final answer in `content`. When max_tokens is too
    low, `content` stays empty. We always use max_tokens=-1 to avoid this.

    Also strips <think>...</think> blocks that some models embed in content.
    """
    choice = response.choices[0]
    message = choice.message

    content = (message.content or "").strip()

    # Strip <think> blocks (DeepSeek, Qwen3, etc.)
    if "<think>" in content:
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

    if content:
        return content

    # Fallback: some LM Studio builds expose reasoning as a separate field
    reasoning = getattr(message, "reasoning", None) or ""
    if reasoning and len(reasoning.strip()) > 20:
        logger.debug("Content empty, using reasoning field as fallback")
        # The reasoning field contains the thinking process — extract only the
        # final answer portion if it has a separator, otherwise use it all
        if "\n\n" in reasoning:
            # Take last substantial paragraph as the answer
            paragraphs = [p.strip() for p in reasoning.split("\n\n") if p.strip()]
            if paragraphs:
                return paragraphs[-1]
        return reasoning.strip()

    return ""


# ─── MAIN LLM CALL ───────────────────────────────────────────────────────────

def extract_insights(
    goal: str,
    pages: list[PageContent],
    cfg: ScoutConfig,
    retries: int = 3,
) -> str:
    """
    Extract insights from gathered pages. Returns a markdown string.

    Pipeline:
      1. DSPy + Instructor (structured, typed, reasoning) → converts to markdown
      2. Raw OpenAI call (fallback for edge cases)

    Raises:
        LLMConnectionError: Cannot reach LM Studio.
        LLMEmptyResponseError: LLM returned no usable content after retries.
        LLMError: Other LLM failures.
    """
    # ── Primary: DSPy + Instructor ──
    try:
        from dspy_llm import extract_insights_dspy
        result = extract_insights_dspy(goal, pages, cfg)
        markdown = result.to_markdown()
        if markdown.strip():
            logger.info("DSPy/Instructor pipeline succeeded")
            return markdown
        logger.warning("DSPy/Instructor returned empty markdown, falling back to raw")
    except LLMConnectionError:
        raise  # connection errors should propagate immediately
    except Exception as e:
        logger.warning("DSPy/Instructor pipeline failed (%s), falling back to raw LLM", e)

    # ── Fallback: Raw OpenAI call ──
    logger.info("Using raw LLM fallback")
    client = OpenAI(
        base_url=cfg.lm_studio_base_url,
        api_key=cfg.lm_studio_api_key,
        timeout=httpx.Timeout(
            connect=10.0,
            read=float(cfg.lm_studio_timeout),
            write=10.0,
            pool=5.0,
        ),
    )

    user_message = _build_user_message(goal, pages)
    estimated_input_tokens = _estimate_tokens(user_message) + _estimate_tokens(SYSTEM_PROMPT)
    logger.debug("Estimated input tokens: ~%d", estimated_input_tokens)

    # Use shorter system prompt if input is very large
    system_prompt = SYSTEM_PROMPT_SHORT if estimated_input_tokens > 12000 else SYSTEM_PROMPT

    last_error: Optional[Exception] = None

    for attempt in range(retries):
        try:
            logger.info("LLM call attempt %d/%d (model: %s)", attempt + 1, retries, cfg.lm_studio_model)

            response = client.chat.completions.create(
                model=cfg.lm_studio_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.3,
                max_tokens=-1,   # CRITICAL: reasoning models need -1 or they return empty content
                stream=False,
            )

            content = _extract_content(response)

            if not content:
                logger.warning("LLM returned empty content on attempt %d", attempt + 1)
                last_error = LLMEmptyResponseError("LLM returned empty content")
                if attempt < retries - 1:
                    time.sleep(2.0 * (attempt + 1))
                continue

            logger.info(
                "LLM responded: %d chars, finish_reason=%s",
                len(content),
                response.choices[0].finish_reason,
            )
            return content

        except APIConnectionError as e:
            raise LLMConnectionError(
                f"Cannot reach LM Studio at {cfg.lm_studio_base_url}.\n"
                f"Make sure LM Studio is running and a model is loaded.\n"
                f"Error: {e}"
            ) from e

        except APITimeoutError as e:
            logger.warning("LLM timeout on attempt %d: %s", attempt + 1, e)
            last_error = e
            if attempt < retries - 1:
                wait = 3.0 * (attempt + 1)
                logger.info("Waiting %.1fs before retry...", wait)
                time.sleep(wait)

        except APIStatusError as e:
            if e.status_code == 503:
                # Model not loaded
                raise LLMConnectionError(
                    f"LM Studio returned 503 — no model is currently loaded.\n"
                    f"Load a model in LM Studio and try again."
                ) from e
            raise LLMError(f"LM Studio API error {e.status_code}: {e.message}") from e

        except Exception as e:
            logger.error("Unexpected LLM error: %s", e)
            last_error = e
            if attempt < retries - 1:
                time.sleep(2.0)

    # All retries exhausted
    if isinstance(last_error, LLMEmptyResponseError):
        raise last_error
    raise LLMError(f"LLM call failed after {retries} attempts: {last_error}")


# ─── MODEL HEALTH CHECK ───────────────────────────────────────────────────────

def check_lm_studio(cfg: ScoutConfig) -> tuple[bool, str]:
    """
    Ping LM Studio to verify it's reachable and the model is available.
    Returns (ok: bool, message: str).
    """
    try:
        client = OpenAI(
            base_url=cfg.lm_studio_base_url,
            api_key=cfg.lm_studio_api_key,
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
        )
        models = client.models.list()
        model_ids = [m.id for m in models.data]

        if cfg.lm_studio_model in model_ids:
            return True, f"Model '{cfg.lm_studio_model}' is available."
        else:
            available = ", ".join(model_ids[:8])
            return False, (
                f"Model '{cfg.lm_studio_model}' not found in LM Studio.\n"
                f"Available models: {available}"
            )

    except APIConnectionError:
        return False, f"Cannot connect to LM Studio at {cfg.lm_studio_base_url}"
    except Exception as e:
        return False, f"Health check failed: {e}"


def list_available_models(cfg: ScoutConfig) -> list[str]:
    """Return list of model IDs from LM Studio. Empty list on error."""
    try:
        client = OpenAI(
            base_url=cfg.lm_studio_base_url,
            api_key=cfg.lm_studio_api_key,
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
        )
        models = client.models.list()
        return [m.id for m in models.data]
    except Exception:
        return []
