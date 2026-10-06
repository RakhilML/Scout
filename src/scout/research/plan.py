"""Decide what to search for: the model plans, a keyword heuristic stands in when it can't."""

from __future__ import annotations

from datetime import date

from scout.llm.base import Backend
from scout.llm.structured import StructuredMode, generate
from scout.research.prompts import plan_messages
from scout.research.rank import tokenize
from scout.research.results import Plan
from scout.research.schema import QueryPlan
from scout.textutil import clean

MAX_QUERIES = 4
_PRICE_WORDS = frozenset(
    {"price", "prices", "cost", "costs", "cheapest", "cheap", "deal", "deals", "msrp"}
)
_NEWS_WORDS = frozenset(
    {"news", "latest", "today", "recent", "happening", "announced", "update", "updates"}
)
_RELEASE_WORDS = frozenset({"release", "released", "version", "versions", "changelog", "features"})
_DEFAULT_RECENCY = {"price": "month", "news": "week", "release": "year", "general": None}


def heuristic_plan(goal: str) -> Plan:
    words = set(tokenize(goal))
    if words & _PRICE_WORDS:
        kind = "price"
    elif words & _NEWS_WORDS:
        kind = "news"
    elif words & _RELEASE_WORDS:
        kind = "release"
    else:
        kind = "general"
    recency = "day" if "today" in words else _DEFAULT_RECENCY[kind]
    return Plan(queries=(goal,), kind=kind, recency=recency, planner="heuristic")


def model_plan(
    goal: str,
    backend: Backend,
    *,
    mode: StructuredMode,
    today: date,
    reasoning_effort: str | None = None,
) -> Plan:
    plan = generate(
        backend,
        plan_messages(goal, today),
        QueryPlan,
        mode=mode,
        purpose="plan",
        reasoning_effort=reasoning_effort,
    )
    queries: list[str] = []
    for query in (clean(q) for q in plan.queries):
        if query and query.casefold() not in {q.casefold() for q in queries}:
            queries.append(query)
    return Plan(
        queries=tuple(queries[:MAX_QUERIES]) or (goal,),
        kind=plan.kind,
        recency=None if plan.recency == "any" else plan.recency,
        planner="model",
    )
