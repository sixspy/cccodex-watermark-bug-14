import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    # WAL：读不阻塞写，补写回放与目录读取可同时进行
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_tx(db_path: str | None = None):
    """排他写事务：BEGIN IMMEDIATE 先抢写锁，再读漏印集合，避免双写同一行。

    双人仓管并发补写时，第二个请求会等待第一个提交，随后看到目标行已盖印，
    被判定为越界请求并整笔回滚，不会覆盖同一行字段。
    """
    conn = connect(db_path)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS watermark_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  column_name TEXT NOT NULL,
  version_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('现行','停用')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS run_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  run_code TEXT NOT NULL UNIQUE,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待盖印','已盖印','已作废')),
  watermark_value TEXT,
  stamped_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_run_code TEXT NOT NULL,
  downstream_run_code TEXT NOT NULL,
  UNIQUE(upstream_run_code, downstream_run_code)
);

CREATE TABLE IF NOT EXISTS gap_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  expected_count INTEGER NOT NULL,
  stamped_count INTEGER NOT NULL,
  gap_count INTEGER NOT NULL,
  missing_count INTEGER NOT NULL DEFAULT 0,
  consistent INTEGER NOT NULL DEFAULT 1,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS refill_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  watermark_value TEXT NOT NULL,
  scope_ids TEXT NOT NULL,
  applied_ids TEXT NOT NULL,
  retained_ids TEXT NOT NULL,
  retained_snapshot TEXT NOT NULL,
  request_snapshot TEXT NOT NULL DEFAULT '[]',
  scope_token TEXT NOT NULL,
  expected_count INTEGER NOT NULL,
  applied_count INTEGER NOT NULL,
  retained_count INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('已落档','已拒绝')),
  reason_code TEXT,
  note TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS refill_scope_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  refill_run_id INTEGER NOT NULL REFERENCES refill_runs(id) ON DELETE CASCADE,
  batch_id INTEGER NOT NULL,
  run_code TEXT NOT NULL,
  table_name TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  outcome TEXT NOT NULL,
  reason_code TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dead_letters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  refill_run_id INTEGER,
  table_name TEXT NOT NULL DEFAULT '',
  watermark_value TEXT NOT NULL DEFAULT '',
  request_scope TEXT NOT NULL DEFAULT '[]',
  batch_ids TEXT NOT NULL DEFAULT '[]',
  reason_code TEXT NOT NULL,
  message TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT '待处理',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS refill_replays (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  refill_run_id INTEGER NOT NULL REFERENCES refill_runs(id) ON DELETE CASCADE,
  round_no INTEGER NOT NULL,
  applied_count INTEGER NOT NULL,
  retained_count INTEGER NOT NULL,
  expected_count INTEGER NOT NULL,
  aligned INTEGER NOT NULL,
  reason_code TEXT,
  fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SEED_TEMPLATES = [
    ("dw_orders_d", "wm_batch", 1, "现行"),
    ("dw_payments_d", "wm_batch", 1, "现行"),
    ("dm_gmv_d", "wm_trace", 1, "现行"),
]

SEED_BATCHES = [
    ("dw_orders_d", "ord-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_payments_d", "pay-20261114-01", "2026-11-14", "待盖印", None),
    ("dm_gmv_d", "gmv-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_orders_d", "ord-20261113-01", "2026-11-13", "已盖印", "WM-ORD-131"),
]

SEED_EDGES = [
    ("ord-20261114-01", "gmv-20261114-01"),
    ("pay-20261114-01", "gmv-20261114-01"),
    ("ord-20261113-01", "gmv-20261114-01"),
]


MIGRATIONS = [
    ("gap_reports", "missing_count", "INTEGER NOT NULL DEFAULT 0"),
    ("gap_reports", "consistent", "INTEGER NOT NULL DEFAULT 1"),
    ("refill_runs", "request_snapshot", "TEXT NOT NULL DEFAULT '[]'"),
]


def _migrate(conn) -> None:
    """老库升级：补齐新增列，已有数据不动。"""
    for table, column, ddl in MIGRATIONS:
        cols = {
            r["name"]
            for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if row:
            return
        for table, col, ver, status in SEED_TEMPLATES:
            conn.execute(
                "INSERT INTO watermark_templates(table_name, column_name, version_no, status) "
                "VALUES (?,?,?,?)",
                (table, col, ver, status),
            )
        for table, code, biz, status, wm in SEED_BATCHES:
            conn.execute(
                "INSERT INTO run_batches(table_name, run_code, biz_date, status, watermark_value, stamped_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    table,
                    code,
                    biz,
                    status,
                    wm,
                    "2026-11-13 18:00:00" if status == "已盖印" else None,
                ),
            )
        for up, down in SEED_EDGES:
            conn.execute(
                "INSERT INTO lineage_edges(upstream_run_code, downstream_run_code) VALUES (?,?)",
                (up, down),
            )
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
