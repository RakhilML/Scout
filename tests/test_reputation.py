from dataclasses import replace
from datetime import timedelta

from scout.research.pipeline import _select
from scout.research.reputation import AVOID_AFTER, RETRY_AFTER, SiteRecord
from scout.research.results import Finding, Plan, Verdict
from scout.store import Rating, Store
from scout.web.fetch import Document, FetchStatus
from scout.web.search import SearchHit
from tests.helpers import NOW
from tests.helpers import SAMPLE_RESULT as RESULT


def test_a_site_that_keeps_failing_is_avoided_for_a_while():
    failing = SiteRecord("paywall.example", streak=AVOID_AFTER, last_failure=NOW)
    assert failing.avoided(NOW + timedelta(days=1)) == "paywall.example failed 3 times in a row"
    assert failing.avoided(NOW + RETRY_AFTER) is None  # another chance
    assert replace(failing, streak=AVOID_AFTER - 1).avoided(NOW) is None


def test_standing_follows_how_quotes_hold_up_and_what_people_said():
    assert SiteRecord("new.example").standing == 0
    reliable = SiteRecord("docs.example", findings=10, verified=10)
    sloppy = SiteRecord("spam.example", findings=10, verified=2)
    assert reliable.standing > 0.8 > 0 > sloppy.standing
    assert replace(reliable, rated_bad=5).standing < 0
    assert abs(reliable.bonus) <= 0.1


def test_the_store_learns_from_fetches_runs_and_ratings():
    with Store(":memory:") as store:
        for status in (FetchStatus.BLOCKED, FetchStatus.TIMEOUT, FetchStatus.NOT_FOUND):
            store.put_page(Document(url="https://shop.example/a", status=status, fetched_at=NOW))
        shop = store.site_records(["shop.example"])["shop.example"]
        assert (shop.failures, shop.streak) == (2, 2)  # a missing page says nothing of the site

        store.put_page(
            Document(url="https://shop.example/b", status=FetchStatus.OK, fetched_at=NOW)
        )
        assert store.site_records(["shop.example"])["shop.example"].streak == 0

        invented = Finding("It is $5", "Only $5!", 1, Verdict.UNVERIFIED)
        run = replace(RESULT, findings=(*RESULT.findings[:1], invented))
        store.add_run(run)
        store.add_run(replace(run, carried_over=True))  # nothing new was read: not counted again
        shop = store.site_records(["shop.example"])["shop.example"]
        assert (shop.findings, shop.verified) == (2, 1)

        rating = Rating(
            run_id=1,
            number=1,
            verdict="bad",
            note=None,
            rated_at=NOW,
            goal=RESULT.goal,
            model="m",
            claim="c",
            quote="q",
            value=None,
            url="https://shop.example/5090",
            trusted=True,
        )
        store.put_rating(rating)
        store.put_rating(rating)  # the same verdict again counts once
        assert store.site_records(["shop.example"])["shop.example"].rated_bad == 1
        store.put_rating(replace(rating, verdict="good"))  # changed their mind
        assert store.site_records(["shop.example"])["shop.example"].rated_bad == 0
        assert store.forget_site("shop.example")
        assert store.site_records(["shop.example"]) == {}


PLAN = Plan(queries=("rtx 5090 price",), kind="general", recency=None, planner="model")


def hit(url: str, title: str, rank: int) -> SearchHit:
    return SearchHit(url=url, title=title, snippet="", rank=rank, query="rtx 5090 price")


HITS = [
    hit("https://paywall.example/5090", "RTX 5090 price", 1),
    hit("https://a.example/5090", "RTX 5090 price", 2),
    hit("https://b.example/5090", "RTX 5090 price", 3),
    hit("https://garden.example/hose", "Garden hoses", 4),
]


def select(sites, limit=2):
    warnings: list[str] = []
    chosen = _select(
        HITS, "rtx 5090 price", PLAN, limit=limit, warnings=warnings, sites=sites, now=NOW
    )
    return [h.url.split("/")[2] for h in chosen], warnings


def test_selection_skips_failing_sites_and_prefers_reliable_ones():
    assert select({})[0] == ["paywall.example", "a.example"]

    failing = SiteRecord("paywall.example", streak=5, last_failure=NOW)
    reliable = SiteRecord("b.example", findings=20, verified=20)
    chosen, warnings = select({"paywall.example": failing, "b.example": reliable})
    assert chosen == ["b.example", "a.example"]
    assert "skipped sites that keep failing: paywall.example failed 5 times in a row" in warnings


def test_standing_never_makes_an_off_topic_page_relevant():
    loved = SiteRecord("garden.example", findings=100, verified=100)
    assert "garden.example" not in select({"garden.example": loved}, limit=3)[0]


def test_failing_sites_are_still_read_when_nothing_else_is_left():
    failing = {
        name: SiteRecord(name, streak=9, last_failure=NOW)
        for name in ("paywall.example", "a.example", "b.example")
    }
    chosen, warnings = select(failing)
    assert len(chosen) == 2
    assert not any("skipped" in warning for warning in warnings)
