"""A watch over several runs of a changing (fake) web: each change alerts once, noise never does."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from scout.app import App
from scout.errors import AnswerPending, ConfigError, NotifyError
from scout.files import FileLock
from scout.monitor.diff import Change
from scout.monitor.runner import WatchRun, deliver, digest, run_watch
from scout.monitor.watches import Watch, WatchBook
from scout.settings import Settings
from scout.store import AlertRecord
from tests.helpers import NOW, FakeFetcher, FakeSearch, ScriptedBackend

SHOP = "https://shop-a.example/rtx-5090"
DEALS = "https://deals-b.example/5090"
SEARCH = {
    "rtx 5090 price": [
        (SHOP, "RTX 5090 Founders Edition price", "Buy the RTX 5090"),
        (DEALS, "RTX 5090 deals", "RTX 5090 price drops"),
    ]
}
PLAN = {"queries": ["rtx 5090 price"], "kind": "price", "recency": "month"}
FALLING = "Prices are falling fast this week."
COOLERS = "Board partners ship new coolers."


def shop_page(price: str, extra: str = "") -> str:
    return f"The RTX 5090 Founders Edition sells for {price} at Shop A. {extra}".strip()


def price_at_shop(price: str) -> dict:
    return {
        "claim": f"Shop A sells the RTX 5090 FE for {price}",
        "quote": f"The RTX 5090 Founders Edition sells for {price} at Shop A.",
        "source": 1,
        "entity": "RTX 5090 Founders Edition",
        "attribute": "price",
        "value": price,
    }


def said(sentence: str) -> dict:
    return {"claim": sentence, "quote": sentence, "source": 2}


def extraction(*findings: dict) -> dict:
    return {"answer": "See the findings.", "findings": list(findings)}


class Inbox:
    """A notifier that keeps what it was sent, and fails while *down*."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.down = False

    def send(self, title: str, body: str) -> None:
        if self.down:
            raise NotifyError("ntfy.sh unreachable")
        self.sent.append((title, body))


class World:
    """An App whose web, model and notifier the test controls."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.model = ScriptedBackend({"plan": [PLAN]})
        monkeypatch.setattr("scout.app.make_backend", lambda settings: self.model)
        self.app = App(Settings(data_dir=tmp_path, reports_dir=tmp_path / "reports"))
        self.web = FakeFetcher(
            {SHOP: shop_page("$1,999"), DEALS: f"{FALLING} {COOLERS}"}, cache=self.app.store
        )
        self.app.watch_search = FakeSearch(SEARCH)
        self.app.watch_fetcher = self.web
        self.inbox = Inbox()
        self.book = WatchBook(tmp_path / "watches.yaml")
        self.last_calls: list[str] = []  # what the model was asked during the last run

    def run(self, watch: Watch, *replies: object) -> WatchRun:
        self.model.add("extract", *replies)
        before = len(self.model.requests)
        outcome = run_watch(self.app, watch, notifier=self.inbox, book=self.book)
        self.last_calls = self.model.purposes()[before:]
        return outcome


@pytest.fixture
def world(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    yield world
    world.app.close()


GPU = Watch(
    name="gpu",
    goal="cheapest RTX 5090 price",
    every="6h",
    alerts=("price below 1900 USD", "changed"),
)


def reasons(outcome) -> list[str]:
    return [trigger.reason for trigger in outcome.alerts]


def test_a_watch_alerts_once_per_change_and_never_on_noise(world):
    # 1. Baseline: everything is new, nothing alerts (the price is above the limit).
    first = world.run(GPU, extraction(price_at_shop("$1,999"), said(FALLING)))
    assert first.baseline
    assert first.counts == {Change.NEW: 2}
    assert first.alerts == ()
    assert world.last_calls == ["plan", "extract"]

    # 2. No page changed: the last analysis is kept and the model is not asked at all.
    second = world.run(GPU)
    assert second.result.carried_over
    assert world.last_calls == []
    assert second.counts == {Change.SAME: 2}
    assert second.alerts == ()

    # 3. The shop drops its price under the limit: two alerts, one notification.
    world.web.pages[SHOP] = shop_page("$1,849")
    third = world.run(GPU, extraction(price_at_shop("$1,849"), said(FALLING)))
    assert world.last_calls == ["extract"]  # the plan is reused
    assert reasons(third) == [
        "price below 1900 USD: RTX 5090 Founders Edition: $1,849",
        "changed: RTX 5090 Founders Edition: $1,999 \N{RIGHTWARDS ARROW} $1,849",
    ]
    assert third.delivered == 2
    title, body = world.inbox.sent[-1]
    assert title == "Scout gpu: 2 alerts"
    assert SHOP in body

    # 4. The page changes elsewhere; the model now reports a sentence that was always there
    #    and forgets another one. The price is still under the limit. Nothing to say.
    world.web.pages[SHOP] = shop_page("$1,849", "Free shipping this week.")
    fourth = world.run(GPU, extraction(price_at_shop("$1,849"), said(COOLERS)))
    assert fourth.counts == {Change.SAME: 2, Change.NOTICED: 1}
    assert fourth.alerts == ()
    assert len(world.inbox.sent) == 1

    # 5. The price goes back up while notifications fail: the alert waits.
    world.web.pages[SHOP] = shop_page("$1,999")
    world.inbox.down = True
    fifth = world.run(GPU, extraction(price_at_shop("$1,999"), said(COOLERS)))
    assert reasons(fifth) == [
        "changed: RTX 5090 Founders Edition: $1,849 \N{RIGHTWARDS ARROW} $1,999"
    ]
    assert (fifth.delivered, fifth.notify_error) == (0, "ntfy.sh unreachable")

    # 6. A sentence leaves the deals page, notifications work again: both alerts go out.
    world.web.pages[DEALS] = COOLERS
    world.inbox.down = False
    sixth = world.run(GPU, extraction(price_at_shop("$1,999"), said(COOLERS)))
    assert reasons(sixth) == [f"no longer on deals-b.example: {FALLING}"]
    assert sixth.delivered == 2
    _, body = world.inbox.sent[-1]
    assert "$1,849 \N{RIGHTWARDS ARROW} $1,999" in body
    assert "no longer on deals-b.example" in body

    store = world.app.store
    assert [alert.delivered_at is not None for alert in store.alerts("gpu")] == [True] * 4
    feed = world.app.settings.feed_path("gpu").read_text(encoding="utf-8")
    assert feed.count("<entry>") == 4  # every run refreshes the watch's feed
    prices = [str(o.amount) for o in store.observations("gpu")]
    assert prices == ["1999", "1999", "1849", "1849", "1999", "1999"]


def test_editing_the_goal_plans_again_and_starts_a_new_baseline(world):
    world.run(GPU, extraction(price_at_shop("$1,999"), said(FALLING)))
    world.model.add("plan", PLAN)
    world.web.pages[SHOP] = shop_page("$1,849")
    edited = replace(GPU, goal="cheapest RTX 5090 price in the US")
    outcome = world.run(edited, extraction(price_at_shop("$1,849"), said(FALLING)))
    assert world.last_calls == ["plan", "extract"]
    assert outcome.baseline
    assert Change.CHANGED not in outcome.counts  # the old question's facts were set aside
    # A baseline keeps "changed" quiet; a price limit that holds still speaks.
    assert reasons(outcome) == ["price below 1900 USD: RTX 5090 Founders Edition: $1,849"]


def test_a_watch_that_stops_when_alerted_is_paused(world):
    watch = Watch(
        name="drop",
        goal="cheapest RTX 5090 price",
        every="1d",
        alerts=("below 2000",),
        stop_when_alerted=True,
    )
    world.book.add(watch)
    outcome = world.run(watch, extraction(price_at_shop("$1,999"), said(FALLING)))
    assert reasons(outcome) == ["below 2000: RTX 5090 Founders Edition: $1,999"]
    assert world.book.get("drop").paused


def test_a_run_that_fails_leaves_the_watch_as_it_was(world, tmp_path):
    world.run(GPU, extraction(price_at_shop("$1,999"), said(FALLING)))
    world.web.pages[SHOP] = shop_page("$1,849")
    with pytest.raises(AnswerPending):
        world.run(GPU, AnswerPending(tmp_path / "request.md"))
    assert len(world.app.store.last_runs("gpu", limit=10)) == 1
    assert world.app.store.facts("gpu")[0].value == "$1,999"


def test_digest_lists_each_alert_with_its_evidence(world):
    world.run(GPU, extraction(price_at_shop("$1,999"), said(FALLING)))
    world.web.pages[DEALS] = COOLERS
    world.run(GPU, extraction(price_at_shop("$1,999")))
    (alert,) = world.app.store.alerts("gpu")
    title, body = digest(GPU, [alert])
    assert title == "Scout gpu: 1 alert"
    assert body.splitlines() == [
        "cheapest RTX 5090 price",
        "",
        f"- no longer on deals-b.example: {FALLING}",
        f'  "{FALLING}"',
        f"  {DEALS}",
        "",
        "details: scout watch changes gpu",
    ]


def test_an_alert_links_to_its_quote_on_the_page_the_run_read(world):
    world.run(GPU, extraction(price_at_shop("$1,999")))
    world.web.pages[SHOP] = shop_page("$1,849")
    world.run(GPU, extraction(price_at_shop("$1,849")))

    deep = f"{SHOP}#:~:text=The%20RTX%205090%20Founders,%241%2C849%20at%20Shop%20A."
    assert [alert.link for alert in world.app.store.alerts("gpu")] == [deep, deep]
    (_, body), *_ = world.inbox.sent
    assert body.count(f"\n  {deep}\n") == 2
    feed = world.app.settings.feed_path("gpu").read_text(encoding="utf-8")
    assert f'href="{deep}"' in feed


def test_one_run_of_a_watch_at_a_time(world):
    held = FileLock(world.app.settings.data_dir / "locks" / "gpu.lock", wait=False)
    with held, pytest.raises(ConfigError, match="'gpu' is already running"):
        world.run(GPU, extraction(price_at_shop("$1,999")))
    assert world.app.store.last_runs("gpu") == []


def test_a_feed_that_cannot_be_written_does_not_lose_the_run(world, caplog):
    world.app.settings.feed_path("gpu").parent.parent.mkdir(parents=True, exist_ok=True)
    world.app.settings.feed_path("gpu").parent.write_text("not a folder", encoding="utf-8")
    outcome = world.run(GPU, extraction(price_at_shop("$1,999")))
    assert world.app.store.last_runs("gpu")[0][0] == outcome.run_id
    assert "could not write the feed of watch gpu" in caplog.text


def test_a_failed_notification_waits_for_the_next_daily_run(world):
    daily = replace(GPU, every="1d")
    world.web.pages[SHOP] = shop_page("$1,849")
    world.inbox.down = True
    first = world.run(daily, extraction(price_at_shop("$1,849")))
    assert first.notify_error == "ntfy.sh unreachable"
    world.inbox.down = False
    next_day = first.result.finished_at + timedelta(hours=25)
    assert deliver(world.app, daily, world.inbox, now=next_day) == (1, None)


def test_alerts_are_delivered_even_when_the_watch_cannot_be_paused(world, caplog):
    gone = replace(GPU, name="gone", stop_when_alerted=True)  # not in the watches file
    world.web.pages[SHOP] = shop_page("$1,849")
    outcome = world.run(gone, extraction(price_at_shop("$1,849")))
    assert outcome.delivered == 1
    assert "could not pause watch gone" in caplog.text


def test_notifications_carry_web_text_as_inert_text():
    alert = AlertRecord(
        id=1,
        watch="gpu",
        run_id=1,
        created_at=NOW,
        reason="new: <!channel> deal",
        url="https://shop.example/a b<c>",
        quote="See [the deal](https://phish.example) <https://phish.example|here>",
        delivered_at=None,
        error=None,
    )
    _, body = digest(GPU, [alert])
    assert not any(char in body for char in "<>[]")
    assert "https://shop.example/a%20b%3Cc%3E" in body
