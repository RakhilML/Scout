import json
from dataclasses import replace
from datetime import timedelta

import pytest
from click.testing import CliRunner

from scout.app import App
from scout.cli import main
from scout.errors import ScoutError
from scout.settings import Settings, load_settings
from scout.web.fetch import Document, FetchStatus
from tests.helpers import NOW
from tests.helpers import SAMPLE_RESULT as RESULT

SHOP = RESULT.sources[0].url


@pytest.fixture
def app(tmp_path):
    with App(Settings(data_dir=tmp_path, reports_dir=tmp_path / "reports")) as app:
        yield app


def stored_run(app: App) -> int:
    """SAMPLE_RESULT, with the shop page it read kept as a snapshot."""
    page = Document(
        url=SHOP,
        status=FetchStatus.OK,
        fetched_at=NOW,
        text="Now $1,999 at Shop.",
        content_hash="h1",
    )
    app.store.put_page(page)
    shop = replace(RESULT.sources[0], content_hash="h1")
    return app.store.add_run(replace(RESULT, sources=(shop, *RESULT.sources[1:])))


def test_findings_are_rated_by_their_number_in_the_report(app):
    run_id = stored_run(app)
    good = app.rate(run_id, 1, "good")
    assert (good.claim, good.url, good.trusted) == ("Shop sells it for $1,999", SHOP, True)
    outlier = app.rate(run_id, 2, "good", note="the $800 listing is real")
    assert (outlier.claim, outlier.trusted) == ("Shop Z sells it for $800", False)
    app.rate(run_id, 2, "bad")  # a second verdict replaces the first
    assert [(r.number, r.verdict, r.note) for r in app.store.ratings()] == [
        (1, "good", None),
        (2, "bad", None),
    ]
    with pytest.raises(ScoutError, match="findings 1 to 3, not 4"):
        app.rate(run_id, 4, "good")
    with pytest.raises(ScoutError, match="no run with id 99"):
        app.rate(99, 1, "good")


def test_a_fact_rated_wrong_is_no_longer_recalled(app):
    run_id = stored_run(app)
    assert app.store.recall("shop sells")
    app.rate(run_id, 1, "bad", note="that price is for the refurbished card")
    assert app.store.recall("shop sells") == []


def test_a_fact_rated_wrong_stays_forgotten_until_rated_right(app):
    run_id = stored_run(app)
    app.rate(run_id, 1, "bad")
    app.store.add_run(replace(RESULT, started_at=NOW + timedelta(days=1)))  # found again
    assert app.store.recall("shop sells") == []
    assert app.store.site_records(["shop.example"])["shop.example"].rated_bad == 1

    app.rate(run_id, 1, "good")
    (memory,) = app.store.recall("shop sells")
    assert (memory.claim, memory.run_id, memory.first_seen) == (
        "Shop sells it for $1,999",
        run_id,
        NOW,
    )
    assert memory.last_seen == NOW + timedelta(days=1)  # seen again while it was hidden
    app.rate(run_id, 1, "bad")
    app.store.forget_site("shop.example")
    app.rate(run_id, 1, "good")
    assert app.store.site_records(["shop.example"])["shop.example"].rated_bad == 0


def test_ratings_become_what_an_eval_case_expects(app):
    run_id = stored_run(app)
    app.rate(run_id, 1, "good")
    app.rate(run_id, 3, "bad")
    case = app.case_from_run(run_id, name="rtx")
    assert case.expect_facts == ("Now $1,999 at Shop.",)
    assert case.expect_untrusted == ("Free shipping",)


def test_rate_and_export_from_the_command_line(workspace):
    with App(load_settings()) as app:
        run_id = stored_run(app)
    runner = CliRunner()
    rated = runner.invoke(main, ["rate", str(run_id), "1", "bad", "--note", "refurbished"])
    assert "Rated bad: Shop sells it for $1,999" in rated.output
    assert "forgotten" in rated.output
    assert "refurbished" in runner.invoke(main, ["ratings"]).output

    dataset = workspace / "ratings.jsonl"
    runner.invoke(main, ["ratings", "--export", str(dataset)])
    (line,) = dataset.read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert (record["verdict"], record["goal"], record["trusted"]) == ("bad", RESULT.goal, True)
    assert runner.invoke(main, ["rate", str(run_id), "1", "maybe"]).exit_code == 2


def test_a_rating_of_an_untrusted_finding_hides_nothing(app):
    run_id = stored_run(app)
    app.rate(run_id, 3, "bad")  # "Free shipping": never trusted, never remembered
    app.store.put_rating(replace(app.store.ratings()[0], number=9, quote=""))
    app.store.add_run(replace(RESULT, started_at=NOW + timedelta(days=1)))
    assert [m.claim for m in app.store.recall("shop sells")] == ["Shop sells it for $1,999"]
