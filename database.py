# File: database.py
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

SEQUENTIAL_PREFIX = "Holoween"


class FileLock:
    """Cross-process lockfile to prevent race conditions during sequence allocation."""
    def __init__(self, lock_path: Path, timeout: float = 10.0) -> None:
        self.lock_path = lock_path
        self.timeout = timeout
        self._fd: Optional[int] = None

    def __enter__(self) -> "FileLock":
        deadline = time.monotonic() + self.timeout
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(self._fd, str(os.getpid()).encode("utf-8"))
                return self
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Could not acquire database sequence lock: {self.lock_path}")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            if self.lock_path.exists():
                self.lock_path.unlink()
        except OSError:
            pass


class DatabaseManager:
    def __init__(self, db_path: Path, prefix: str = SEQUENTIAL_PREFIX, start_seq: int = 111) -> None:
        self.db_path = db_path
        self.lock_path = db_path.with_suffix(".lock")
        self.prefix = prefix
        self.start_seq = start_seq
        self._init_db()

    @contextmanager
    def _transaction(self, immediate: bool = False) -> Generator[sqlite3.Cursor, None, None]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=20.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.execute("BEGIN IMMEDIATE;" if immediate else "BEGIN;")
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._transaction(immediate=True) as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sequence_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    next_seq INTEGER NOT NULL
                );
            """)
            cur.execute(
                "INSERT OR IGNORE INTO sequence_meta (id, next_seq) VALUES (1, ?);",
                (self.start_seq,),
            )
            cur.execute("""
                CREATE TABLE IF NOT EXISTS active_session (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    seq_num INTEGER NOT NULL UNIQUE,
                    username TEXT NOT NULL UNIQUE,
                    started_at TEXT NOT NULL
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS confirmed_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seq_num INTEGER UNIQUE,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    x_user_id TEXT NOT NULL,
                    x_name TEXT NOT NULL,
                    x_created_at TEXT,
                    public_discoverable INTEGER NOT NULL DEFAULT 1,
                    admin_verified INTEGER NOT NULL DEFAULT 1,
                    confirmed_at TEXT NOT NULL
                );
            """)

    def format_username(self, seq_num: int) -> str:
        return f"{self.prefix}{seq_num:05d}"

    def _compute_safe_next_seq(self, cur: sqlite3.Cursor) -> int:
        cur.execute("SELECT next_seq FROM sequence_meta WHERE id = 1;")
        row = cur.fetchone()
        meta_seq = int(row["next_seq"]) if row else self.start_seq

        cur.execute("SELECT MAX(seq_num) AS max_seq FROM confirmed_accounts WHERE seq_num IS NOT NULL;")
        max_row = cur.fetchone()
        max_confirmed = int(max_row["max_seq"]) if (max_row and max_row["max_seq"] is not None) else (self.start_seq - 1)

        candidate = max(meta_seq, max_confirmed + 1, self.start_seq)
        while True:
            uname = self.format_username(candidate)
            cur.execute("SELECT 1 FROM confirmed_accounts WHERE seq_num = ? OR LOWER(username) = LOWER(?);", (candidate, uname))
            if not cur.fetchone():
                break
            candidate += 1

        if candidate != meta_seq:
            cur.execute("UPDATE sequence_meta SET next_seq = ? WHERE id = 1;", (candidate,))
        return candidate

    def peek_next_sequence(self) -> Tuple[int, str, Optional[Dict[str, Any]]]:
        with FileLock(self.lock_path):
            with self._transaction(immediate=False) as cur:
                seq = self._compute_safe_next_seq(cur)
                cur.execute("SELECT seq_num, username, started_at FROM active_session WHERE id = 1;")
                sess = cur.fetchone()
                return seq, self.format_username(seq), (dict(sess) if sess else None)

    def start_manual_session(self) -> Tuple[bool, Dict[str, Any]]:
        with FileLock(self.lock_path):
            with self._transaction(immediate=True) as cur:
                cur.execute("SELECT seq_num, username, started_at FROM active_session WHERE id = 1;")
                existing = cur.fetchone()
                if existing:
                    return False, dict(existing)

                seq = self._compute_safe_next_seq(cur)
                username = self.format_username(seq)
                now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                cur.execute(
                    "INSERT INTO active_session (id, seq_num, username, started_at) VALUES (1, ?, ?, ?);",
                    (seq, username, now_iso),
                )
                return True, {"seq_num": seq, "username": username, "started_at": now_iso}

    def get_active_session(self) -> Optional[Dict[str, Any]]:
        with self._transaction(immediate=False) as cur:
            cur.execute("SELECT seq_num, username, started_at FROM active_session WHERE id = 1;")
            row = cur.fetchone()
            return dict(row) if row else None

    def cancel_active_session(self) -> Optional[Dict[str, Any]]:
        with FileLock(self.lock_path):
            with self._transaction(immediate=True) as cur:
                cur.execute("SELECT seq_num, username, started_at FROM active_session WHERE id = 1;")
                row = cur.fetchone()
                if not row:
                    return None
                session_data = dict(row)
                cur.execute("DELETE FROM active_session WHERE id = 1;")
                return session_data

    def record_confirmed_account(
        self, username: str, x_user_id: str, x_name: str, x_created_at: Optional[str]
    ) -> Tuple[bool, Dict[str, Any]]:
        with FileLock(self.lock_path):
            with self._transaction(immediate=True) as cur:
                now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                cur.execute("SELECT * FROM confirmed_accounts WHERE LOWER(username) = LOWER(?);", (username,))
                existing = cur.fetchone()

                seq_num: Optional[int] = None
                if username.lower().startswith(self.prefix.lower()):
                    suffix = username[len(self.prefix):]
                    if suffix.isdigit():
                        seq_num = int(suffix)

                cur.execute("SELECT seq_num, username FROM active_session WHERE id = 1;")
                active_sess = cur.fetchone()
                session_closed = False

                if active_sess and active_sess["username"].lower() == username.lower():
                    seq_num = int(active_sess["seq_num"])
                    cur.execute("DELETE FROM active_session WHERE id = 1;")
                    session_closed = True

                if existing:
                    rec = dict(existing)
                    rec["already_recorded"] = True
                    rec["session_closed"] = session_closed
                    return False, rec

                cur.execute("""
                    INSERT INTO confirmed_accounts (
                        seq_num, username, x_user_id, x_name, x_created_at, public_discoverable, admin_verified, confirmed_at
                    ) VALUES (?, ?, ?, ?, ?, 1, 1, ?);
                """, (seq_num, username, x_user_id, x_name, x_created_at, now_iso))

                next_seq = self._compute_safe_next_seq(cur)
                cur.execute("UPDATE sequence_meta SET next_seq = ? WHERE id = 1;", (next_seq,))

                cur.execute("SELECT * FROM confirmed_accounts WHERE LOWER(username) = LOWER(?);", (username,))
                inserted = dict(cur.fetchone())
                inserted["already_recorded"] = False
                inserted["session_closed"] = session_closed
                inserted["next_seq"] = next_seq
                return True, inserted

    def list_confirmed_accounts(self, limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
        with self._transaction(immediate=False) as cur:
            cur.execute("""
                SELECT seq_num, username, x_user_id, x_name, x_created_at, confirmed_at
                FROM confirmed_accounts ORDER BY id DESC LIMIT ? OFFSET ?;
            """, (limit, offset))
            return [dict(r) for r in cur.fetchall()]

    def get_account_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        with self._transaction(immediate=False) as cur:
            cur.execute("""
                SELECT seq_num, username, x_user_id, x_name, x_created_at, public_discoverable, admin_verified, confirmed_at
                FROM confirmed_accounts WHERE LOWER(username) = LOWER(?);
            """, (username,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_stats(self) -> Dict[str, Any]:
        with self._transaction(immediate=False) as cur:
            next_seq = self._compute_safe_next_seq(cur)
            cur.execute("SELECT COUNT(*) AS cnt FROM confirmed_accounts;")
            total_confirmed = int(cur.fetchone()["cnt"])
            cur.execute("SELECT username, confirmed_at FROM confirmed_accounts ORDER BY id DESC LIMIT 1;")
            latest = cur.fetchone()
            cur.execute("SELECT seq_num, username, started_at FROM active_session WHERE id = 1;")
            active_sess = cur.fetchone()
            return {
                "total_confirmed": total_confirmed,
                "start_sequence": self.start_seq,
                "next_seq": next_seq,
                "next_username": self.format_username(next_seq),
                "latest_account": dict(latest) if latest else None,
                "active_session": dict(active_sess) if active_sess else None,
            }

    def check_health(self) -> Tuple[bool, str]:
        try:
            with self._transaction(immediate=False) as cur:
                cur.execute("SELECT 1;")
                cur.fetchone()
            return True, "SQLite WAL operational"
        except Exception as exc:
            return False, f"SQLite error: {exc}"
