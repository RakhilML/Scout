"""SQLite persistence: page and search caches, runs, findings and the facts ledger.

One file (``data_dir/scout.db``) holds everything. The schema evolves through the ordered
``_MIGRATIONS`` list; ``PRAGMA user_version`` records how many have been applied.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from scout.monitor.diff import Change, Delta, Fact
from scout.monitor.rules import Trigger
from scout.research.rank import tokenize
from scout.research.reputation import SITE_FAILURES, SiteRecord
from scout.research.results import CHECK_KIND, Finding, RunResult, Verdict
from scout.research.verify import quoted_in
from scout.textutil import fold
from scout.web.domains import hostname
from scout.web.fetch import Document
from scout.web.search import SearchHit

_MIGRATIONS: tuple[str, ...] = (
    # 1: caches that make runs resumable and repeat runs cheap
    """
    CREATE TABLE pages (
        url TEXT PRIMARY KEY,
        fetched_at TEXT NOT NULL,
        document TEXT NOT NULL
    );
    CREATE TABLE searches (
        key TEXT PRIMARY KEY,
        fetched_at TEXT NOT NULL,
        hits TEXT NOT NULL
    );
    """,
    # 2: run history and what each model can do
    """
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY,
        scout TEXT,
        goal TEXT NOT NULL,
        started_at TEXT NOT NULL,
        confidence TEXT NOT NULL,
        verified INTEGER NOT NULL,
        findings INTEGER NOT NULL,
        result TEXT NOT NULL
    );
    CREATE INDEX runs_by_scout ON runs (scout, started_at);
    CREATE TABLE models (
        key TEXT PRIMARY KEY,
        probed_at TEXT NOT NULL,
        capabilities TEXT NOT NULL
    );
    """,
    # 3: every page version read, once, by content hash (replays, evals, change detection)
    """
    CREATE TABLE snapshots (
        content_hash TEXT PRIMARY KEY,
        url TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        document TEXT NOT NULL
    );
    CREATE INDEX snapshots_by_url ON snapshots (url, fetched_at);
    """,
    # 4: the facts ledger of each watch, value history, and alerts
    """
    CREATE TABLE facts (
        watch TEXT NOT NULL,
        key TEXT NOT NULL,
        status TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        fact TEXT NOT NULL,
        PRIMARY KEY (watch, key)
    );
    CREATE TABLE observations (
        watch TEXT NOT NULL,
        key TEXT NOT NULL,
        run_id INTEGER NOT NULL,
        observed_at TEXT NOT NULL,
        label TEXT NOT NULL,
        amount TEXT NOT NULL,
        currency TEXT,
        unit TEXT
    );
    CREATE INDEX observations_by_fact ON observations (watch, key, observed_at);
    CREATE TABLE alerts (
        id INTEGER PRIMARY KEY,
        watch TEXT NOT NULL,
        key TEXT NOT NULL,
        run_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        reason TEXT NOT NULL,
        url TEXT NOT NULL,
        quote TEXT,
        holding INTEGER NOT NULL,
        delivered_at TEXT,
        error TEXT
    );
    -- A condition (price below X) is raised once while it holds: one holding alert per key.
    CREATE UNIQUE INDEX alerts_holding ON alerts (watch, key) WHERE holding = 1;
    CREATE INDEX alerts_by_watch ON alerts (watch, created_at);
    """,
    # 5: what Scout knows: every trusted finding once per page and quote, searchable
    """
    CREATE TABLE knowledge (
        id INTEGER PRIMARY KEY,
        url TEXT NOT NULL,
        quote TEXT NOT NULL,
        claim TEXT NOT NULL,
        goal TEXT NOT NULL,
        run_id INTEGER NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        UNIQUE (url, quote)
    );
    CREATE VIRTUAL TABLE knowledge_index USING fts5(
        claim, quote, goal,
        content = 'knowledge', content_rowid = 'id',
        tokenize = 'porter unicode61 remove_diacritics 2'
    );
    CREATE TRIGGER knowledge_added AFTER INSERT ON knowledge BEGIN
        INSERT INTO knowledge_index (rowid, claim, quote, goal)
        VALUES (new.id, new.claim, new.quote, new.goal);
    END;
    CREATE TRIGGER knowledge_removed AFTER DELETE ON knowledge BEGIN
        INSERT INTO knowledge_index (knowledge_index, rowid, claim, quote, goal)
        VALUES ('delete', old.id, old.claim, old.quote, old.goal);
    END;
    -- What earlier runs found (trusted: verified and not flagged), oldest first.
    INSERT INTO knowledge (url, quote, claim, goal, run_id, first_seen, last_seen)
    SELECT json_extract(source.value, '$.url'), json_extract(finding.value, '$.quote'),
           json_extract(finding.value, '$.claim'), runs.goal, runs.id, runs.started_at,
           runs.started_at
    FROM runs
    JOIN json_each(runs.result, '$.findings') AS finding
    JOIN json_each(runs.result, '$.sources') AS source
        ON json_extract(source.value, '$.index') = json_extract(finding.value, '$.source')
    WHERE json_extract(finding.value, '$.verdict') = 'verified'
        AND json_extract(finding.value, '$.flag') IS NULL
    ORDER BY runs.started_at, runs.id
    ON CONFLICT (url, quote) DO UPDATE SET last_seen = excluded.last_seen;
    """,
    # 6: what people said about findings (numbered as reports number them)
    """
    CREATE TABLE ratings (
        run_id INTEGER NOT NULL,
        number INTEGER NOT NULL,
        verdict TEXT NOT NULL CHECK (verdict IN ('good', 'bad')),
        note TEXT,
        rated_at TEXT NOT NULL,
        goal TEXT NOT NULL,
        model TEXT NOT NULL,
        claim TEXT NOT NULL,
        quote TEXT NOT NULL,
        value TEXT,
        url TEXT,
        trusted INTEGER NOT NULL,
        PRIMARY KEY (run_id, number)
    );
    """,
    # 7: what Scout learned about each site: can it be read, do its pages hold up
    """
    CREATE TABLE sites (
        site TEXT PRIMARY KEY,
        reads INTEGER NOT NULL DEFAULT 0,
        failures INTEGER NOT NULL DEFAULT 0,
        streak INTEGER NOT NULL DEFAULT 0,
        last_failure TEXT,
        findings INTEGER NOT NULL DEFAULT 0,
        verified INTEGER NOT NULL DEFAULT 0,
        rated_bad INTEGER NOT NULL DEFAULT 0
    );
    """,
    # 8: what a run waiting for a model answer read, so that it resumes with the same pages;
    # findings rated wrong are hidden from recall rather than deleted, so a change of mind restores
    # them with their history
    """
    ALTER TABLE knowledge ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE pins (
        run TEXT NOT NULL,
        key TEXT NOT NULL,
        pinned_at TEXT NOT NULL,
        data TEXT NOT NULL,
        PRIMARY KEY (run, key)
    );
    """,
)


SEARCH_KEPT = timedelta(days=7)
PAGES_KEPT = timedelta(days=90)


@dataclass(frozen=True, slots=True)
class RunSummary:
    id: int
    watch: str | None
    goal: str
    started_at: datetime
    confidence: str
    verified: int
    findings: int


@dataclass(frozen=True, slots=True)
class Observation:
    key: str
    run_id: int
    observed_at: datetime
    label: str
    amount: Decimal
    currency: str | None
    unit: str | None


@dataclass(frozen=True, slots=True)
class Knowledge:
    """A trusted finding as Scout remembers it: once per page and quote."""

    claim: str
    quote: str
    url: str
    goal: str  # the question it was found for
    run_id: int  # the run that first found it
    first_seen: datetime
    last_seen: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "quote": self.quote,
            "url": self.url,
            "goal": self.goal,
            "run_id": self.run_id,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class Rating:
    """A person's verdict on a finding, with what the finding said at the time."""

    run_id: int
    number: int  # as reports number the run's findings
    verdict: str  # "good" or "bad"
    note: str | None
    rated_at: datetime
    goal: str
    model: str
    claim: str
    quote: str
    value: str | None
    url: str | None
    trusted: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "number": self.number,
            "verdict": self.verdict,
            "note": self.note,
            "rated_at": self.rated_at.isoformat(),
            "goal": self.goal,
            "model": self.model,
            "claim": self.claim,
            "quote": self.quote,
            "value": self.value,
            "url": self.url,
            "trusted": self.trusted,
        }


@dataclass(frozen=True, slots=True)
class AlertRecord:
    id: int
    watch: str
    run_id: int
    created_at: datetime
    reason: str
    url: str
    quote: str | None  # the evidence, for a fact read from the page's text
    delivered_at: datetime | None
    error: str | None  # why the last delivery failed


class Store:
    """Thread-safe access to the Scout database (one connection guarded by a lock)."""

    def __init__(self, path: Path | str) -> None:
        in_memory = str(path) == ":memory:"
        if not in_memory:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if not in_memory:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._migrate()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def get_page(self, url: str) -> Document | None:
        row = self._one("SELECT document FROM pages WHERE url = ?", (url,))
        return Document.from_dict(json.loads(row["document"])) if row else None

    def put_page(self, doc: Document) -> None:
        """Cache the latest fetch of a URL; readable versions are also kept as snapshots, and
        how it went is noted for the site."""
        payload = _dumps(doc.to_dict())
        with self._transaction() as conn:
            _note_fetch(conn, doc)
            conn.execute(
                "INSERT INTO pages (url, fetched_at, document) VALUES (?, ?, ?) "
                "ON CONFLICT(url) DO UPDATE SET "
                "fetched_at = excluded.fetched_at, document = excluded.document",
                (doc.url, doc.fetched_at.isoformat(), payload),
            )
            if doc.ok and doc.content_hash:
                conn.execute(
                    "INSERT OR IGNORE INTO snapshots (content_hash, url, fetched_at, document) "
                    "VALUES (?, ?, ?, ?)",
                    (doc.content_hash, doc.url, doc.fetched_at.isoformat(), payload),
                )

    def get_snapshot(self, content_hash: str) -> Document | None:
        row = self._one("SELECT document FROM snapshots WHERE content_hash = ?", (content_hash,))
        return Document.from_dict(json.loads(row["document"])) if row else None

    def get_search(self, key: str, *, newer_than: datetime) -> list[SearchHit] | None:
        row = self._one("SELECT fetched_at, hits FROM searches WHERE key = ?", (key,))
        if row is None or datetime.fromisoformat(row["fetched_at"]) < newer_than:
            return None
        return [SearchHit.from_dict(item) for item in json.loads(row["hits"])]

    def put_search(self, key: str, hits: Sequence[SearchHit], fetched_at: datetime) -> None:
        self._execute(
            "INSERT INTO searches (key, fetched_at, hits) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET fetched_at = excluded.fetched_at, hits = excluded.hits",
            (key, fetched_at.isoformat(), _dumps([hit.to_dict() for hit in hits])),
        )

    def add_run(self, result: RunResult, *, learn: bool = True) -> int:
        """Keep a run and remember what it found. A replay (*learn* False) read no page: it
        teaches nothing about the pages or their sites. Watches keep their runs with record()."""
        with self._transaction() as conn:
            return _insert_run(conn, result, None, learn=learn)

    def get_run(self, run_id: int) -> RunResult | None:
        row = self._one("SELECT result FROM runs WHERE id = ?", (run_id,))
        return RunResult.from_dict(json.loads(row["result"])) if row else None

    def recent_runs(self, *, limit: int = 20, watch: str | None = None) -> list[RunSummary]:
        where, params = ("WHERE scout = ?", (watch,)) if watch else ("", ())
        rows = self._all(
            "SELECT id, scout, goal, started_at, confidence, verified, findings FROM runs "
            f"{where} ORDER BY started_at DESC, id DESC LIMIT ?",
            (*params, limit),
        )
        return [
            RunSummary(
                id=row["id"],
                watch=row["scout"],
                goal=row["goal"],
                started_at=datetime.fromisoformat(row["started_at"]),
                confidence=row["confidence"],
                verified=row["verified"],
                findings=row["findings"],
            )
            for row in rows
        ]

    def last_runs(self, watch: str, *, limit: int = 1) -> list[tuple[int, RunResult]]:
        """A watch's latest runs with their ids, newest first."""
        rows = self._all(
            "SELECT id, result FROM runs WHERE scout = ? ORDER BY started_at DESC, id DESC LIMIT ?",
            (watch, limit),
        )
        return [(row["id"], RunResult.from_dict(json.loads(row["result"]))) for row in rows]

    def recall(self, question: str, *, limit: int = 10) -> list[Knowledge]:
        """Remembered findings that best match *question* (full-text search, BM25-ranked)."""
        terms = tokenize(question)
        if not terms:
            return []
        # Each term quoted: whatever the question contains, it is never FTS5 query syntax.
        query = " OR ".join(f'"{term}"' for term in dict.fromkeys(terms))
        rows = self._all(
            "SELECT knowledge.* FROM knowledge_index "
            "JOIN knowledge ON knowledge.id = knowledge_index.rowid "
            "WHERE knowledge_index MATCH ? AND knowledge.hidden = 0 "
            "ORDER BY bm25(knowledge_index, 3.0, 1.0, 0.5), knowledge.last_seen DESC LIMIT ?",
            (query, limit),
        )
        return [
            Knowledge(
                claim=row["claim"],
                quote=row["quote"],
                url=row["url"],
                goal=row["goal"],
                run_id=row["run_id"],
                first_seen=datetime.fromisoformat(row["first_seen"]),
                last_seen=datetime.fromisoformat(row["last_seen"]),
            )
            for row in rows
        ]

    def site_records(self, sites: Collection[str]) -> dict[str, SiteRecord]:
        """The records of these sites (sites never seen are left out)."""
        wanted = list(dict.fromkeys(sites))
        records: dict[str, SiteRecord] = {}
        for start in range(0, len(wanted), 500):  # SQLite limits the number of parameters
            batch = wanted[start : start + 500]
            marks = ", ".join("?" * len(batch))
            for row in self._all(f"SELECT * FROM sites WHERE site IN ({marks})", batch):
                records[row["site"]] = _site_record(row)
        return records

    def all_sites(self) -> list[SiteRecord]:
        return [_site_record(row) for row in self._all("SELECT * FROM sites ORDER BY site")]

    def forget_site(self, site: str) -> bool:
        with self._lock:
            return self._conn.execute("DELETE FROM sites WHERE site = ?", (site,)).rowcount > 0

    def put_rating(self, rating: Rating) -> None:
        """Keep a rating; rating the same finding again replaces the earlier verdict. A trusted
        finding rated wrong counts against its site and is hidden from recall while any rating
        still says it is wrong (an untrusted one was not believed in the first place)."""
        with self._transaction() as conn:
            before = conn.execute(
                "SELECT verdict FROM ratings WHERE run_id = ? AND number = ?",
                (rating.run_id, rating.number),
            ).fetchone()
            was_bad = before is not None and before["verdict"] == "bad"
            is_bad = rating.verdict == "bad"
            if rating.url and rating.trusted and was_bad != is_bad:
                site = hostname(rating.url)
                _ensure_site(conn, site)
                conn.execute(
                    "UPDATE sites SET rated_bad = max(rated_bad + ?, 0) WHERE site = ?",
                    (1 if is_bad else -1, site),
                )
            conn.execute(
                "INSERT INTO ratings (run_id, number, verdict, note, rated_at, goal, model, claim, "
                "quote, value, url, trusted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (run_id, number) DO UPDATE SET verdict = excluded.verdict, "
                "note = excluded.note, rated_at = excluded.rated_at",
                (
                    rating.run_id,
                    rating.number,
                    rating.verdict,
                    rating.note,
                    rating.rated_at.isoformat(),
                    rating.goal,
                    rating.model,
                    rating.claim,
                    rating.quote,
                    rating.value,
                    rating.url,
                    int(rating.trusted),
                ),
            )
            if rating.url and rating.trusted:
                hidden = _rated_bad(conn, rating.url, rating.quote)
                rows = conn.execute("SELECT id, quote FROM knowledge WHERE url = ?", (rating.url,))
                conn.executemany(
                    "UPDATE knowledge SET hidden = ? WHERE id = ?",
                    [(hidden, r["id"]) for r in rows if _same_quote(r["quote"], rating.quote)],
                )

    def ratings(self, *, run_id: int | None = None) -> list[Rating]:
        where, params = ("WHERE run_id = ?", (run_id,)) if run_id is not None else ("", ())
        rows = self._all(f"SELECT * FROM ratings {where} ORDER BY rated_at, run_id, number", params)
        return [
            Rating(
                run_id=row["run_id"],
                number=row["number"],
                verdict=row["verdict"],
                note=row["note"],
                rated_at=datetime.fromisoformat(row["rated_at"]),
                goal=row["goal"],
                model=row["model"],
                claim=row["claim"],
                quote=row["quote"],
                value=row["value"],
                url=row["url"],
                trusted=bool(row["trusted"]),
            )
            for row in rows
        ]

    def facts(self, watch: str) -> list[Fact]:
        """The facts a watch currently knows (gone ones excluded)."""
        rows = self._all(
            "SELECT fact FROM facts WHERE watch = ? AND status = 'active' ORDER BY rowid", (watch,)
        )
        return [Fact.from_dict(json.loads(row["fact"])) for row in rows]

    def record(
        self,
        watch: str,
        result: RunResult,
        deltas: Sequence[Delta],
        *,
        raised: Sequence[Trigger] = (),
        cleared: Sequence[str] = (),
    ) -> tuple[int, list[int | None]]:
        """Keep a watch's run, apply its changes to the ledger and raise its alerts in one
        transaction: a run that stops halfway leaves nothing, so no change goes unalerted.

        *cleared* are keys of conditions that stopped holding. Returns the run id and the new
        alert ids in the order of *raised* (None where a condition still holds from before).
        """
        with self._transaction() as conn:
            run_id = _insert_run(conn, result, watch, learn=True)
            for delta in deltas:
                _apply_delta(conn, watch, run_id, delta, result.started_at)
            conn.executemany(
                "UPDATE alerts SET holding = 0 WHERE watch = ? AND key = ? AND holding = 1",
                [(watch, key) for key in cleared],
            )
            alerts = [_raise(conn, watch, run_id, t, result.finished_at) for t in raised]
            return run_id, alerts

    def retire(self, watch: str) -> None:
        """Set aside what a watch knew (its question changed); its value history stays."""
        with self._transaction() as conn:
            conn.execute("UPDATE facts SET status = 'gone' WHERE watch = ?", (watch,))
            conn.execute("UPDATE alerts SET holding = 0 WHERE watch = ?", (watch,))

    def observations(self, watch: str) -> list[Observation]:
        rows = self._all(
            "SELECT key, run_id, observed_at, label, amount, currency, unit FROM observations "
            "WHERE watch = ? ORDER BY observed_at, rowid",
            (watch,),
        )
        return [
            Observation(
                key=row["key"],
                run_id=row["run_id"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
                label=row["label"],
                amount=Decimal(row["amount"]),
                currency=row["currency"],
                unit=row["unit"],
            )
            for row in rows
        ]

    def alerts(self, watch: str, *, limit: int = 50) -> list[AlertRecord]:
        """A watch's alerts, newest first."""
        return self._alerts("WHERE watch = ? ORDER BY id DESC LIMIT ?", (watch, limit))

    def undelivered(self, watch: str, *, since: datetime) -> list[AlertRecord]:
        """Alerts raised since *since* that no notification has carried yet, oldest first."""
        return self._alerts(
            "WHERE watch = ? AND delivered_at IS NULL AND created_at >= ? ORDER BY id",
            (watch, since.isoformat()),
        )

    def mark_alerts(self, ids: Sequence[int], *, at: datetime, error: str | None = None) -> None:
        """Record a delivery attempt: delivered at *at*, or failed with *error* (retried later)."""
        with self._transaction() as conn:
            conn.executemany(
                "UPDATE alerts SET delivered_at = ?, error = ? WHERE id = ?",
                [(None if error else at.isoformat(), error, alert_id) for alert_id in ids],
            )

    def _alerts(self, where: str, params: Sequence[Any]) -> list[AlertRecord]:
        rows = self._all(
            "SELECT id, watch, run_id, created_at, reason, url, quote, delivered_at, error "
            f"FROM alerts {where}",
            params,
        )
        return [
            AlertRecord(
                id=row["id"],
                watch=row["watch"],
                run_id=row["run_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
                reason=row["reason"],
                url=row["url"],
                quote=row["quote"],
                delivered_at=_when(row["delivered_at"]),
                error=row["error"],
            )
            for row in rows
        ]

    def get_pin(self, run: str, key: str, *, newer_than: datetime) -> Any | None:
        row = self._one(
            "SELECT data FROM pins WHERE run = ? AND key = ? AND pinned_at >= ?",
            (run, key, newer_than.isoformat()),
        )
        return json.loads(row["data"]) if row else None

    def put_pins(self, run: str, pins: Mapping[str, Any], at: datetime) -> None:
        with self._transaction() as conn:
            conn.executemany(
                "INSERT INTO pins (run, key, pinned_at, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run, key) DO UPDATE SET data = excluded.data",
                [(run, key, at.isoformat(), _dumps(data)) for key, data in pins.items()],
            )

    def drop_pins(self, run: str, *, before: datetime | None = None) -> None:
        """Forget a run's pins, or only those pinned before *before*."""
        if before is None:
            self._execute("DELETE FROM pins WHERE run = ?", (run,))
        else:
            self._execute(
                "DELETE FROM pins WHERE run = ? AND pinned_at < ?", (run, before.isoformat())
            )

    def prune(self, *, now: datetime) -> int:
        """Drop old searches, cached pages and page versions: how many went. A page version stays
        while it is its page's newest or a recent run read it, so recent runs can be replayed."""
        week = (now - SEARCH_KEPT).isoformat()
        pages = (now - PAGES_KEPT).isoformat()
        with self._transaction() as conn:
            removed = conn.execute("DELETE FROM searches WHERE fetched_at < ?", (week,)).rowcount
            removed += conn.execute("DELETE FROM pins WHERE pinned_at < ?", (week,)).rowcount
            removed += conn.execute("DELETE FROM pages WHERE fetched_at < ?", (pages,)).rowcount
            removed += conn.execute(
                "DELETE FROM snapshots WHERE fetched_at < :cutoff"
                " AND fetched_at < (SELECT max(newer.fetched_at) FROM snapshots AS newer"
                " WHERE newer.url = snapshots.url)"
                " AND content_hash NOT IN (SELECT json_extract(source.value, '$.content_hash')"
                " FROM runs, json_each(runs.result, '$.sources') AS source"
                " WHERE runs.started_at >= :cutoff"
                " AND json_extract(source.value, '$.content_hash') IS NOT NULL)",
                {"cutoff": pages},
            ).rowcount
            return removed

    def forget(self, watch: str) -> None:
        """Drop a watch's ledger, history and alerts (its runs stay in the run history)."""
        with self._transaction() as conn:
            conn.execute("DELETE FROM facts WHERE watch = ?", (watch,))
            conn.execute("DELETE FROM observations WHERE watch = ?", (watch,))
            conn.execute("DELETE FROM alerts WHERE watch = ?", (watch,))

    def get_capabilities(self, key: str, *, newer_than: datetime) -> dict[str, Any] | None:
        row = self._one("SELECT probed_at, capabilities FROM models WHERE key = ?", (key,))
        if row is None or datetime.fromisoformat(row["probed_at"]) < newer_than:
            return None
        data: dict[str, Any] = json.loads(row["capabilities"])
        return data

    def put_capabilities(self, key: str, capabilities: dict[str, Any], probed_at: datetime) -> None:
        self._execute(
            "INSERT INTO models (key, probed_at, capabilities) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "probed_at = excluded.probed_at, capabilities = excluded.capabilities",
            (key, probed_at.isoformat(), _dumps(capabilities)),
        )

    def _migrate(self) -> None:
        current = self.schema_version
        for number, script in enumerate(_MIGRATIONS[current:], start=current + 1):
            try:
                self._conn.executescript(
                    f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;"
                )
            except sqlite3.Error:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:  # SQLite may have rolled back already (disk full)
                    self._conn.execute("ROLLBACK")
                raise

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._conn.execute(sql, params).fetchone()
            return row

    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)


def _insert_run(
    conn: sqlite3.Connection, result: RunResult, watch: str | None, *, learn: bool
) -> int:
    cursor = conn.execute(
        "INSERT INTO runs (scout, goal, started_at, confidence, verified, findings, result)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            watch,
            result.goal,
            result.started_at.isoformat(),
            result.confidence.level,
            len(result.trusted),
            len(result.findings),
            _dumps(result.to_dict()),
        ),
    )
    run_id = int(cursor.lastrowid or 0)
    if learn:
        for finding in result.trusted:
            source = result.source(finding.source)
            if source is not None:
                _learn(conn, finding, source.url, result.goal, run_id, result.started_at)
        # A carried-over run repeats evidence already counted. A fact-check's quotes are chosen
        # for a claim under test, often a false one: whether they hold says more about the claim
        # and the model's reading than about the site.
        if not result.carried_over and result.plan.kind != CHECK_KIND:
            _note_findings(conn, result)
    return run_id


def _learn(
    conn: sqlite3.Connection, finding: Finding, url: str, goal: str, run_id: int, at: datetime
) -> None:
    """Remember a trusted finding, or note that it was seen again. A quote of the same page with
    a little more or less text around it (and the same numbers) is the same finding. One that
    someone rated wrong stays hidden."""
    for row in conn.execute("SELECT id, quote FROM knowledge WHERE url = ?", (url,)):
        if _same_quote(row["quote"], finding.quote):
            conn.execute(  # a resumed run can be older than one kept meanwhile
                "UPDATE knowledge SET last_seen = max(last_seen, ?) WHERE id = ?",
                (at.isoformat(), row["id"]),
            )
            return
    hidden = _rated_bad(conn, url, finding.quote)
    conn.execute(
        "INSERT INTO knowledge (url, quote, claim, goal, run_id, first_seen, last_seen, hidden) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (url, finding.quote, finding.claim, goal, run_id, at.isoformat(), at.isoformat(), hidden),
    )


def _note_fetch(conn: sqlite3.Connection, doc: Document) -> None:
    """Count a download for its site; a readable page ends a run of failures."""
    site = hostname(doc.url)
    if doc.ok:
        _ensure_site(conn, site)
        conn.execute("UPDATE sites SET reads = reads + 1, streak = 0 WHERE site = ?", (site,))
    elif doc.status in SITE_FAILURES:
        _ensure_site(conn, site)
        conn.execute(
            "UPDATE sites SET failures = failures + 1, streak = streak + 1, last_failure = ? "
            "WHERE site = ?",
            (doc.fetched_at.isoformat(), site),
        )


def _note_findings(conn: sqlite3.Connection, result: RunResult) -> None:
    """Count how the model's findings from each site held up (published offers are not the
    model's, so they say nothing about the site)."""
    tallies: dict[str, list[int]] = {}
    for finding in result.findings:
        source = result.source(finding.source)
        if source is None or finding.origin != "model":
            continue
        tally = tallies.setdefault(hostname(source.url), [0, 0])
        tally[0] += 1
        tally[1] += finding.verdict is Verdict.VERIFIED
    for site, (found, verified) in tallies.items():
        _ensure_site(conn, site)
        conn.execute(
            "UPDATE sites SET findings = findings + ?, verified = verified + ? WHERE site = ?",
            (found, verified, site),
        )


def _ensure_site(conn: sqlite3.Connection, site: str) -> None:
    conn.execute("INSERT INTO sites (site) VALUES (?) ON CONFLICT (site) DO NOTHING", (site,))


def _site_record(row: sqlite3.Row) -> SiteRecord:
    return SiteRecord(
        site=row["site"],
        reads=row["reads"],
        failures=row["failures"],
        streak=row["streak"],
        last_failure=_when(row["last_failure"]),
        findings=row["findings"],
        verified=row["verified"],
        rated_bad=row["rated_bad"],
    )


def _rated_bad(conn: sqlite3.Connection, url: str, quote: str) -> bool:
    """Someone says this trusted finding is wrong (untrusted ones are never remembered)."""
    rows = conn.execute(
        "SELECT quote FROM ratings WHERE url = ? AND verdict = 'bad' AND trusted = 1", (url,)
    )
    return any(_same_quote(row["quote"], quote) for row in rows)


def _same_quote(one: str, other: str) -> bool:
    """Two quotes of one page state the same finding: one holds the other, numbers intact."""
    one, other = fold(one), fold(other)
    return quoted_in(one, other) or quoted_in(other, one)


def _apply_delta(
    conn: sqlite3.Connection, watch: str, run_id: int, delta: Delta, at: datetime
) -> None:
    fact = delta.fact
    status = "gone" if delta.change is Change.GONE else "active"
    conn.execute(
        "INSERT INTO facts (watch, key, status, last_seen, fact) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(watch, key) DO UPDATE SET status = excluded.status, "
        "last_seen = excluded.last_seen, fact = excluded.fact",
        (watch, fact.key, status, at.isoformat(), _dumps(fact.to_dict())),
    )
    if delta.change is not Change.GONE and fact.amount is not None:
        conn.execute(
            "INSERT INTO observations "
            "(watch, key, run_id, observed_at, label, amount, currency, unit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                watch,
                fact.key,
                run_id,
                at.isoformat(),
                f"{fact.entity or fact.claim} ({fact.site})",
                str(fact.amount),
                fact.currency,
                fact.unit,
            ),
        )


def _raise(
    conn: sqlite3.Connection, watch: str, run_id: int, trigger: Trigger, at: datetime
) -> int | None:
    fact = trigger.delta.fact
    cursor = conn.execute(
        "INSERT INTO alerts (watch, key, run_id, created_at, reason, url, quote, holding)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (
            watch,
            trigger.key,
            run_id,
            at.isoformat(),
            trigger.reason,
            fact.url,
            None if fact.structured else fact.quote,
            int(trigger.rule.is_condition),
        ),
    )
    return cursor.lastrowid if cursor.rowcount else None


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
