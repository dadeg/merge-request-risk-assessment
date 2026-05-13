"""SQLite-backed persistence for dedupe + rate-limit bookkeeping."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Store:
    """Tiny wrapper around a SQLite db.

    Thread-safety: not designed for it. The bot is a single-loop process.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.executescript(_SCHEMA_PATH.read_text())

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    def has_seen(self, project_id: int, mr_iid: int, head_sha: str) -> bool:
        with self._cursor() as cur:
            cur.execute(
                "SELECT 1 FROM seen_mrs WHERE project_id=? AND mr_iid=? AND head_sha=? LIMIT 1",
                (project_id, mr_iid, head_sha),
            )
            return cur.fetchone() is not None

    def record_decision(
        self,
        project_id: int,
        mr_iid: int,
        head_sha: str,
        decision: str,
        risk: str | None = None,
        confidence: str | None = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT OR REPLACE INTO seen_mrs
                  (project_id, mr_iid, head_sha, decided_at, decision, risk, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (project_id, mr_iid, head_sha, int(time.time()), decision, risk, confidence),
            )

    def log_action(self, action: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO rate_log (ts, action) VALUES (?, ?)",
                (int(time.time()), action),
            )

    def actions_in_last_hour(self, action: str) -> int:
        cutoff = int(time.time()) - 3600
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM rate_log WHERE ts >= ? AND action = ?",
                (cutoff, action),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
