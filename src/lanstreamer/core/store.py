"""Configuration store: outputs and their inputs in SQLite (data folder, lanstreamer.db).

The schema is versioned with PRAGMA user_version; MIGRATIONS[n] upgrades from
version n to n+1. On the first start the store is seeded with one output, two
test inputs and the "Standby" fallback, so the stream shows something before
anything is configured.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from lanstreamer.core.config import InputConfig, OutputConfig

MIGRATIONS = [
    """
    CREATE TABLE outputs (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        width INTEGER NOT NULL,
        height INTEGER NOT NULL,
        fps INTEGER NOT NULL,
        bitrate_kbps INTEGER NOT NULL,
        encoder TEXT NOT NULL,
        position INTEGER NOT NULL
    );
    CREATE TABLE inputs (
        id TEXT PRIMARY KEY,
        output_id TEXT NOT NULL REFERENCES outputs(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        plugin TEXT NOT NULL,
        settings TEXT NOT NULL,
        priority INTEGER NOT NULL,
        fallback INTEGER NOT NULL,
        min_duration_s REAL NOT NULL,
        max_duration_s REAL,
        override TEXT NOT NULL,
        position INTEGER NOT NULL
    );
    """,
    # 2: key/value settings (admin password hash, ...)
    """
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 3: every output gets a fallback "Standby" input if it has none (requirement F-SCH-02)
    """
    INSERT INTO inputs (id, output_id, name, plugin, settings, priority, fallback,
                        min_duration_s, max_duration_s, override, position)
    SELECT 'standby-' || o.id, o.id, 'Standby', 'standby', '{"image": ""}', 0, 1, 0, NULL, 'none',
           (SELECT COUNT(*) FROM inputs i WHERE i.output_id = o.id)
    FROM outputs o
    WHERE NOT EXISTS (SELECT 1 FROM inputs i WHERE i.output_id = o.id AND i.fallback = 1);
    """,
]

INPUT_FIELDS = ("name", "plugin", "settings", "priority", "fallback", "min_duration_s",
                "max_duration_s", "override")  # fmt: skip
OUTPUT_FIELDS = ("name", "width", "height", "fps", "bitrate_kbps", "encoder")


class NotFound(KeyError):
    pass


def new_id(name: str, taken: set[str]) -> str:
    """A readable, unique id from a name: "Twitch Just Chatting" → "twitch-just-chatting"."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "item"
    candidate = base
    while candidate in taken:
        candidate = f"{base}-{secrets.token_hex(2)}"
    return candidate


def seed() -> OutputConfig:
    return OutputConfig(
        id="out1",
        name="Output 1",
        inputs=[
            InputConfig("test-a", "Test input A", "test-pattern",
                        {"pattern": "smpte", "tone_hz": 440}, min_duration_s=20),
            InputConfig("test-b", "Test input B", "test-pattern",
                        {"pattern": "ball", "tone_hz": 660}, min_duration_s=20),
            InputConfig("standby", "Standby", "standby", {"image": ""}, priority=0, fallback=True),
        ],
    )  # fmt: skip


class Store:
    def __init__(self, path: Path | str):
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        self._migrate()
        if not self.outputs():
            self.add_output(seed())

    def _migrate(self) -> None:
        with self._lock:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            for n in range(version, len(MIGRATIONS)):
                self._db.executescript(f"BEGIN; {MIGRATIONS[n]}; PRAGMA user_version = {n + 1}; COMMIT;")

    def close(self) -> None:
        self._db.close()

    # --- read -----------------------------------------------------------------------

    def outputs(self) -> list[OutputConfig]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM outputs ORDER BY position, id").fetchall()
            return [self._output(row) for row in rows]

    def output(self, output_id: str) -> OutputConfig:
        with self._lock:
            row = self._db.execute("SELECT * FROM outputs WHERE id = ?", (output_id,)).fetchone()
            if row is None:
                raise NotFound(output_id)
            return self._output(row)

    def _output(self, row: sqlite3.Row) -> OutputConfig:
        inputs = self._db.execute(
            "SELECT * FROM inputs WHERE output_id = ? ORDER BY position, id", (row["id"],)
        ).fetchall()
        return OutputConfig(
            id=row["id"], name=row["name"], width=row["width"], height=row["height"],
            fps=row["fps"], bitrate_kbps=row["bitrate_kbps"], encoder=row["encoder"],
            inputs=[self._input(i) for i in inputs],
        )  # fmt: skip

    @staticmethod
    def _input(row: sqlite3.Row) -> InputConfig:
        return InputConfig(
            id=row["id"], name=row["name"], plugin=row["plugin"],
            settings=json.loads(row["settings"]), priority=row["priority"],
            fallback=bool(row["fallback"]), min_duration_s=row["min_duration_s"],
            max_duration_s=row["max_duration_s"], override=row["override"],
        )  # fmt: skip

    # --- write ----------------------------------------------------------------------

    def add_output(self, cfg: OutputConfig) -> OutputConfig:
        with self._lock:
            taken = {r[0] for r in self._db.execute("SELECT id FROM outputs")}
            if not cfg.id or cfg.id in taken:
                cfg.id = new_id(cfg.name, taken)
            position = len(taken)
            self._db.execute(
                "INSERT INTO outputs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (cfg.id, cfg.name, cfg.width, cfg.height, cfg.fps, cfg.bitrate_kbps,
                 cfg.encoder, position),
            )  # fmt: skip
        for inp in cfg.inputs:
            self.add_input(cfg.id, inp)
        return self.output(cfg.id)

    def update_output(self, output_id: str, **changes: Any) -> OutputConfig:
        self._update("outputs", output_id, OUTPUT_FIELDS, changes)
        return self.output(output_id)

    def delete_output(self, output_id: str) -> None:
        with self._lock:
            if self._db.execute("DELETE FROM outputs WHERE id = ?", (output_id,)).rowcount == 0:
                raise NotFound(output_id)

    def add_input(self, output_id: str, cfg: InputConfig) -> InputConfig:
        with self._lock:
            if self._db.execute("SELECT 1 FROM outputs WHERE id = ?", (output_id,)).fetchone() is None:
                raise NotFound(output_id)
            taken = {r[0] for r in self._db.execute("SELECT id FROM inputs")}
            if not cfg.id or cfg.id in taken:
                cfg.id = new_id(cfg.name, taken)
            position = self._db.execute(
                "SELECT COUNT(*) FROM inputs WHERE output_id = ?", (output_id,)
            ).fetchone()[0]
            data = asdict(cfg)
            self._db.execute(
                "INSERT INTO inputs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cfg.id, output_id, cfg.name, cfg.plugin, json.dumps(data["settings"]),
                 cfg.priority, int(cfg.fallback), cfg.min_duration_s, cfg.max_duration_s,
                 cfg.override, position),
            )  # fmt: skip
        return cfg

    def update_input(self, input_id: str, **changes: Any) -> InputConfig:
        if "settings" in changes:
            changes["settings"] = json.dumps(changes["settings"])
        if "fallback" in changes:
            changes["fallback"] = int(bool(changes["fallback"]))
        self._update("inputs", input_id, INPUT_FIELDS, changes)
        with self._lock:
            row = self._db.execute("SELECT * FROM inputs WHERE id = ?", (input_id,)).fetchone()
            return self._input(row)

    def output_of_input(self, input_id: str) -> str:
        with self._lock:
            row = self._db.execute("SELECT output_id FROM inputs WHERE id = ?", (input_id,)).fetchone()
            if row is None:
                raise NotFound(input_id)
            return row[0]

    def delete_input(self, input_id: str) -> None:
        with self._lock:
            if self._db.execute("DELETE FROM inputs WHERE id = ?", (input_id,)).rowcount == 0:
                raise NotFound(input_id)

    def reorder_inputs(self, output_id: str, input_ids: list[str]) -> None:
        """Sets the list order (ties in priority are broken by this order)."""
        with self._lock:
            for position, input_id in enumerate(input_ids):
                self._db.execute(
                    "UPDATE inputs SET position = ? WHERE id = ? AND output_id = ?",
                    (position, input_id, output_id),
                )

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def _update(self, table: str, row_id: str, allowed: tuple[str, ...], changes: dict) -> None:
        unknown = set(changes) - set(allowed)
        if unknown:
            raise ValueError(f"cannot change {', '.join(sorted(unknown))}")
        if not changes:
            return
        assignments = ", ".join(f"{key} = ?" for key in changes)
        with self._lock:
            cursor = self._db.execute(
                f"UPDATE {table} SET {assignments} WHERE id = ?",
                (*changes.values(), row_id),
            )
            if cursor.rowcount == 0:
                raise NotFound(row_id)
