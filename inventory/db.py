"""SQLite storage.

Stock is never stored as a number that gets overwritten. Every change is a
row in `movements`, grouped under a `txns` row that says what happened (a
goods receipt, a production entry, wastage...). Current stock is the sum of
movements. That gives a full audit trail for free, and a wrong entry is
fixed by reversing it, never by editing history.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('raw', 'consumable', 'finished')),
    uom           TEXT NOT NULL,
    reorder_level REAL NOT NULL DEFAULT 0
);

-- Bill of materials: how much of each component one finished unit uses.
-- scrap_pct covers process loss (e.g. sheet offcuts), so consumption is
-- qty_per_unit * (1 + scrap_pct / 100).
CREATE TABLE IF NOT EXISTS bom (
    finished_id   INTEGER NOT NULL REFERENCES items(id),
    component_id  INTEGER NOT NULL REFERENCES items(id),
    qty_per_unit  REAL NOT NULL CHECK (qty_per_unit > 0),
    scrap_pct     REAL NOT NULL DEFAULT 0 CHECK (scrap_pct >= 0),
    PRIMARY KEY (finished_id, component_id)
);

CREATE TABLE IF NOT EXISTS txns (
    id                 INTEGER PRIMARY KEY,
    kind               TEXT NOT NULL,
    ref                TEXT,
    note               TEXT,
    user               TEXT,
    override_reason    TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    reverses_txn_id    INTEGER REFERENCES txns(id),
    reversed_by_txn_id INTEGER REFERENCES txns(id)
);

CREATE TABLE IF NOT EXISTS movements (
    id       INTEGER PRIMARY KEY,
    txn_id   INTEGER NOT NULL REFERENCES txns(id),
    item_id  INTEGER NOT NULL REFERENCES items(id),
    qty      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mov_item ON movements(item_id);
CREATE INDEX IF NOT EXISTS idx_txn_kind_ref ON txns(kind, ref);
"""


def db_path() -> str:
    return os.environ.get("INVENTORY_DB", str(Path(__file__).resolve().parent.parent / "inventory.db"))


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
