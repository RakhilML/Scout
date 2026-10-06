from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from scout.errors import (
    AnswerPending,
    ContextOverflow,
    LLMUnavailable,
    SearchError,
    UnsupportedResponseFormat,
)
from scout.llm.base import Completion, CompletionRequest
from scout.llm.structured import StructuredMode
from scout.research.pipeline import Researcher, ResearchOptions, source_budget
from scout.research.results import Flag, Plan, Verdict
from scout.store import Store
from scout.web.extract import Offer
from scout.web.fetch import Document, FetchStatus
from tests.helpers import NOW, Clock, FakeFetcher, FakeSearch, ScriptedBackend

GOAL = "cheapest RTX 5090 price"
SHOP_A = "https://shop-a.example/rtx-5090"
DEALS_B = "https://deals-b.example/5090"
BLOCKED_C = "https://blocked.example/5090-tracker"
GARDEN = "https://garden.example/hose"

SEARCH = {
    "rtx 5090 price": [
        (SHOP_A, "RTX 5090 Founders Edition price", "Buy the RTX 5090"),
        (GARDEN, "Best garden hoses 2026", "Water your plants"),
        (BLOCKED_C, "RTX 5090 price tracker", "Track RTX 5090 prices daily"),
    ],
    "rtx 5090 deals": [
        (DEALS_B, "RTX 5090 deals", "RTX 5090 price drops"),
        (SHOP_A, "RTX 5090 Founders Edition price", "Buy the RTX 5090"),
    ],
}
PAGES = {
    SHOP_A: "The RTX 5090 Founders Edition sells for $1,999 at Shop A. Stock is limited.",
    DEALS_B: Document(
        url=DEALS_B,
        status=FetchStatus.OK,
        fetched_at=NOW,
        title="RTX 5090 deals",
        text="Deals on the RTX 5090 graphics card this week.",
        offers=(Offer(product="RTX 5090", price=Decimal("1899"), currency="USD"),),
    ),
}
PLAN = {"queries": ["rtx 5090 price", "rtx 5090 deals"], "kind": "price", "recency": "month"}
EXTRACTION = {
    "answer": "The cheapest RTX 5090 listed is $1,999 at Shop A.",
    "findings": [
        {
            "claim": "Shop A sells the RTX 5090 FE for $1,999",
            "quote": "The RTX 5090 Founders Edition sells for $1,999 at Shop A.",
            "source": 1,
            "entity": "RTX 5090 Founders Edition",
            "attribute": "price",
            "value": "$1,999",
        },
        {
            "claim": "Shop Z has it for $800",
            "quote": "Shop Z has the RTX 5090 for $800.",
            "source": 2,
            "entity": "RTX 5090",
            "attribute": "price",
            "value": "$800",
        },
    ],
}


def researcher(backend, *, search=None, pages=None, **options) -> Researcher:
    return Researcher(
        search=search or FakeSearch(SEARCH),
        fetcher=FakeFetcher(PAGES if pages is None else pages),
        backend=backend,
        options=ResearchOptions(**options),
        clock=Clock(),
    )


def test_a_full_run_plans_filters_reads_extracts_and_verifies():
    backend = ScriptedBackend({"plan": [PLAN], "extract": [EXTRACTION]})
    search = FakeSearch(SEARCH)
    result = researcher(backend, search=search).run(GOAL)

    assert backend.purposes() == ["plan", "extract"]
    assert [call["query"] for call in search.calls] == ["rtx 5090 price", "rtx 5090 deals"]
    assert {call["recency"] for call in search.calls} == {"month"}
    assert (result.plan.planner, result.plan.kind) == ("model", "price")

    # the garden hose was off-topic; the blocked page is kept as a snippet
    assert [s.url for s in result.sources] == [SHOP_A, DEALS_B, BLOCKED_C]
    assert [s.snippet_only for s in result.sources] == [False, False, True]
    prompt = backend.requests[1].messages[1].content
    assert 'note="only the search snippet; the page could not be read"' in prompt
    assert "Prices this page publishes as structured data" in prompt

    shop, offer, invented = result.findings  # trusted first, as reports number them
    assert (shop.verdict, shop.amount, shop.currency) == (Verdict.VERIFIED, Decimal("1999"), "USD")
    assert invented.verdict is Verdict.UNVERIFIED
    assert (offer.origin, offer.amount, offer.source) == ("structured-data", Decimal("1899"), 2)

    assert result.confidence.level == "medium"
    assert result.confidence.reason == "2 of 3 findings trusted across 2 site(s)"
    assert "ignored 1 off-topic search result(s)" in result.warnings
    assert any("1 of 3 pages could not be read (1 blocked)" in w for w in result.warnings)


def test_unusable_plan_falls_back_to_searching_the_goal():
    backend = ScriptedBackend({"plan": ["not json", "still not json"], "extract": [EXTRACTION]})
    search = FakeSearch({GOAL: SEARCH["rtx 5090 price"]})
    result = researcher(backend, search=search).run(GOAL)
    assert result.plan.planner == "heuristic"
    assert search.calls[0]["query"] == GOAL
    assert any("plan was unusable" in w for w in result.warnings)


def test_forced_kind_and_recency_override_the_plan():
    backend = ScriptedBackend({"plan": [PLAN], "extract": [EXTRACTION]})
    search = FakeSearch(SEARCH)
    result = researcher(backend, search=search, kind="general", recency="any").run(GOAL)
    assert (result.plan.kind, result.plan.recency) == ("general", None)
    assert {call["recency"] for call in search.calls} == {None}
    assert all(f.origin == "model" for f in result.findings)  # offers are for price goals


def test_a_given_plan_is_not_replanned_but_still_obeys_forced_options():
    backend = ScriptedBackend({"extract": [EXTRACTION]})
    search = FakeSearch(SEARCH)
    stored = Plan(queries=("rtx 5090 price",), kind="price", recency="month", planner="model")
    result = researcher(backend, search=search, recency="week").run(GOAL, plan=stored)
    assert backend.purposes() == ["extract"]
    assert result.plan == replace(stored, recency="week")
    assert {call["recency"] for call in search.calls} == {"week"}


def test_empty_dated_search_retries_without_a_date_limit():
    class DatedOnlyEmpty(FakeSearch):
        def search(self, query, *, max_results, region, recency, news):
            hits = super().search(
                query, max_results=max_results, region=region, recency=recency, news=news
            )
            return [] if recency else hits

    backend = ScriptedBackend({"plan": [PLAN], "extract": [EXTRACTION]})
    result = researcher(backend, search=DatedOnlyEmpty(SEARCH)).run(GOAL)
    assert "nothing from the last month; searched without a date limit" in result.warnings
    assert result.sources


def test_no_results_at_all_is_an_error():
    backend = ScriptedBackend({"plan": [PLAN]})
    with pytest.raises(SearchError, match="no search results"):
        researcher(backend, search=FakeSearch({})).run(GOAL)


def test_context_overflow_retries_once_with_less_text():
    backend = ScriptedBackend(
        {"plan": [PLAN], "extract": [ContextOverflow("too long"), EXTRACTION]}
    )
    result = researcher(backend).run(GOAL)
    first, second = (r for r in backend.requests if r.purpose == "extract")
    assert len(second.messages[1].content) <= len(first.messages[1].content)
    assert "the sources did not fit the model's context window; sent less text" in result.warnings


def test_pages_left_out_by_a_small_context_are_reported():
    urls = [f"https://site{i}.example/rtx-5090" for i in range(5)]
    long_page = "RTX 5090 price report " + "x" * 3000  # no sentence breaks: 900-character chunks
    search = FakeSearch({"rtx 5090 price": [(u, "RTX 5090 price", "RTX 5090") for u in urls]})
    backend = ScriptedBackend({"plan": [PLAN], "extract": [{"answer": "a", "findings": []}]})
    result = researcher(
        backend, search=search, pages=dict.fromkeys(urls, long_page), context_tokens=1024
    ).run(GOAL)
    assert source_budget(1024) < 5 * 900
    assert "2 readable page(s) did not fit the context window" in result.warnings


class SchemaRejectingBackend(ScriptedBackend):
    def complete(self, request: CompletionRequest) -> Completion:
        if request.schema is not None:
            self.requests.append(request)
            raise UnsupportedResponseFormat(
                "'response_format.type' must be 'json_schema' or 'text'"
            )
        return super().complete(request)


def test_schema_mode_falls_back_to_prompt_mode_once():
    backend = SchemaRejectingBackend({"plan": [PLAN], "extract": [EXTRACTION]})
    run = researcher(backend, structured_mode=StructuredMode.SCHEMA)
    result = run.run(GOAL)
    assert run.structured_mode is StructuredMode.PROMPT
    assert [r.schema is not None for r in backend.requests] == [True, False, False]
    assert result.findings


def test_pending_answers_are_not_mistaken_for_bad_plans():
    backend = ScriptedBackend({"plan": [AnswerPending(Path("requests/abc.md"))]})
    with pytest.raises(AnswerPending):
        researcher(backend).run(GOAL)


@pytest.mark.parametrize(
    ("options", "efforts"), [(("low", "medium", "high"), ["low", "medium"]), ((), [None, None])]
)
def test_reasoning_effort_is_sent_only_when_the_model_supports_it(options, efforts):
    backend = ScriptedBackend({"plan": [PLAN], "extract": [EXTRACTION]})
    researcher(backend, reasoning_options=options).run(GOAL)
    assert [r.reasoning_effort for r in backend.requests] == efforts


def test_the_same_pages_found_in_another_order_carry_the_analysis_over():
    backend = ScriptedBackend({"plan": [PLAN], "extract": [EXTRACTION, EXTRACTION]})
    first = researcher(backend).run(GOAL)
    reordered = FakeSearch({query: rows[::-1] for query, rows in SEARCH.items()})
    fresh = researcher(backend, search=reordered).run(GOAL, plan=first.plan)
    assert [s.url for s in fresh.sources] != [s.url for s in first.sources]

    again = researcher(backend, search=reordered).run(GOAL, plan=first.plan, reuse=first)
    assert again.carried_over
    assert backend.purposes() == ["plan", "extract", "extract"]  # not asked a third time
    assert [(s.index, s.url) for s in again.sources] == [(s.index, s.url) for s in first.sources]


@pytest.mark.parametrize(("kind", "flag"), [("news", Flag.STALE), ("price", None)])
def test_old_news_is_set_aside_however_exactly_it_is_quoted(kind, flag):
    dated = Document(
        url=SHOP_A,
        status=FetchStatus.OK,
        fetched_at=NOW,
        text=PAGES[SHOP_A],
        published=date(2025, 1, 2),
    )
    plan = Plan(queries=("rtx 5090 price",), kind=kind, recency="week", planner="model")
    backend = ScriptedBackend({"extract": [EXTRACTION]})
    result = researcher(backend, pages={**PAGES, SHOP_A: dated}).run(GOAL, plan=plan)
    shop = next(f for f in result.findings if f.source == 1)
    assert (shop.verdict, shop.flag) == (Verdict.VERIFIED, flag)
    if flag:
        assert shop.note == "from a page dated 2025-01-02, older than the week asked"


def test_a_run_waiting_for_an_answer_resumes_with_the_pages_it_read():
    clock = Clock()
    backend = ScriptedBackend(
        {"plan": [PLAN, PLAN], "extract": [AnswerPending(Path("requests/x.md")), EXTRACTION]}
    )
    with Store(":memory:") as store:

        def run(pages):
            fetcher = FakeFetcher(pages)
            search = FakeSearch(SEARCH)
            return Researcher(
                search=search, fetcher=fetcher, backend=backend, clock=clock, pins=store
            ).run(GOAL)

        with pytest.raises(AnswerPending):
            run(PAGES)
        clock.advance(3 * 3600)  # the page changed while the answer was being written
        result = run({**PAGES, SHOP_A: PAGES[SHOP_A].replace("1,999", "1,949")})

        assert backend.requests[1].messages == backend.requests[3].messages
        assert result.started_at == NOW
        assert result.findings[0].verdict is Verdict.VERIFIED
        assert store.get_pin(GOAL, "started", newer_than=NOW - timedelta(days=9)) is None


def test_a_failing_round_keeps_what_earlier_rounds_found():
    backend = ScriptedBackend(
        {
            "plan": [PLAN],
            "extract": [EXTRACTION, LLMUnavailable("the model server went away")],
            "follow_up": [LOOK_IN_EUROPE],
        }
    )
    result = deep(backend).run_deep(GOAL)
    assert result.rounds == 1
    assert "stopped looking further: the model server went away" in result.warnings


def test_source_budget_leaves_room_for_the_answer():
    assert source_budget(8192) == int((8192 - 2048 - 1200) * 3.2)
    assert source_budget(131072) == int((131072 - 32768 - 1200) * 3.2)
    assert source_budget(2048) == int(1024 * 3.2)  # never below a useful minimum


EU_SHOP = "https://eu-shop.example/rtx-5090"
DEEP_SEARCH = {
    **SEARCH,
    "rtx 5090 price europe": [
        (SHOP_A, "RTX 5090 Founders Edition price", "Buy the RTX 5090"),  # read already
        (EU_SHOP, "RTX 5090 price Europe", "RTX 5090 in euros"),
    ],
}
DEEP_PAGES = {**PAGES, EU_SHOP: "The RTX 5090 costs 2,199 EUR at EU Shop. Ships from Berlin."}
EU_PRICE = {
    "claim": "EU Shop sells the RTX 5090 for 2,199 EUR",
    "quote": "The RTX 5090 costs 2,199 EUR at EU Shop.",
    "source": 4,
    "entity": "RTX 5090",
    "attribute": "price",
    "value": "2,199 EUR",
}
LOOK_IN_EUROPE = {
    "done": False,
    "gaps": "only US prices so far",
    "queries": ["rtx 5090 price europe", "rtx 5090 price"],  # the second was searched already
}


def deep(backend):
    return researcher(backend, search=FakeSearch(DEEP_SEARCH), pages=DEEP_PAGES)


def test_deep_research_fills_gaps_with_pages_it_has_not_read():
    backend = ScriptedBackend(
        {
            "plan": [PLAN],
            "extract": [EXTRACTION, {"answer": "2,199 EUR in Europe.", "findings": [EU_PRICE]}],
            "follow_up": [LOOK_IN_EUROPE, {"done": True, "gaps": "", "queries": []}],
            "answer": [{"answer": "About $1,999 in the US [1] and 2,199 EUR in Europe [3]."}],
        }
    )
    result = deep(backend).run_deep(GOAL, rounds=3)

    assert backend.purposes() == ["plan", "extract", "follow_up", "extract", "follow_up", "answer"]
    assert result.rounds == 2
    assert result.plan.queries == ("rtx 5090 price", "rtx 5090 deals", "rtx 5090 price europe")
    assert [s.index for s in result.sources] == [1, 2, 3, 4]
    assert result.sources[3].url == EU_SHOP
    eu = next(f for f in result.findings if f.currency == "EUR")
    assert (eu.verdict, eu.source, eu.amount) == (Verdict.VERIFIED, 4, Decimal("2199"))
    assert result.answer == "About $1,999 in the US [1] and 2,199 EUR in Europe [3]."
    assert "deep research: 2 rounds, 3 pages read" in result.warnings

    follow_up = backend.requests[2].messages[1].content
    assert "[1] Shop A sells the RTX 5090 FE for $1,999" in follow_up
    assert "- rtx 5090 deals" in follow_up  # the searches made so far


def test_deep_research_stops_when_a_round_adds_nothing():
    known_again = {"answer": "Same.", "findings": [dict(EXTRACTION["findings"][0], source=4)]}
    backend = ScriptedBackend(
        {"plan": [PLAN], "extract": [EXTRACTION, known_again], "follow_up": [LOOK_IN_EUROPE]}
    )
    result = deep(backend).run_deep(GOAL, rounds=3)
    assert result.rounds == 1
    assert "answer" not in backend.purposes()  # the first answer stands
    assert len(result.sources) == 3


def test_an_unusable_follow_up_ends_the_search_with_a_note():
    backend = ScriptedBackend(
        {"plan": [PLAN], "extract": [EXTRACTION], "follow_up": ["no json", "still none"]}
    )
    result = deep(backend).run_deep(GOAL)
    assert result.rounds == 1
    assert any("stopped looking further" in warning for warning in result.warnings)


def test_a_run_waiting_more_than_a_day_reads_the_web_again():
    clock = Clock()
    pending = AnswerPending(Path("requests/x.md"))
    backend = ScriptedBackend({"plan": [PLAN] * 3, "extract": [pending, pending, EXTRACTION]})
    pages = dict(PAGES)
    with Store(":memory:") as store:

        def run():
            fetcher = FakeFetcher(pages)
            return Researcher(
                search=FakeSearch(SEARCH), fetcher=fetcher, backend=backend, clock=clock, pins=store
            ).run(GOAL)

        for hours in (0, 20):  # still waiting: retried, the pins keep their age
            clock.advance(hours * 3600)
            with pytest.raises(AnswerPending):
                run()
        clock.advance(10 * 3600)
        pages[SHOP_A] = PAGES[SHOP_A].replace("1,999", "1,949")
        result = run()
    assert result.started_at == NOW + timedelta(hours=30)
    assert "1,949" in backend.requests[-1].messages[1].content


def test_runs_of_one_goal_by_different_watches_resume_apart():
    clock = Clock()
    backend = ScriptedBackend(
        {
            "plan": [PLAN] * 3,
            "extract": [AnswerPending(Path("requests/x.md")), EXTRACTION, EXTRACTION],
        }
    )
    pages = dict(PAGES)
    with Store(":memory:") as store:

        def run(scope):
            return Researcher(
                search=FakeSearch(SEARCH),
                fetcher=FakeFetcher(pages),
                backend=backend,
                clock=clock,
                pins=store,
                pin_scope=scope,
            ).run(GOAL)

        with pytest.raises(AnswerPending):
            run("watch:a")
        pages[SHOP_A] = PAGES[SHOP_A].replace("1,999", "1,949")
        run("watch:b")  # done: its pins go, not those of watch a
        run("watch:a")
    assert backend.requests[1].messages == backend.requests[-1].messages


def test_a_carried_over_news_run_still_ages():
    dated = Document(
        url=SHOP_A, status=FetchStatus.OK, fetched_at=NOW, text=PAGES[SHOP_A], published=NOW.date()
    )
    plan = Plan(queries=("rtx 5090 price",), kind="news", recency="week", planner="model")
    clock = Clock()
    backend = ScriptedBackend({"extract": [EXTRACTION]})

    def run(reuse=None):
        return Researcher(
            search=FakeSearch(SEARCH),
            fetcher=FakeFetcher({**PAGES, SHOP_A: dated}),
            backend=backend,
            clock=clock,
        ).run(GOAL, plan=plan, reuse=reuse)

    first = run()
    assert first.findings[0].flag is None
    clock.advance(30 * 24 * 3600)
    later = run(reuse=first)
    assert later.carried_over
    assert Flag.STALE in {finding.flag for finding in later.findings}
