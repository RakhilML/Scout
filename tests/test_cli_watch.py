from decimal import Decimal

import pytest
from click.testing import CliRunner

from scout.cli import main
from scout.monitor.diff import Change, Delta, Fact
from scout.monitor.rules import Trigger, parse_rule
from scout.monitor.runner import WatchRun
from tests.helpers import NOW
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
