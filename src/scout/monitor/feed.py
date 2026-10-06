"""A watch's alerts as an Atom feed: subscribe to a watch in any feed reader."""

from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from scout import __version__
from scout.files import write_atomic
from scout.monitor.watches import Watch
from scout.store import AlertRecord

FEED_ENTRIES = 50
_ATOM = "http://www.w3.org/2005/Atom"
_NOT_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def atom(watch: Watch, alerts: Sequence[AlertRecord], *, now: datetime) -> str:
    """The feed of *alerts* (newest first). Entry ids stay the same however often it is written."""
    ET.register_namespace("", _ATOM)
    feed = ET.Element(f"{{{_ATOM}}}feed")
    _add(feed, "id", _id(f"scout:watch:{watch.name}"))
    _add(feed, "title", f"Scout: {watch.name}")
    _add(feed, "subtitle", watch.goal)
    _add(feed, "updated", (alerts[0].created_at if alerts else now).isoformat())
    _add(feed, "generator", f"Scout {__version__}")
    _add(ET.SubElement(feed, f"{{{_ATOM}}}author"), "name", "Scout")
    for alert in alerts:
        entry = ET.SubElement(feed, f"{{{_ATOM}}}entry")
        _add(entry, "id", _id(f"scout:watch:{watch.name}:alert:{alert.id}"))
        _add(entry, "title", alert.reason)
        _add(entry, "updated", alert.created_at.isoformat())
        if alert.url.startswith(("https://", "http://")):
            ET.SubElement(entry, f"{{{_ATOM}}}link", href=alert.url)
        _add(entry, "summary", alert.quote or alert.reason)
    return ET.tostring(feed, encoding="unicode", xml_declaration=True)


def write_feed(path: Path, watch: Watch, alerts: Sequence[AlertRecord], *, now: datetime) -> None:
    write_atomic(path, atom(watch, alerts, now=now))


def _add(parent: ET.Element, tag: str, text: str) -> None:
    ET.SubElement(parent, f"{{{_ATOM}}}{tag}").text = _NOT_XML.sub("", text)


def _id(name: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, name).urn
