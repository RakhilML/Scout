import xml.etree.ElementTree as ET
from dataclasses import replace

from scout.monitor.feed import atom, write_feed
from scout.monitor.watches import Watch
from scout.store import AlertRecord
from tests.helpers import NOW

ATOM = "{http://www.w3.org/2005/Atom}"
WATCH = Watch(name="gpu", goal="cheapest RTX 5090 <in stock> & cheap", every="6h")
ALERT = AlertRecord(
    id=3,
    watch="gpu",
    run_id=9,
    created_at=NOW,
    reason="below 1800 USD: RTX 5090 & co: $1,799",
    url="https://shop.example/5090?a=1&b=2",
    quote="Now <b>$1,799</b>.",
    delivered_at=None,
    error=None,
)


def test_alerts_become_atom_entries():
    feed = ET.fromstring(atom(WATCH, [ALERT], now=NOW))
    assert feed.findtext(f"{ATOM}subtitle") == WATCH.goal
    (entry,) = feed.findall(f"{ATOM}entry")
    assert entry.findtext(f"{ATOM}title") == ALERT.reason
    assert entry.find(f"{ATOM}link").get("href") == ALERT.url
    assert entry.findtext(f"{ATOM}summary") == ALERT.quote


def test_entry_ids_are_stable_and_distinct():
    ids = [
        ET.fromstring(atom(WATCH, [alert], now=NOW)).findtext(f"{ATOM}entry/{ATOM}id")
        for alert in (ALERT, ALERT, replace(ALERT, id=4))
    ]
    assert ids[0] == ids[1] != ids[2]
    assert ids[0].startswith("urn:uuid:")


def test_text_xml_cannot_carry_is_left_out():
    odd = replace(ALERT, quote="Now\x00 $1,799\x1b.", reason="below\x0b 1800")
    entry = ET.fromstring(atom(WATCH, [odd], now=NOW)).find(f"{ATOM}entry")
    assert entry.findtext(f"{ATOM}summary") == "Now $1,799."
    assert entry.findtext(f"{ATOM}title") == "below 1800"


def test_only_web_pages_are_linked(tmp_path):
    odd = replace(ALERT, url="javascript:alert(1)")
    path = tmp_path / "feeds" / "gpu.xml"
    write_feed(path, WATCH, [odd], now=NOW)
    entry = ET.fromstring(path.read_text(encoding="utf-8")).find(f"{ATOM}entry")
    assert entry.find(f"{ATOM}link") is None
