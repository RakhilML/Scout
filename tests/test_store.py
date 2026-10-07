import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import responses

from scout.monitor.diff import Change, Delta, Fact
from scout.monitor.rules import cleared, parse_rule, triggers
from scout.store import _MIGRATIONS, Store
from scout.web.fetch import Document, Fetcher, FetchStatus
from scout.web.search import SearchHit
from tests.helpers import CHECK_RESULT, NOW, Clock
from tests.helpers import SAMPLE_RESULT as RESULT


def test_migrations_apply_once(tmp_path):
    path = tmp_path / "scout.db"
    with Store(path) as store:
        assert store.schema_version == len(_MIGRATIONS)
    with Store(path) as store:  # reopening must not re-run migrations
        assert store.schema_version == len(_MIGRATIONS)


def test_page_round_trip_and_overwrite():
    with Store(":memory:") as store:
        doc = Document(url="https://a.example", status=FetchStatus.OK, fetched_at=NOW, text="hello")
        store.put_page(doc)
        assert store.get_page("https://a.example") == doc
        newer = Document(
            url="https://a.example", status=FetchStatus.BLOCKED, fetched_at=NOW + timedelta(hours=1)
        )
        store.put_page(newer)
        assert store.get_page("https://a.example") == newer
        assert store.get_page("https://missing.example") is None


def test_search_cache_respects_age():
    hits = [SearchHit(url="https://a.example", title="A", snippet="s", rank=1, query="q")]
    with Store(":memory:") as store:
        store.put_search("key", hits, NOW)
        assert store.get_search("key", newer_than=NOW - timedelta(minutes=5)) == hits
        assert store.get_search("key", newer_than=NOW + timedelta(seconds=1)) is None
        assert store.get_search("other", newer_than=NOW - timedelta(days=1)) is None


@responses.activate
def test_store_works_as_the_fetchers_page_cache(tmp_path):
    body = (
        b"<html><body><article><p>"
        + b"Cached content survives restarts. " * 20
        + b"</p></article></body></html>"
    )
    responses.add(responses.GET, "https://a.example/p", body=body, content_type="text/html")
    clock = Clock()
    with Store(tmp_path / "scout.db") as store:
        Fetcher(cache=store, clock=clock).fetch("https://a.example/p")
    with Store(tmp_path / "scout.db") as store:
        doc = Fetcher(cache=store, clock=clock).fetch("https://a.example/p")
    assert doc.from_cache
    assert "survives restarts" in doc.text
    assert len(responses.calls) == 1


def test_runs_are_stored_listed_and_filtered_by_scout():
    with Store(":memory:") as store:
        first = store.add_run(RESULT)
        second, _ = store.record("gpu-watch", RESULT, [])
        assert store.get_run(first) == RESULT
        assert store.get_run(999) is None
        recent = store.recent_runs(limit=10)
        assert [summary.id for summary in recent] == [second, first]
        assert (recent[0].watch, recent[0].verified, recent[0].findings) == ("gpu-watch", 1, 3)
        assert [s.id for s in store.recent_runs(watch="gpu-watch")] == [second]


def test_capabilities_cache_respects_age():
    with Store(":memory:") as store:
        store.put_capabilities("key", {"model": "m"}, NOW)
        assert store.get_capabilities("key", newer_than=NOW - timedelta(hours=1)) == {"model": "m"}
        assert store.get_capabilities("key", newer_than=NOW + timedelta(seconds=1)) is None
        assert store.get_capabilities("other", newer_than=NOW - timedelta(days=1)) is None


def price(amount: str) -> Fact:
    return Fact(
        key="value|rtx 5090|price|shop.example",
        claim=f"RTX 5090 costs ${amount}",
        quote=f"Now ${amount}.",
        url="https://shop.example/5090",
        seen=NOW,
        entity="RTX 5090",
        value=f"${amount}",
        amount=Decimal(amount),
        currency="USD",
    )


def run_at(at):
    return replace(RESULT, started_at=at)


def test_the_ledger_raises_a_condition_once_per_episode_and_every_event():
    rules = [parse_rule("below 1800"), parse_rule("changed")]
    runs = [
        [Delta(Change.NEW, price("1799"))],  # under the limit: alert
        [Delta(Change.SAME, price("1799"), price("1799"))],  # still under: quiet
        [Delta(Change.CHANGED, price("1899"), price("1799"))],  # over: a change, and re-armed
        [Delta(Change.CHANGED, price("1750"), price("1899"))],  # under again: alert again
    ]
    raised = []
    with Store(":memory:") as store:
        for number, deltas in enumerate(runs, start=1):
            at = NOW + timedelta(hours=number)
            fired = triggers(rules, deltas, baseline=number == 1)
            _, ids = store.record(
                "gpu", run_at(at), deltas, raised=fired, cleared=cleared(rules, deltas)
            )
            raised.append([t.reason for t, i in zip(fired, ids, strict=True) if i is not None])

        assert raised == [
            ["below 1800: RTX 5090: $1799"],
            [],
            ["changed: RTX 5090: $1799 \N{RIGHTWARDS ARROW} $1899"],
            ["below 1800: RTX 5090: $1750", "changed: RTX 5090: $1899 \N{RIGHTWARDS ARROW} $1750"],
        ]
        assert [fact.amount for fact in store.facts("gpu")] == [Decimal("1750")]
        history = [o.amount for o in store.observations("gpu")]
        assert history == [Decimal(a) for a in ("1799", "1799", "1899", "1750")]

        store.record("gpu", run_at(NOW + timedelta(hours=5)), [Delta(Change.GONE, price("1750"))])
        assert store.facts("gpu") == []
        store.forget("gpu")
        assert store.alerts("gpu") == []


def test_a_run_is_recorded_with_its_changes_or_not_at_all():
    with Store(":memory:") as store:
        broken = Delta(Change.NEW, replace(price("1799"), seen=None))
        with pytest.raises(AttributeError):
            store.record("gpu", RESULT, [Delta(Change.NEW, price("1799")), broken])
        assert (store.last_runs("gpu"), store.facts("gpu")) == ([], [])

        run_id, _ = store.record("gpu", RESULT, [Delta(Change.NEW, price("1799"))])
        assert [i for i, _ in store.last_runs("gpu")] == [run_id]


def test_a_retired_watch_starts_over_but_keeps_its_value_history():
    rule = parse_rule("below 1800")
    with Store(":memory:") as store:
        delta = Delta(Change.NEW, price("1799"))
        store.record("gpu", RESULT, [delta], raised=triggers([rule], [delta], baseline=True))
        store.retire("gpu")
        assert store.facts("gpu") == []
        assert len(store.observations("gpu")) == 1
        _, ids = store.record(
            "gpu", RESULT, [delta], raised=triggers([rule], [delta], baseline=True)
        )
        assert ids[0] is not None  # the condition is raised again for the new question


def test_prune_keeps_recent_searches_and_the_page_versions_still_needed():
    old = NOW - timedelta(days=200)

    def page(url, text, at):
        return Document(url=url, status=FetchStatus.OK, fetched_at=at, text=text, content_hash=text)

    with Store(":memory:") as store:
        store.put_search("old", [], old)
        store.put_search("new", [], NOW)
        store.put_page(page("https://a.example", "v1", old))
        store.put_page(page("https://a.example", "v2", old + timedelta(days=1)))
        store.put_page(page("https://a.example", "v3", NOW))
        store.put_page(page("https://b.example", "only", old))
        store.put_page(page("https://c.example", "read", old))
        store.put_page(page("https://c.example", "later", old + timedelta(days=1)))
        read = replace(RESULT.sources[0], url="https://c.example", content_hash="read")
        store.add_run(replace(RESULT, sources=(read,), findings=()))

        assert store.prune(now=NOW) == 5  # a search, v1, v2, and the cached b and c pages
        assert store.get_search("old", newer_than=old) is None
        assert store.get_search("new", newer_than=old) == []
        kept = [h for h in ("v1", "v2", "v3", "only", "read", "later") if store.get_snapshot(h)]
        assert kept == ["v3", "only", "read", "later"]
        assert store.get_page("https://a.example") is not None
        assert store.prune(now=NOW) == 0


def test_undelivered_alerts_wait_for_a_notification_that_works():
    rule = parse_rule("new")
    with Store(":memory:") as store:
        for number, amount in enumerate(("1799", "1699"), start=1):
            delta = Delta(Change.NEW, price(amount))
            fired = triggers([rule], [delta], baseline=False)
            store.record("gpu", run_at(NOW + timedelta(hours=number)), [delta], raised=fired)
        first, second = store.undelivered("gpu", since=NOW)
        assert (first.url, first.quote) == ("https://shop.example/5090", "Now $1799.")

        store.mark_alerts([first.id], at=NOW, error="ntfy is down")
        store.mark_alerts([second.id], at=NOW)
        assert [a.id for a in store.undelivered("gpu", since=NOW)] == [first.id]
        assert store.undelivered("gpu", since=NOW + timedelta(hours=2)) == []  # too old to retry
        newest, oldest = store.alerts("gpu")
        assert (oldest.error, newest.delivered_at) == ("ntfy is down", NOW)


def test_what_runs_find_is_remembered_once_and_recalled_by_relevance():
    later = replace(RESULT, started_at=NOW + timedelta(days=1))
    with Store(":memory:") as store:
        first = store.add_run(RESULT)
        store.add_run(later)  # the same finding again: remembered once, seen later
        (memory,) = store.recall("how much does the RTX 5090 shop charge")
        assert (memory.claim, memory.url) == (
            "Shop sells it for $1,999",
            "https://shop.example/5090",
        )
        assert (memory.run_id, memory.first_seen, memory.last_seen) == (
            first,
            NOW,
            later.started_at,
        )
        # Only trusted findings are remembered: not the outlier, not the unverified quote.
        assert store.recall("$800") == []
        assert store.recall("free shipping") == []
        # The same page quoted with more text around it is the same finding; a new price is not.
        wider = replace(RESULT.findings[0], quote="Deal: Now $1,999 at Shop. Hurry.")
        cheaper = replace(RESULT.findings[0], claim="Shop sells it for $1,899", quote="Now $1,899.")
        store.add_run(replace(RESULT, findings=(wider, cheaper)))
        assert [m.claim for m in store.recall("shop sells")] == [
            "Shop sells it for $1,999",
            "Shop sells it for $1,899",
        ]
        # Whatever the question contains, it is not search syntax.
        assert store.recall('shop" OR (NEAR "x') != []
        assert store.recall("?!") == []


def test_existing_runs_are_remembered_when_the_database_is_upgraded(tmp_path):
    path = tmp_path / "scout.db"
    old = sqlite3.connect(path)
    for number, script in enumerate(_MIGRATIONS[:4], start=1):
        old.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
    old.execute(
        "INSERT INTO runs (goal, started_at, confidence, verified, findings, result) "
        "VALUES (?, ?, 'medium', 1, 3, ?)",
        (RESULT.goal, NOW.isoformat(), json.dumps(RESULT.to_dict())),
    )
    old.commit()
    old.close()
    with Store(path) as store:
        assert [m.claim for m in store.recall("shop sells")] == ["Shop sells it for $1,999"]


def test_a_fact_check_remembers_what_pages_state_and_leaves_site_tallies_alone():
    with Store(":memory:") as store:
        run_id = store.add_run(CHECK_RESULT)
        assert store.get_run(run_id) == CHECK_RESULT
        # The verified quotes with what they state: not the quote set aside, and never the
        # claims under test.
        remembered = store.recall("python 3.13 released october 2023 2024 added jit")
        assert sorted((m.claim, m.quote, m.run_id) for m in remembered) == sorted(
            (finding.claim, finding.quote, run_id) for finding in CHECK_RESULT.trusted
        )
        assert not {m.claim for m in remembered} & {c.claim for c in CHECK_RESULT.claims}

        # Its quotes were chosen for a claim under test: they say little about the sites.
        assert store.all_sites() == []
        store.add_run(RESULT)
        shop = store.site_records(["shop.example"])["shop.example"]
        assert (shop.findings, shop.verified) == (2, 1)
