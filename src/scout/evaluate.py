"""Benchmark a model on saved research: which one extracts the most verifiable findings?

A case is a goal, its plan and the exact pages that were read, plus expectations: facts the
trusted findings should contain, and claims that must never be trusted. ``scout eval export``
turns any stored run into a case; ``scout eval run`` scores the configured model on cases. With
``--recorded``, a case's saved model replies are replayed instead, which checks Scout's own logic
(verification, flags) against real model output without a model.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from scout.errors import ScoutError
from scout.llm.base import Completion, CompletionRequest
from scout.research.results import Plan, RunResult, Source
from scout.textutil import fold


@dataclass(frozen=True, slots=True)
class EvalCase:
    name: str
    goal: str
    plan: Plan
    sources: tuple[Source, ...]  # with the full text that was read
    expect_facts: tuple[str, ...] = ()
    expect_untrusted: tuple[str, ...] = ()
    replies: dict[str, tuple[str, ...]] = field(default_factory=dict)  # recorded, by step

    @classmethod
    def load(cls, path: Path) -> EvalCase:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            plan = data["plan"]
            return cls(
                name=data.get("name") or path.stem,
                goal=data["goal"],
                plan=Plan(
                    queries=tuple(plan["queries"]),
                    kind=plan["kind"],
                    recency=plan.get("recency"),
                    planner=plan.get("planner", "model"),
                ),
                sources=tuple(
                    replace(Source.from_dict(item), text=item.get("text", item.get("snippet", "")))
                    for item in data["sources"]
                ),
                expect_facts=tuple(data.get("expect", {}).get("facts", [])),
                expect_untrusted=tuple(data.get("expect", {}).get("untrusted", [])),
                replies={step: tuple(texts) for step, texts in data.get("replies", {}).items()},
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ScoutError(f"{path} is not a readable eval case: {exc}") from exc

    def save(self, path: Path) -> None:
        data: dict[str, Any] = {
            "name": self.name,
            "goal": self.goal,
            "plan": {
                "queries": list(self.plan.queries),
                "kind": self.plan.kind,
                "recency": self.plan.recency,
                "planner": self.plan.planner,
            },
            "sources": [source.to_dict() | {"text": source.text} for source in self.sources],
            "expect": {"facts": list(self.expect_facts), "untrusted": list(self.expect_untrusted)},
        }
        if self.replies:
            data["replies"] = {step: list(texts) for step, texts in self.replies.items()}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


@dataclass(frozen=True, slots=True)
class EvalScore:
    case: str
    model: str
    findings: int
    trusted: int
    facts_found: tuple[str, ...]
    facts_missed: tuple[str, ...]
    violations: tuple[str, ...]  # claims that should never be trusted, but were
    confidence: str
    seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "findings": self.findings,
            "trusted": self.trusted,
            "facts_found": list(self.facts_found),
            "facts_missed": list(self.facts_missed),
            "violations": list(self.violations),
            "confidence": self.confidence,
            "seconds": round(self.seconds, 1),
        }

    @classmethod
    def from_dict(cls, case: str, data: dict[str, Any]) -> EvalScore:
        return cls(
            case=case,
            model=data["model"],
            findings=data["findings"],
            trusted=data["trusted"],
            facts_found=tuple(data["facts_found"]),
            facts_missed=tuple(data["facts_missed"]),
            violations=tuple(data["violations"]),
            confidence=data["confidence"],
            seconds=data.get("seconds", 0.0),
        )


def score(case: EvalCase, result: RunResult) -> EvalScore:
    trusted = [
        fold(f"{finding.claim} {finding.quote} {finding.value or ''}")
        for finding in result.findings
        if finding.trusted
    ]

    def present(text: str) -> bool:
        needle = fold(text)
        return any(needle in haystack for haystack in trusted)

    return EvalScore(
        case=case.name,
        model=result.model,
        findings=len(result.findings),
        trusted=len(trusted),
        facts_found=tuple(fact for fact in case.expect_facts if present(fact)),
        facts_missed=tuple(fact for fact in case.expect_facts if not present(fact)),
        violations=tuple(text for text in case.expect_untrusted if present(text)),
        confidence=result.confidence.level,
        seconds=(result.finished_at - result.started_at).total_seconds(),
    )


def save_scores(scores: Sequence[EvalScore], path: Path) -> None:
    """Keep scores as a baseline for later runs to be compared with."""
    data = {score.case: score.to_dict() for score in scores}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_scores(path: Path) -> dict[str, EvalScore]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {case: EvalScore.from_dict(case, item) for case, item in data.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ScoutError(f"{path} is not a readable scores file: {exc}") from exc


def regressions(baseline: dict[str, EvalScore], scores: Sequence[EvalScore]) -> list[str]:
    """What got worse than the baseline: an expected fact no longer found, or a claim that
    must never be trusted now being trusted. Cases the baseline lacks are not compared."""
    problems = []
    for score in scores:
        before = baseline.get(score.case)
        if before is None:
            continue
        lost = sorted(set(before.facts_found) - set(score.facts_found))
        trusted_now = sorted(set(score.violations) - set(before.violations))
        if lost:
            problems.append(f"{score.case}: no longer finds {'; '.join(lost)}")
        if trusted_now:
            problems.append(f"{score.case}: now trusts {'; '.join(trusted_now)}")
    return problems


def case_files(paths: Sequence[Path]) -> list[Path]:
    """Case files named directly, or every *.json inside the named folders."""
    files: list[Path] = []
    for path in paths:
        files.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])
    return files


class RecordedReplies:
    """A backend that answers each step with the case's recorded replies, in order."""

    def __init__(self, case: EvalCase) -> None:
        self.model = f"recorded ({case.name})"
        self._queues = {step: list(texts) for step, texts in case.replies.items()}

    def close(self) -> None:
        pass

    def complete(self, request: CompletionRequest) -> Completion:
        queue = self._queues.get(request.purpose)
        if not queue:
            raise ScoutError(f"the case has no recorded reply for the {request.purpose} step")
        return Completion(text=queue.pop(0), model=self.model)
