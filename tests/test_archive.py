"""The Wayback Machine client: its index asked for a dead page's newest working copies, and a
copy read as the page served it; and the working copy chosen among them."""

from datetime import UTC, datetime

import pytest
import requests
import responses
from responses import matchers

from scout.app import App
from scout.errors import ConfigError, FetchError
from scout.settings import Settings
from scout.web.archive import (
    CDX,
    Copy,
    Snapshot,
    Wayback,
    in_archive,
    make_archive,
    snapshot_of,
    working,
)
from scout.web.fetch import Document, FetchConfig, Fetcher, FetchStatus
from scout.web.soft404 import error_page
from tests.helpers import NOW, FakeArchive

PAGE = "https://developers.google.com/speed/pagespeed/service"
TAKEN = datetime(2023, 1, 20, 23, 40, 50, tzinfo=UTC)
REPLAY = f"https://web.archive.org/web/20230120234050/{PAGE}"
RAW = f"https://web.archive.org/web/20230120234050id_/{PAGE}"
HEADER = ["timestamp", "original"]
ASKED = {
    "url": PAGE,
    "output": "json",
    "fl": "timestamp,original",
    "filter": "statuscode:200",
    "limit": "-1",
    "fastLatest": "true",
}
TURNED_OFF = "PageSpeed Service was turned off on August 3rd, 2015."


@pytest.fixture
def wayback():
    with Fetcher(FetchConfig(retries=0)) as fetcher:
        yield Wayback(fetcher)


@responses.activate
def test_the_index_is_asked_for_the_newest_copies_the_page_served_whole(wayback):
    responses.add(
        responses.GET,
        CDX,
        json=[HEADER, ["20230120234050", PAGE]],
        match=[matchers.query_param_matcher(ASKED)],
    )
    before = datetime(2015, 1, 1, tzinfo=UTC)
    responses.add(
        responses.GET,
        CDX,
        json=[
            HEADER,
            ["20141231080000", "http://developers.google.com:80/speed/pagespeed/service"],
        ],
        match=[matchers.query_param_matcher({**ASKED, "to": "20150101000000"})],
    )

    (snapshot,) = wayback.recent(f"{PAGE}#how-it-works")
    assert snapshot == Snapshot(PAGE, TAKEN)
    assert (snapshot.page, snapshot.raw, snapshot.taken_on) == (REPLAY, RAW, "2023-01-20")
    (earlier,) = wayback.recent(PAGE, before=before)
    assert earlier.page == (
        "https://web.archive.org/web/20141231080000/"
        "http://developers.google.com:80/speed/pagespeed/service"
    )
    assert responses.calls[0].request.headers["Accept"] == "application/json"


@responses.activate
def test_several_copies_come_newest_first_without_the_rows_it_cannot_read(wayback):
    older = datetime(2021, 5, 14, 9, 0, tzinfo=UTC)
    responses.add(
        responses.GET,
        CDX,
        json=[HEADER, ["20210514090000", PAGE], ["2022", PAGE], ["20230120234050", PAGE]],
        match=[matchers.query_param_matcher({**ASKED, "limit": "-3"})],
    )
    assert wayback.recent(PAGE, count=3) == [Snapshot(PAGE, TAKEN), Snapshot(PAGE, older)]


@pytest.mark.parametrize(
    "answer",
    [[], [HEADER], [HEADER, ["2023", PAGE]], [HEADER, ["20230120234050"]], {"rows": []}],
    ids=["nothing", "only the header", "a short timestamp", "a short row", "not a list"],
)
@responses.activate
def test_no_copy_lists_nothing(wayback, answer):
    responses.add(responses.GET, CDX, json=answer)
    assert wayback.recent(PAGE) == []


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ({"status": 429}, "HTTP 429"),
        ({"body": requests.ConnectTimeout()}, "no response within 90s"),
        ({"body": "<html>Wayback Machine is down</html>"}, "not JSON"),
    ],
    ids=["too many requests", "timeout", "not json"],
)
@responses.activate
def test_an_archive_that_does_not_answer_says_why(wayback, reply, message):
    responses.add(responses.GET, CDX, **reply)
    with pytest.raises(FetchError, match=f"^{message}$"):
        wayback.recent(PAGE)


@responses.activate
def test_a_copy_is_read_as_the_page_served_it(wayback):
    body = f"<html><body><article><p>{TURNED_OFF} " + "It was a free service. " * 12
    responses.add(
        responses.GET, RAW, body=f"{body}</p></article></body></html>", content_type="text/html"
    )
    document = wayback.read(Snapshot(PAGE, TAKEN))
    assert document.status is FetchStatus.OK
    assert TURNED_OFF in document.text
    assert [call.request.url for call in responses.calls] == [RAW]


def test_a_wayback_address_tells_its_page_and_day():
    assert snapshot_of(REPLAY) == Snapshot(PAGE, TAKEN)
    assert snapshot_of(RAW) == Snapshot(PAGE, TAKEN)
    for other in (PAGE, "https://web.archive.org/web/2015/" + PAGE, "https://archive.ph/abc"):
        assert snapshot_of(other) is None
    assert in_archive(REPLAY)
    assert in_archive("https://web.archive.org/web/2015/" + PAGE)
    assert not in_archive(PAGE)
    assert Copy(Snapshot(PAGE, TAKEN), holds=True, anchor="text=PageSpeed").link == (
        f"{REPLAY}#:~:text=PageSpeed"
    )


def test_the_archive_setting():
    with Fetcher() as fetcher:
        assert isinstance(make_archive("wayback", fetcher), Wayback)
        assert make_archive("off", fetcher) is None
        with pytest.raises(ConfigError, match="SCOUT_ARCHIVE must be wayback or off"):
            make_archive("archive.today", fetcher)


def test_a_bad_archive_setting_opens_nothing(tmp_path):
    with pytest.raises(ConfigError, match="SCOUT_ARCHIVE must be wayback or off"):
        App(Settings(data_dir=tmp_path, archive="bogus"))
    assert not (tmp_path / "scout.db").exists()


DEPOT = "https://news.example.com/2019/depot"
OPENED = "The depot opened in 1899. " + "It served the line for decades. " * 8
MISSING = "Page not found. Sorry, we could not find what you were looking for. " * 4
DAYS = [datetime(2023, 3, day, tzinfo=UTC) for day in (3, 2, 1)]  # newest first


def test_the_working_copy_passes_over_archived_error_pages():
    archive = FakeArchive({DEPOT: [(DAYS[2], OPENED), (DAYS[0], MISSING), (DAYS[1], OPENED)]})
    found = working(archive, DEPOT, rejected=error_page)
    assert found is not None
    snapshot, document = found
    assert (snapshot, document.text) == (Snapshot(DEPOT, DAYS[1]), OPENED)
    assert archive.web.fetched == [Snapshot(DEPOT, day).raw for day in DAYS[:2]]
    assert archive.asked == [(DEPOT, None)]


def test_of_copies_that_all_look_like_error_pages_the_newest_is_the_working_one():
    archive = FakeArchive({DEPOT: [(day, MISSING) for day in DAYS]})
    found = working(archive, DEPOT, rejected=error_page)
    assert found is not None
    assert found[0] == Snapshot(DEPOT, DAYS[0])
    assert len(archive.web.fetched) == 3
    assert working(FakeArchive({}), DEPOT, rejected=error_page) is None


def test_an_archive_that_does_not_serve_an_older_copy_keeps_nothing():
    throttled = Document(url="x", status=FetchStatus.BLOCKED, fetched_at=NOW, error="HTTP 429")
    archive = FakeArchive({DEPOT: [(DAYS[0], MISSING), (DAYS[1], throttled)]})
    with pytest.raises(FetchError, match="HTTP 429"):
        working(archive, DEPOT, rejected=error_page)
