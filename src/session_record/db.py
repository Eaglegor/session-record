"""SQLite storage for sessions, OSC events, song takes and video files."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY,
    name         TEXT,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    marker_count INTEGER NOT NULL DEFAULT 0
);
-- Every start/stop command received; marker_seq mirrors the marker index on the Wing.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,          -- 'start' | 'stop'
    song_number INTEGER,
    marker_seq  INTEGER,                -- NULL for manual commands that placed no Wing marker
    address     TEXT,
    args        TEXT,
    note        TEXT
);
-- One take = one start..stop span of a song.
CREATE TABLE IF NOT EXISTS takes (
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER NOT NULL REFERENCES sessions(id),
    seq          INTEGER NOT NULL,
    song_number  INTEGER,
    started_at   REAL NOT NULL,
    stopped_at   REAL,
    start_marker INTEGER,
    stop_marker  INTEGER
);
CREATE TABLE IF NOT EXISTS videos (
    id         INTEGER PRIMARY KEY,
    take_id    INTEGER NOT NULL REFERENCES takes(id),
    camera     TEXT NOT NULL,
    path       TEXT NOT NULL,
    started_at REAL,
    stopped_at REAL,
    status     TEXT NOT NULL            -- 'recording' | 'ok' | 'failed'
);
"""


@dataclass
class Take:
    id: int
    session_id: int
    seq: int
    song_number: int | None
    started_at: float
    stopped_at: float | None
    start_marker: int | None
    stop_marker: int | None


@dataclass
class Video:
    id: int
    take_id: int
    camera: str
    path: str
    started_at: float | None
    stopped_at: float | None
    status: str


class Database:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    # sessions -----------------------------------------------------------
    def create_session(self, name: str | None, ts: float) -> int:
        return self._exec("INSERT INTO sessions (name, started_at) VALUES (?, ?)", (name, ts)).lastrowid

    def end_session(self, session_id: int, ts: float) -> None:
        self._exec("UPDATE sessions SET ended_at = ? WHERE id = ?", (ts, session_id))

    def latest_session_id(self) -> int | None:
        row = self._exec("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        return row["id"] if row else None

    def get_session(self, session_id: int) -> sqlite3.Row | None:
        return self._exec("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()

    def list_sessions(self) -> list[sqlite3.Row]:
        return self._exec(
            "SELECT s.*, (SELECT COUNT(*) FROM takes t WHERE t.session_id = s.id) AS take_count "
            "FROM sessions s ORDER BY s.id"
        ).fetchall()

    def next_marker(self, session_id: int) -> int:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET marker_count = marker_count + 1 WHERE id = ?", (session_id,)
            )
            return self._conn.execute(
                "SELECT marker_count FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()[0]

    # events -------------------------------------------------------------
    def add_event(self, session_id: int, ts: float, kind: str, song_number: int | None,
                  marker_seq: int | None, address: str | None, args: tuple, note: str | None = None) -> None:
        self._exec(
            "INSERT INTO events (session_id, ts, kind, song_number, marker_seq, address, args, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, ts, kind, song_number, marker_seq, address, json.dumps(list(args)), note),
        )

    def events(self, session_id: int) -> list[sqlite3.Row]:
        return self._exec("SELECT * FROM events WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()

    # takes --------------------------------------------------------------
    def create_take(self, session_id: int, song_number: int | None, ts: float, start_marker: int | None) -> Take:
        with self._lock:
            seq = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM takes WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
            take_id = self._conn.execute(
                "INSERT INTO takes (session_id, seq, song_number, started_at, start_marker) VALUES (?, ?, ?, ?, ?)",
                (session_id, seq, song_number, ts, start_marker),
            ).lastrowid
        return Take(take_id, session_id, seq, song_number, ts, None, start_marker, None)

    def close_take(self, take_id: int, ts: float, stop_marker: int | None) -> None:
        self._exec("UPDATE takes SET stopped_at = ?, stop_marker = ? WHERE id = ?", (ts, stop_marker, take_id))

    def takes(self, session_id: int) -> list[Take]:
        rows = self._exec("SELECT * FROM takes WHERE session_id = ? ORDER BY seq", (session_id,)).fetchall()
        return [Take(**dict(r)) for r in rows]

    def open_take(self, session_id: int) -> Take | None:
        row = self._exec(
            "SELECT * FROM takes WHERE session_id = ? AND stopped_at IS NULL ORDER BY seq DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return Take(**dict(row)) if row else None

    # videos -------------------------------------------------------------
    def add_video(self, take_id: int, camera: str, path: str, ts: float) -> int:
        return self._exec(
            "INSERT INTO videos (take_id, camera, path, started_at, status) VALUES (?, ?, ?, ?, 'recording')",
            (take_id, camera, path, ts),
        ).lastrowid

    def finish_video(self, video_id: int, ts: float, status: str) -> None:
        self._exec("UPDATE videos SET stopped_at = ?, status = ? WHERE id = ?", (ts, status, video_id))

    def videos(self, take_id: int) -> list[Video]:
        rows = self._exec("SELECT * FROM videos WHERE take_id = ? ORDER BY id", (take_id,)).fetchall()
        return [Video(**dict(r)) for r in rows]
