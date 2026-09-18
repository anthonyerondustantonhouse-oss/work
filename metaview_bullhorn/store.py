"""SQLite state: the `processed` table (dedup + confirmation queue) and the
`runs` table (per-run summary and consecutive failure tracking).

Nothing is ever written to Bullhorn twice: a conversation id is inserted
before any write, and only rows in status `confirmed` may be written.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import Conversation, MatchResult, RecordRef

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_WRITTEN = "written"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUSES = (STATUS_PENDING, STATUS_CONFIRMED, STATUS_WRITTEN, STATUS_SKIPPED, STATUS_FAILED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    conversation_id   TEXT PRIMARY KEY,
    first_seen        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('pending','confirmed','written','skipped','failed')),
    bullhorn_entity   TEXT,
    bullhorn_record_id INTEGER,
    note_id           INTEGER,
    job_order_id      INTEGER,
    action_type       TEXT,
    confidence        TEXT,
    match_reason      TEXT,
    proposed_record   TEXT,   -- JSON RecordRef
    alternatives      TEXT,   -- JSON list of RecordRef
    job_order_options TEXT,   -- JSON list of {id, title, ...}
    draft_note        TEXT,
    conversation      TEXT,   -- JSON Conversation (local only; never sent to Bullhorn)
    error             TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    ok            INTEGER NOT NULL DEFAULT 0,
    found         INTEGER NOT NULL DEFAULT 0,
    new           INTEGER NOT NULL DEFAULT 0,
    matched       INTEGER NOT NULL DEFAULT 0,
    queued        INTEGER NOT NULL DEFAULT 0,
    written       INTEGER NOT NULL DEFAULT 0,
    errors        INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- processed -----------------------------------------------------

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM processed WHERE conversation_id = ?", (conversation_id,)).fetchone()
        return self._row_to_item(row) if row else None

    def status_of(self, conversation_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT status FROM processed WHERE conversation_id = ?", (conversation_id,)).fetchone()
        return row["status"] if row else None

    def is_known(self, conversation_id: str) -> bool:
        return self.status_of(conversation_id) is not None

    def add_new(
        self,
        conversation: Conversation,
        match: MatchResult,
        draft_note: str,
        action_type: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """Insert a newly seen conversation. Refuses to overwrite an existing row."""
        if status not in (STATUS_PENDING, STATUS_CONFIRMED):
            raise ValueError(f"new items must start pending or confirmed, not {status}")
        if status == STATUS_CONFIRMED and match.proposed is None:
            raise ValueError("cannot confirm without a proposed record")
        now = utcnow()
        with self._lock:
            self._conn.execute(
                """INSERT INTO processed (conversation_id, first_seen, updated_at, status, bullhorn_entity,
                       bullhorn_record_id, action_type, confidence, match_reason, proposed_record, alternatives,
                       job_order_options, draft_note, conversation, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    conversation.id,
                    now,
                    now,
                    status,
                    match.proposed.entity if match.proposed else None,
                    match.proposed.id if match.proposed else None,
                    action_type,
                    match.confidence,
                    match.reason,
                    _dumps(match.proposed.to_dict() if match.proposed else None),
                    _dumps([a.to_dict() for a in match.alternatives]),
                    _dumps(match.job_orders),
                    draft_note,
                    _dumps(conversation.to_dict()),
                    error,
                ),
            )
            self._conn.commit()

    def confirm(
        self,
        conversation_id: str,
        record: RecordRef,
        draft_note: str,
        action_type: str,
        job_order_id: int | None,
    ) -> None:
        """Human approval: pin the record, note text, action and job order."""
        item = self._require(conversation_id)
        if item["status"] not in (STATUS_PENDING, STATUS_FAILED):
            raise ValueError(f"cannot confirm an item in status {item['status']}")
        with self._lock:
            self._conn.execute(
                """UPDATE processed SET status=?, bullhorn_entity=?, bullhorn_record_id=?, proposed_record=?,
                       draft_note=?, action_type=?, job_order_id=?, error=NULL, updated_at=?
                   WHERE conversation_id=?""",
                (
                    STATUS_CONFIRMED,
                    record.entity,
                    record.id,
                    _dumps(record.to_dict()),
                    draft_note,
                    action_type,
                    job_order_id,
                    utcnow(),
                    conversation_id,
                ),
            )
            self._conn.commit()

    def skip(self, conversation_id: str, reason: str | None = None) -> None:
        item = self._require(conversation_id)
        if item["status"] == STATUS_WRITTEN:
            raise ValueError("cannot skip an item that has already been written")
        with self._lock:
            self._conn.execute(
                "UPDATE processed SET status=?, error=?, updated_at=? WHERE conversation_id=?",
                (STATUS_SKIPPED, reason, utcnow(), conversation_id),
            )
            self._conn.commit()

    def reopen(self, conversation_id: str) -> None:
        """Move a skipped or failed item back to pending so it can be reviewed again."""
        item = self._require(conversation_id)
        if item["status"] not in (STATUS_SKIPPED, STATUS_FAILED):
            raise ValueError(f"cannot reopen an item in status {item['status']}")
        with self._lock:
            self._conn.execute(
                "UPDATE processed SET status=?, error=NULL, updated_at=? WHERE conversation_id=?",
                (STATUS_PENDING, utcnow(), conversation_id),
            )
            self._conn.commit()

    def mark_written(self, conversation_id: str, note_id: int) -> None:
        item = self._require(conversation_id)
        if item["status"] != STATUS_CONFIRMED:
            raise ValueError(f"only confirmed items can be marked written (was {item['status']})")
        with self._lock:
            self._conn.execute(
                "UPDATE processed SET status=?, note_id=?, error=NULL, updated_at=? WHERE conversation_id=?",
                (STATUS_WRITTEN, note_id, utcnow(), conversation_id),
            )
            self._conn.commit()

    def mark_failed(self, conversation_id: str, error: str) -> None:
        item = self._require(conversation_id)
        if item["status"] == STATUS_WRITTEN:
            raise ValueError("cannot fail an item that has already been written")
        with self._lock:
            self._conn.execute(
                "UPDATE processed SET status=?, error=?, updated_at=? WHERE conversation_id=?",
                (STATUS_FAILED, error[:2000], utcnow(), conversation_id),
            )
            self._conn.commit()

    def list_by_status(self, statuses: Iterable[str]) -> list[dict[str, Any]]:
        statuses = list(statuses)
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM processed WHERE status IN ({placeholders}) ORDER BY first_seen ASC",
                statuses,
            ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def queue(self) -> list[dict[str, Any]]:
        """Items waiting for a human: pending first, then failed."""
        return self.list_by_status([STATUS_PENDING]) + self.list_by_status([STATUS_FAILED])

    def confirmed(self) -> list[dict[str, Any]]:
        return self.list_by_status([STATUS_CONFIRMED])

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM processed GROUP BY status").fetchall()
        counts = {s: 0 for s in STATUSES}
        for row in rows:
            counts[row["status"]] = row["n"]
        return counts

    def _require(self, conversation_id: str) -> dict[str, Any]:
        item = self.get(conversation_id)
        if item is None:
            raise KeyError(f"unknown conversation {conversation_id}")
        return item

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["proposed_record"] = RecordRef.from_dict(_loads(item.get("proposed_record"), None))
        item["alternatives"] = [RecordRef.from_dict(a) for a in _loads(item.get("alternatives"), [])]
        item["job_order_options"] = _loads(item.get("job_order_options"), [])
        item["conversation"] = _loads(item.get("conversation"), None)
        return item

    # ---- runs ----------------------------------------------------------

    def start_run(self) -> int:
        with self._lock:
            cur = self._conn.execute("INSERT INTO runs (started_at) VALUES (?)", (utcnow(),))
            self._conn.commit()
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, ok: bool, stats: dict[str, int], error_message: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE runs SET finished_at=?, ok=?, found=?, new=?, matched=?, queued=?, written=?, errors=?,
                       error_message=? WHERE id=?""",
                (
                    utcnow(),
                    1 if ok else 0,
                    stats.get("found", 0),
                    stats.get("new", 0),
                    stats.get("matched", 0),
                    stats.get("queued", 0),
                    stats.get("written", 0),
                    stats.get("errors", 0),
                    error_message,
                    run_id,
                ),
            )
            self._conn.commit()

    def consecutive_failures(self) -> int:
        """Number of most recent finished runs, counting back, that were not ok."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ok FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 100"
            ).fetchall()
        n = 0
        for row in rows:
            if row["ok"]:
                break
            n += 1
        return n

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
