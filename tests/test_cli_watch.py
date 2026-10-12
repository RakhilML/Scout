import io
import re
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from click.testing import CliRunner
from rich.console import Console

from scout.app import App
from scout.cli import main
from scout.cli._common import when
from scout.errors import ConfigError
from scout.monitor import runner
from scout.monitor.claims import compare, pages, ruling_keys
from scout.monitor.diff import Change, Delta, Fact
from scout.monitor.rules import Trigger, parse_rule
from scout.monitor.runner import WatchRun, run_lock
from scout.monitor.watches import WatchBook
from scout.store import Store
from tests.helpers import (
    AUDIT_RESULT,
    CHECK_RESULT,
    GONE,
    HAS_JIT,
    NOW,
    RELEASED_2023,
    FakeResearcher,
)
from tests.helpers import SAMPLE_RESULT as RESULT


def invoke(*args, stdin=None):
    return CliRunner().invoke(main, list(args), input=stdin, catch_exceptions=False)


def test_a_watch_from_add_to_remove(workspace):
    added = invoke(
        "watch", "add", "gpu", "cheapest RTX 5090", "--every", "6h", "--alert", "below 1800 USD"
    )
    assert added.exit_code == 0, added.output
    assert "Watching gpu every 6h" in added.output
    assert "name: gpu" in (workspace / "data" / "watches.yaml").read_text(encoding="utf-8")

    listed = invoke("watch", "list").output
    assert "below 1800 USD" in listed
    assert "never" in listed

    invoke("watch", "pause", "gpu")
    assert "paused" in invoke("watch", "list").output
    invoke("watch", "resume", "gpu")
    assert "active" in invoke("watch", "list").output

    shown = invoke("watch", "show", "gpu").output
    assert "cheapest RTX 5090" in shown
    assert "No alerts yet." in shown

    assert invoke("watch", "remove", "gpu", stdin="n\n").exit_code == 1  # declined
    feed = workspace / "data" / "feeds" / "gpu.xml"
    feed.parent.mkdir()
    feed.write_text("<feed/>", encoding="utf-8")
    assert invoke("watch", "remove", "gpu", "--yes").exit_code == 0
    assert "No watches yet" in invoke("watch", "list").output
    assert not feed.exists()


def test_a_watch_without_a_schedule_runs_daily_and_cron_works(workspace):
    assert "every 1d" in invoke("watch", "add", "daily", "Python news").output
    added = invoke("watch", "add", "morning", "Python news", "--cron", "0 9 * * *")
    assert 'on cron "0 9 * * *"' in added.output


def test_bad_watches_are_refused_with_the_reason(workspace):
    rule = invoke("watch", "add", "gpu", "goal", "--alert", "when it is cheap")
    assert rule.exit_code == 1
    assert "cannot understand the alert rule" in rule.output
    name = invoke("watch", "add", "GPU Watch", "goal")
    assert "lowercase" in name.output
    assert "no watch named 'nope'" in invoke("watch", "run", "nope").output
    assert not (workspace / "data" / "watches.yaml").exists()


def test_an_unusable_notification_url_is_refused_when_added(workspace):
    pytest.importorskip("apprise")
    refused = invoke("watch", "add", "gpu", "goal", "--notify", "nonsense://x")
    assert "not a notification URL" in refused.output
    assert not (workspace / "data" / "watches.yaml").exists()


def test_watch_run_reports_changes_alerts_and_delivery_trouble(workspace, monkeypatch):
    invoke("watch", "add", "gpu", "cheapest RTX 5090")
    price = Fact(
        key="value|rtx 5090|price|shop.example",
        claim="Shop sells the RTX 5090 for $1,799 [limited]",
        quote="Now $1,799.",
        url="https://shop.example/5090",
        seen=NOW,
        entity="RTX 5090",
        value="$1,799",
        amount=Decimal("1799"),
        currency="USD",
    )
    delta = Delta(Change.NEW, price)
    outcome = WatchRun(
        watch="gpu",
        run_id=7,
        result=RESULT,
        deltas=(delta, Delta(Change.SAME, price, price)),
        alerts=(Trigger(parse_rule("new"), delta, f"new: {price.claim}"),),
        baseline=False,
        notify_error="ntfy.sh unreachable",
    )
    monkeypatch.setattr("scout.cli.watch.run_watch", lambda app, watch, **options: outcome)
    result = invoke("watch", "run", "gpu")
    assert "gpu run 7: 1 new, 1 same" in result.output
    assert "new: Shop sells the RTX 5090 for $1,799 [limited]" in result.output  # not markup
    assert "notification failed: ntfy.sh unreachable" in result.output


def flat(text: str) -> str:
    """Output with its line wrapping undone."""
    return " ".join(text.split())


def test_a_claim_watch_is_added_listed_and_shown_with_its_rulings(workspace):
    added = invoke("watch", "add", "py", CHECK_RESULT.checked_text, "--check")
    assert added.exit_code == 0, added.output
    assert "Watching py every 1d (claims). Run it now: scout watch run py" in flat(added.output)
    assert "check: true" in (workspace / "data" / "watches.yaml").read_text(encoding="utf-8")
    listed = invoke("watch", "list").output
    assert "changed" in listed
    assert "new" not in listed

    with Store(workspace / "data" / "scout.db") as store:
        rulings, evidence = compare([], CHECK_RESULT, earlier={}, published={}, since=None)
        store.record("py", CHECK_RESULT, [*rulings, *evidence])
    shown = invoke("watch", "show", "py").output
    assert flat(shown).startswith(f"py (claims): {CHECK_RESULT.checked_text}")
    assert "alerts on: changed" in shown
    day = when(NOW, date_only=True)
    assert re.search(r"claim\W+ruling\W+since\W+quotes\W+noticed\W", shown)
    assert re.search(rf"refuted\W+{day}\W+1\W+0\W", shown)
    assert re.search(rf"supported\W+{day}\W+2\W+0\W", shown)


def test_a_claim_watch_run_says_how_each_ruling_moved(workspace, monkeypatch):
    invoke("watch", "add", "py", CHECK_RESULT.checked_text, "--check")
    released = Fact(
        key="claim|1",
        claim=RELEASED_2023,
        quote="Python 3.13.0 was released on October 7, 2024.",
        url="https://www.python.org/downloads/release/python-3130/",
        seen=NOW,
        entity=RELEASED_2023.removesuffix("."),
        value="refuted",
    )
    jit = replace(released, key="claim|2", claim=HAS_JIT, quote="It has a JIT.", value="disputed")
    gone = replace(jit, key="claim|3", claim="Python 3.13 is fast.", quote="It is fast.")
    held = replace(jit, value="supported")
    other = replace(jit, key="claim|4", claim="Python 3.13 removed the GIL.")
    misread = replace(jit, key="evidence|2|refutes|x", quote="It has no JIT yet.", value="refutes")
    rulings = (
        Delta(Change.CHANGED, released, replace(released, value="supported")),
        Delta(Change.NOTICED, held, held),
        Delta(Change.CHANGED, replace(gone, value="unclear"), replace(gone, value="supported")),
        Delta(Change.SAME, other, other),
    )
    reason = f"changed: {released.entity}: supported \N{RIGHTWARDS ARROW} refuted"
    outcome = WatchRun(
        watch="py",
        run_id=40,
        result=CHECK_RESULT,
        deltas=(
            *rulings,
            Delta(Change.NOTICED, replace(misread, noticed=True)),
            Delta(Change.GONE, replace(gone, key="evidence|3|supports|x", value="supports")),
        ),
        alerts=(Trigger(parse_rule("changed"), rulings[0], reason),),
        baseline=False,
        delivered=1,
        rulings=rulings,
    )
    monkeypatch.setattr("scout.cli.watch.run_watch", lambda app, watch, **options: outcome)
    printed = flat(invoke("watch", "run", "py").output)

    assert printed.startswith("py run 40: 4 claims checked, 2 changed, 1 noticed")
    assert (
        f"{reason} 1. Refuted (was supported): {RELEASED_2023} "
        '"Python 3.13.0 was released on October 7, 2024." (python.org) '
        f"2. Supported: {HAS_JIT} "
        'noticed, not alerted: refutes "It has no JIT yet." (python.org) '
        '3. Unclear (was supported): Python 3.13 is fast. no longer on python.org: "It is fast." '
        "4. Disputed: Python 3.13 removed the GIL. sent 1 alert(s)"
    ) in printed


def test_in_a_terminal_a_quotes_site_and_an_alert_open_the_page_at_the_quote(
    workspace, monkeypatch
):
    invoke("watch", "add", "py", CHECK_RESULT.checked_text, "--check")
    release = "https://www.python.org/downloads/release/python-3130/"
    released = Fact(
        key="claim|1",
        claim=RELEASED_2023,
        quote="Python 3.13.0 was released on October 7, 2024.",
        url=release,
        seen=NOW,
        entity=RELEASED_2023.removesuffix("."),
        value="refuted",
        anchor="text=Python%203.13.0%20was",
    )
    ruling = Delta(Change.CHANGED, released, replace(released, value="supported"))
    alert = Trigger(parse_rule("changed"), ruling, "changed: supported to refuted")
    with Store(workspace / "data" / "scout.db") as store:
        store.record("py", CHECK_RESULT, [ruling], raised=[alert])
    outcome = WatchRun(
        watch="py",
        run_id=40,
        result=CHECK_RESULT,
        deltas=(ruling,),
        alerts=(alert,),
        baseline=False,
        rulings=(ruling,),
    )
    monkeypatch.setattr("scout.cli.watch.run_watch", lambda app, watch, **options: outcome)
    terminal = Console(file=io.StringIO(), force_terminal=True, legacy_windows=False, width=200)
    monkeypatch.setattr("scout.cli.watch.out", terminal)

    invoke("watch", "run", "py")
    deep = f";{release}#:~:text=Python%203.13.0%20was\x1b\\"
    assert f"{deep}python.org\x1b]8;;" in terminal.file.getvalue()
    invoke("watch", "changes", "py")
    assert f"{deep}changed: supported to refuted" in terminal.file.getvalue()


PSF = "https://en.wikipedia.org/wiki/Python_Software_Foundation"
AUDITED = replace(
    AUDIT_RESULT,
    goal=f"citation audit: {PSF}",
    cites={source.index: source.url for source in AUDIT_RESULT.sources},
)


def baseline(result=AUDITED) -> WatchRun:
    rulings, evidence = compare([], result, earlier={}, published={}, since=None)
    cited = pages([], result, evidence)
    return WatchRun(
        watch="psf",
        run_id=61,
        result=result,
        deltas=(*rulings, *evidence, *cited),
        alerts=(),
        baseline=True,
        rulings=tuple(rulings),
        pages=tuple(cited),
    )


def test_a_citation_watch_is_added_and_shown_with_its_labels_and_dead_pages(workspace):
    added = invoke("watch", "add", "psf", PSF, "--cited")
    assert added.exit_code == 0, added.output
    assert "Watching psf every 1d (citations). Run it now: scout watch run psf" in flat(
        added.output
    )
    stored = (workspace / "data" / "watches.yaml").read_text(encoding="utf-8")
    assert ("check: true" in stored, "cited: true" in stored) == (True, True)
    assert "changed, dead" in invoke("watch", "list").output

    with Store(workspace / "data" / "scout.db") as store:
        store.record("psf", AUDITED, baseline().deltas)
    shown = invoke("watch", "show", "psf").output
    assert flat(shown).startswith(f"psf (citations): {PSF} runs every 1d alerts on: changed, dead")
    assert "4 claims: 2 contradicted, 1 not found, 1 unreadable" in shown
    day = when(NOW, date_only=True)
    assert re.search(r"Not backed\W+label\W+claim\W+cites\W+since\W", shown)
    # a claim may wrap inside its cell, but its row's first line always holds the cites and date
    assert re.search(
        rf"contradicted\W+Python 3\.13 was released[^\n]*\[1\] python\.org\W+{day}", shown
    )
    assert re.search(
        rf"unreadable\W+Python 3\.13 runs on iOS[^\n]*\[4\] example\.org\W+{day}", shown
    )
    assert re.search(r"Cited pages that could not be read\W+page\W+why\W+since\W", shown)
    assert re.search(rf"{GONE}\W+not found: HTTP 404\W+{day}", shown)


def test_a_citation_watchs_first_run_tallies_its_labels_and_lists_what_is_not_backed(
    workspace, monkeypatch
):
    invoke("watch", "add", "psf", PSF, "--cited")
    monkeypatch.setattr("scout.cli.watch.run_watch", lambda app, watch, **options: baseline())
    printed = flat(invoke("watch", "run", "psf").output)

    assert printed.startswith(
        "psf run 61: 4 claims checked: 2 contradicted, 1 not found, 1 unreadable "
        "first run: this is what later runs are compared with "
        f"1. Contradicted [1]: {RELEASED_2023} "
        '"Python 3.13.0 was released on October 7, 2024." (python.org) '
        "2. Contradicted [2]: Python 3.13 removed the global interpreter lock by default. "
    )
    assert printed.endswith(
        "3. Not found [3]: Python 3.13's JIT makes it 40% faster than Python 3.12. "
        "4. Unreadable [4]: Python 3.13 runs on iOS as a tier 3 platform. "
        "could not read [4] example.org (not found: HTTP 404)"
    )

    unclear = AUDITED.claims[2]
    many = replace(
        AUDITED,
        claims=tuple(
            replace(unclear, claim=f"Python 3.{n} has {n} new modules.") for n in range(23)
        ),
    )
    monkeypatch.setattr("scout.cli.watch.run_watch", lambda app, watch, **options: baseline(many))
    printed = flat(invoke("watch", "run", "psf").output)
    assert printed.count("Not found [3]") == 20
    assert printed.endswith(
        "20. Not found [3]: Python 3.19 has 19 new modules. and 3 more: scout show 61"
    )


def test_a_citation_watchs_later_run_says_what_it_kept_and_what_moved(workspace, monkeypatch):
    invoke("watch", "add", "psf", PSF, "--cited")
    released, removed, faster, ios = AUDITED.claims
    jit, gone = AUDITED.sources[2], AUDITED.sources[3]
    later = replace(
        AUDITED,
        sources=(
            *AUDITED.sources[:2],
            replace(jit, snippet_only=True, status="not_found", error="HTTP 404"),
            replace(gone, snippet_only=False, status="ok", text="It runs on iOS."),
        ),
        claims=(replace(released, kept=True), removed, replace(faster, kept=True), ios),
    )
    first = baseline()
    assert ruling_keys(later) == [delta.fact.key for delta in first.rulings]
    was = [delta.fact for delta in first.rulings]
    rulings = (
        Delta(Change.SAME, was[0], was[0]),
        Delta(Change.CHANGED, replace(was[1], value="not found"), was[1]),
        Delta(Change.SAME, was[2], was[2]),
        Delta(Change.NEW, replace(was[3], value="backed", quote="It runs on iOS.", url=GONE)),
    )
    left = replace(was[1], key=f"evidence|{was[1].key.removeprefix('claim|')}|refutes|x")
    jit_page, ios_page = (delta.fact for delta in first.pages[2:])
    dead = replace(jit_page, value="not found: HTTP 404", quote="Python 3.13 ships a JIT.")
    cited = (
        Delta(Change.CHANGED, dead, jit_page),
        Delta(Change.CHANGED, replace(ios_page, value="read"), replace(ios_page, missed=True)),
    )
    changed = (
        f"changed: {removed.claim.removesuffix('.')}: contradicted \N{RIGHTWARDS ARROW} not found"
    )
    outcome = WatchRun(
        watch="psf",
        run_id=62,
        result=later,
        deltas=(*rulings, Delta(Change.GONE, left), *cited),
        alerts=(
            Trigger(parse_rule("changed"), rulings[1], f"{changed} (no longer on docs.python.org)"),
            Trigger(parse_rule("dead"), cited[0], f"dead: {jit.url} (not found: HTTP 404)"),
        ),
        baseline=False,
        rulings=rulings,
        pages=cited,
        left=1,
    )
    monkeypatch.setattr("scout.cli.watch.run_watch", lambda app, watch, **options: outcome)
    printed = flat(invoke("watch", "run", "psf").output)

    assert printed == (
        "psf run 62: 4 claims checked (2 kept, 2 judged): 1 changed, 1 new, 1 left the text, "
        "1 cited page dead, 1 cited page back "
        f"\N{BELL} {changed} (no longer on docs.python.org) "
        f"\N{BELL} dead: {jit.url} (not found: HTTP 404) "
        'it said: "Python 3.13 ships a JIT." '
        f"back: {GONE}, cited for: {ios.claim} "
        f"2. Not found (was contradicted) [2]: {removed.claim} "
        f'no longer on docs.python.org: "{was[1].quote}" '
        f'4. New, backed [4]: {ios.claim} "It runs on iOS." (example.org)'
    )


@pytest.mark.parametrize(
    ("goal", "kind", "result"),
    [
        ("cheapest RTX 5090", (), replace(RESULT, goal="cheapest RTX 5090")),
        (CHECK_RESULT.checked_text, ("--check",), CHECK_RESULT),
        (PSF, ("--cited",), AUDITED),
    ],
    ids=["research", "claims", "citations"],
)
def test_a_watch_removed_and_added_again_starts_a_new_baseline(
    workspace, monkeypatch, goal, kind, result
):
    monkeypatch.setattr(App, "researcher", lambda self, **options: FakeResearcher(result))
    first = "first run: this is what later runs are compared with"
    invoke("watch", "add", "w", goal, *kind)
    assert first in flat(invoke("watch", "run", "w").output)
    assert first not in flat(invoke("watch", "run", "w").output)

    assert invoke("watch", "remove", "w", "--yes").exit_code == 0
    invoke("watch", "add", "w", goal, *kind)
    assert "never" in invoke("watch", "list").output  # no last run until it runs again
    assert invoke("history", "--watch", "w").output.count("medium") == 2  # the old runs stay
    assert first in flat(invoke("watch", "run", "w").output)
    assert "never" not in invoke("watch", "list").output
    assert first not in flat(invoke("watch", "run", "w").output)


@pytest.mark.parametrize("failing", ["settings", "store"])
def test_a_remove_that_fails_leaves_the_watch_listed_to_remove_again(
    workspace, monkeypatch, failing
):
    invoke("watch", "add", "gpu", "cheapest RTX 5090")
    data = workspace / "data"
    feed = data / "feeds" / "gpu.xml"
    feed.parent.mkdir(parents=True)
    feed.write_text("<feed/>", encoding="utf-8")
    with Store(data / "scout.db") as store:
        store.record("gpu", RESULT, [])

    def broken(*args, **options):
        raise ConfigError("SCOUT_ARCHIVE must be off or a URL")

    with monkeypatch.context() as patched:
        if failing == "settings":  # the App cannot be made
            patched.setattr("scout.cli.watch.App", broken)
        else:
            patched.setattr(Store, "forget", broken)
        failed = invoke("watch", "remove", "gpu", "--yes")
    assert failed.exit_code == 1
    assert "SCOUT_ARCHIVE must be off or a URL" in flat(failed.output)
    assert [watch.name for watch in WatchBook(data / "watches.yaml").load()] == ["gpu"]
    assert feed.exists()

    assert invoke("watch", "remove", "gpu", "--yes").exit_code == 0  # once the cause is fixed
    assert "No watches yet" in invoke("watch", "list").output
    assert not feed.exists()
    with Store(data / "scout.db") as store:
        assert store.last_runs("gpu") == []  # added again, it starts anew


def test_a_watch_removed_after_watch_run_read_it_records_nothing(workspace, monkeypatch):
    monkeypatch.setattr(App, "researcher", lambda self, **options: FakeResearcher(RESULT))
    invoke("watch", "add", "gpu", RESULT.goal)
    data = workspace / "data"
    lock = runner.run_lock

    def removed_first(data_dir, name, **options):  # removed before the run took its lock
        WatchBook(data / "watches.yaml").remove(name)
        return lock(data_dir, name, **options)

    monkeypatch.setattr(runner, "run_lock", removed_first)
    ran = invoke("watch", "run", "gpu")
    assert ran.exit_code == 1
    assert "no watch named 'gpu'" in ran.output
    with Store(data / "scout.db") as store:
        assert (store.recent_runs(watch="gpu"), store.facts("gpu")) == ([], [])
    assert not (data / "feeds" / "gpu.xml").exists()


def test_a_watch_is_not_removed_while_it_runs(workspace, monkeypatch):
    invoke("watch", "add", "gpu", "cheapest RTX 5090")
    feed = workspace / "data" / "feeds" / "gpu.xml"
    feed.parent.mkdir(parents=True)
    feed.write_text("<feed/>", encoding="utf-8")
    price = Fact(
        key="value|rtx 5090|price|shop.example",
        claim="RTX 5090 costs $1,999",
        quote="Now $1,999.",
        url="https://shop.example/5090",
        seen=NOW,
    )
    with Store(workspace / "data" / "scout.db") as store:
        store.record("gpu", RESULT, [Delta(Change.NEW, price)])

    with run_lock(workspace / "data", "gpu"):  # a run in progress
        refused = invoke("watch", "remove", "gpu", "--yes")
    assert refused.exit_code == 1
    assert "watch 'gpu' is running: remove it when the run ends" in flat(refused.output)
    assert [watch.name for watch in WatchBook(workspace / "data" / "watches.yaml").load()] == [
        "gpu"
    ]
    assert feed.exists()
    with Store(workspace / "data" / "scout.db") as store:
        assert store.facts("gpu") == [price]

    tried = []  # a run that starts while the watch is removed
    forget = Store.forget

    def forgetting(self, name):
        with (
            pytest.raises(ConfigError, match="'gpu' is already running"),
            run_lock(workspace / "data", name),
        ):
            pass
        tried.append(name)
        forget(self, name)

    monkeypatch.setattr(Store, "forget", forgetting)
    assert invoke("watch", "remove", "gpu", "--yes").exit_code == 0
    assert tried == ["gpu"]
    assert not feed.exists()
    with run_lock(workspace / "data", "gpu"):  # free again
        pass


def test_watch_add_tells_the_default_alert_rules_of_each_kind(workspace):
    told = flat(invoke("watch", "add", "--help").output)
    assert "[default: new, changed; with --check: changed; with --cited: changed, dead]" in told
    assert "[default: new, changed]" not in told


def test_a_trend_csv_is_written_whole_with_the_same_rows(workspace, monkeypatch):
    invoke("watch", "add", "gpu", "cheapest RTX 5090")
    with Store(workspace / "data" / "scout.db") as store:
        for hours, amount in enumerate(("1999", "1849")):
            fact = Fact(
                key="value|rtx 5090|price|shop.example",
                claim="RTX 5090 costs money",
                quote=f"Now ${amount}.",
                url="https://shop.example/5090",
                seen=NOW,
                entity="RTX 5090",
                value=f"${amount}",
                amount=Decimal(amount),
                currency="USD",
            )
            run = replace(RESULT, started_at=NOW + timedelta(hours=hours))
            store.record("gpu", run, [Delta(Change.SAME, fact, fact)])
    target = workspace / "gpu.csv"
    assert invoke("watch", "trend", "gpu", "--csv", str(target)).exit_code == 0
    written = target.read_bytes()
    assert written.count(b"\r\n") == 3  # the header and two rows, as csv writes them
    assert b"\r\r\n" not in written
    assert written.startswith(b"observed_at,label,amount,currency,unit,run_id,key\r\n")

    def broken(observations, out):
        out.write("observed_at,label\r\n")
        raise OSError("the disk is full")

    monkeypatch.setattr("scout.cli.watch.write_csv", broken)
    failed = CliRunner().invoke(main, ["watch", "trend", "gpu", "--csv", str(target)])
    assert isinstance(failed.exception, OSError)
    assert target.read_bytes() == written  # the earlier file, whole
    assert list(workspace.glob(".gpu.csv*")) == []


def test_a_citation_watch_run_says_when_a_cited_page_failed_once(workspace, monkeypatch):
    invoke("watch", "add", "psf", PSF, "--cited")
    first = baseline()
    kept = replace(AUDITED, claims=tuple(replace(claim, kept=True) for claim in AUDITED.claims))
    jit = first.pages[2].fact
    pages_now = (*first.pages[:2], Delta(Change.SAME, replace(jit, missed=True), jit))
    outcome = replace(first, result=kept, baseline=False, pages=pages_now, deltas=pages_now)
    monkeypatch.setattr("scout.cli.watch.run_watch", lambda app, watch, **options: outcome)
    printed = flat(invoke("watch", "run", "psf").output)
    assert "1 cited page could not be read this time (not alerted unless it fails again)" in printed
    assert "the model was not asked again" not in printed

    quiet = replace(outcome, pages=first.pages[:3], deltas=first.pages[:3])
    monkeypatch.setattr("scout.cli.watch.run_watch", lambda app, watch, **options: quiet)
    assert "no cited page changed, so the model was not asked again" in flat(
        invoke("watch", "run", "psf").output
    )
