"""The idempotency ledger.

One row per Message-ID, holding a status and a counter. It deliberately stores
no subject, no sender and no body: the ledger answers "did I already handle
this?" and nothing else.

Processing a mail has external side effects (a moved mail, a webhook call), so
exactly-once is not achievable. What this gives is a durable claim: a message is
claimed before the actions run and marked done after they ran. A crash in
between leaves the claim open, and the mail is retried at most `max_attempts`
times instead of forever.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

RUNNING = "running"
DONE = "done"
INCOMPLETE = "incomplete"
FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    message_id TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    category   TEXT,
    rule       TEXT,
    reason     TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS processed_status_idx ON processed (status);
"""


class Ledger:
    """SQLite-backed de-duplication store. Safe across restarts and processes."""

    def __init__(self, path: str | Path, max_attempts: int = 3) -> None:
        self.path = Path(path)
        self.max_attempts = max_attempts
        if self.path.parent.name:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, isolation_level=None, timeout=30.0)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)

    def claim(self, message_id: str) -> bool:
        """Reserve a message. True means: you own it, go process it."""
        now = time.time()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute(
                "SELECT status, attempts FROM processed WHERE message_id = ?", (message_id,)
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO processed (message_id, status, attempts, first_seen, last_seen)"
                    " VALUES (?, ?, 1, ?, ?)",
                    (message_id, RUNNING, now, now),
                )
                granted = True
            elif row[0] == DONE or row[1] >= self.max_attempts:
                granted = False
            else:
                self._db.execute(
                    "UPDATE processed SET status = ?, attempts = attempts + 1, last_seen = ?"
                    " WHERE message_id = ?",
                    (RUNNING, now, message_id),
                )
                granted = True
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")
        return granted

    def finish(
        self,
        message_id: str,
        status: str,
        category: str | None = None,
        rule: str | None = None,
        reason: str | None = None,
    ) -> None:
        self._db.execute(
            "UPDATE processed SET status = ?, category = ?, rule = ?, reason = ?, last_seen = ?"
            " WHERE message_id = ?",
            (status, category, rule, reason, time.time(), message_id),
        )

    def status_of(self, message_id: str) -> str | None:
        row = self._db.execute(
            "SELECT status FROM processed WHERE message_id = ?", (message_id,)
        ).fetchone()
        return None if row is None else str(row[0])

    def forget(self, message_id: str) -> bool:
        """Release a message so the next run picks it up again."""
        cursor = self._db.execute("DELETE FROM processed WHERE message_id = ?", (message_id,))
        return cursor.rowcount > 0

    def counts(self) -> dict[str, int]:
        rows = self._db.execute("SELECT status, COUNT(*) FROM processed GROUP BY status")
        return {str(status): int(count) for status, count in rows}

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
