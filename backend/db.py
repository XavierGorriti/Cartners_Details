"""SQLite helpers for the shared Cartners backend."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "cartners.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  spending_mode TEXT NOT NULL,
  servings INTEGER NOT NULL,
  restrictions_json TEXT NOT NULL,
  likes_json TEXT NOT NULL,
  max_prep_minutes INTEGER NOT NULL,
  list_revision INTEGER NOT NULL DEFAULT 0,
  last_store_name TEXT
);

CREATE TABLE IF NOT EXISTS actors (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_actors (
  workspace_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  PRIMARY KEY (workspace_id, actor_id)
);

CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  quantity REAL NOT NULL,
  unit TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  request_for TEXT NOT NULL,
  added_by TEXT NOT NULL,
  bought INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  removed INTEGER NOT NULL DEFAULT 0,
  run_id TEXT,
  reserved_plan_id TEXT
);

CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  event TEXT NOT NULL,
  plan_date TEXT NOT NULL,
  servings INTEGER,
  recipe_json TEXT NOT NULL,
  action_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  previous_draft_id TEXT,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trips (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  claimed_by TEXT,
  store_name TEXT,
  claimed_at TEXT,
  request_deadline_at TEXT,
  state TEXT NOT NULL,
  claimed_item_ids_json TEXT NOT NULL DEFAULT '[]',
  completed_trips_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS expenses (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  run_id TEXT,
  total_cents INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'USD',
  payer_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  shared_member_ids_json TEXT NOT NULL,
  personal_allocations_json TEXT NOT NULL,
  allocations_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
  action_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  change_summary TEXT NOT NULL,
  undo_json TEXT,
  undone INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  actor_id TEXT,
  message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingredients (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  price_cents INTEGER,
  currency TEXT NOT NULL DEFAULT 'USD',
  package_quantity REAL,
  package_unit TEXT,
  store_name TEXT NOT NULL DEFAULT 'Sample Kroger',
  source_mode TEXT NOT NULL DEFAULT 'sample'
);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path or DEFAULT_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)
