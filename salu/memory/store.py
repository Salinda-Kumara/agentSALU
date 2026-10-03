"""SQLite persistence: task history, conversation log, and preference memory.

Secrets are refused at the storage layer: anything that looks like a password,
key or token is rejected for memory facts and redacted from history.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..security.redact import contains_secret, redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    request TEXT NOT NULL,
    status TEXT NOT NULL,           -- running | done | failed | cancelled
    summary TEXT,
    tools_used TEXT,                -- JSON list
    started_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,             -- user | assistant | system
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_started ON tasks(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""


class SecretRejected(ValueError):
    pass


class MemoryStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL") if str(path) != ":memory:" else None
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def self_test(self) -> None:
        """Write/read/delete round trip — used by the startup check."""
        key = f"__selftest_{uuid.uuid4().hex[:6]}"
        self._exec("INSERT INTO memories(key,value,category,updated_at) VALUES(?,?,?,?)", (key, "ok", "system", time.time()))
        rows = self._query("SELECT value FROM memories WHERE key=?", (key,))
        self._exec("DELETE FROM memories WHERE key=?", (key,))
        if not rows or rows[0]["value"] != "ok":
            raise RuntimeError("memory self-test read-back failed")

    # --- tasks ------------------------------------------------------------
    def start_task(self, request: str, session_id: str | None = None) -> str:
        task_id = uuid.uuid4().hex[:12]
        self._exec("INSERT INTO tasks(id,session_id,request,status,started_at) VALUES(?,?,?,?,?)",
                   (task_id, session_id, redact(request)[:2000], "running", time.time()))
        return task_id

    def finish_task(self, task_id: str, status: str, summary: str = "", tools_used: list[str] | None = None) -> None:
        self._exec("UPDATE tasks SET status=?, summary=?, tools_used=?, finished_at=? WHERE id=?",
                   (status, redact(summary)[:2000], json.dumps(tools_used or []), time.time(), task_id))

    def recent_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM tasks ORDER BY started_at DESC LIMIT ?", (limit,))
        for r in rows:
            r["tools_used"] = json.loads(r["tools_used"] or "[]")
        return rows

    def mark_interrupted_tasks(self) -> int:
        """Tasks left 'running' by a previous crash are marked failed (never silently forgotten)."""
        cur = self._exec("UPDATE tasks SET status='failed', summary='Interrupted (SALU was stopped)', "
                         "finished_at=? WHERE status='running'", (time.time(),))
        return cur.rowcount

    # --- conversation -----------------------------------------------------
    def add_message(self, session_id: str, role: str, content: str) -> None:
        self._exec("INSERT INTO messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                   (session_id, role, redact(content), time.time()))

    def messages(self, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._query("SELECT role, content, created_at FROM messages WHERE session_id=? "
                           "ORDER BY id DESC LIMIT ?", (session_id, limit))
        return list(reversed(rows))

    # --- memory facts -------------------------------------------------------
    def remember(self, key: str, value: str, category: str = "general") -> None:
        key, value = key.strip(), value.strip()
        if not key or not value:
            raise ValueError("key and value are required")
        if contains_secret(key) or contains_secret(value):
            raise SecretRejected("This looks like a secret (password/key/token). SALU does not store "
                                 "secrets in memory — use environment variables or a credential manager.")
        self._exec("INSERT INTO memories(key,value,category,updated_at) VALUES(?,?,?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value, category=excluded.category, "
                   "updated_at=excluded.updated_at", (key[:200], value[:4000], category[:50], time.time()))

    def recall(self, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        if query:
            like = f"%{query}%"
            return self._query("SELECT key,value,category,updated_at FROM memories WHERE category!='system' AND "
                               "(key LIKE ? OR value LIKE ? OR category LIKE ?) ORDER BY updated_at DESC LIMIT ?",
                               (like, like, like, limit))
        return self._query("SELECT key,value,category,updated_at FROM memories WHERE category!='system' "
                           "ORDER BY updated_at DESC LIMIT ?", (limit,))

    def forget(self, key: str) -> bool:
        return self._exec("DELETE FROM memories WHERE key=?", (key,)).rowcount > 0
