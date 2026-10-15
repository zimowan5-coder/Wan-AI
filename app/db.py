"""SQLite 存储层。

设计取舍（v0.1）：
- 单文件 SQLite + WAL 模式：零运维、Windows 双击即用，不引入 Postgres/Redis。
- 全局写锁 + busy_timeout：单进程多线程（Web + Worker）下安全。
- 所有金额统一存 CNY（元），kind/is_simulated 区分真实与模拟——模拟收入永远不许混进真实盈利。
"""
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS modules(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'ready',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    module_key TEXT,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    payload_json TEXT DEFAULT '{}',
    status TEXT DEFAULT 'pending',
    priority INTEGER DEFAULT 0,
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    timeout_sec INTEGER DEFAULT 300,
    needs_approval INTEGER DEFAULT 0,
    approval_id INTEGER,
    created_at TEXT,
    updated_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    result_summary TEXT DEFAULT '',
    error TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS task_logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    ts TEXT,
    level TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id);
CREATE TABLE IF NOT EXISTS research_reports(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    topic TEXT,
    report_md TEXT,
    sources_json TEXT,
    stats_json TEXT,
    ai_mode TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS ledger(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    kind TEXT NOT NULL,
    amount_cny REAL NOT NULL,
    currency TEXT DEFAULT 'CNY',
    category TEXT DEFAULT '',
    description TEXT DEFAULT '',
    ref_type TEXT,
    ref_id INTEGER,
    is_simulated INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ledger_ts ON ledger(ts);
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module_key TEXT,
    customer TEXT DEFAULT '',
    item TEXT DEFAULT '',
    amount_cny REAL DEFAULT 0,
    status TEXT DEFAULT 'paid',
    is_simulated INTEGER DEFAULT 0,
    created_at TEXT,
    notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS approvals(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    detail_json TEXT DEFAULT '{}',
    amount_cny REAL DEFAULT 0,
    status TEXT DEFAULT 'pending',
    decided_at TEXT,
    decided_by TEXT DEFAULT '',
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS artifacts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    name TEXT,
    path TEXT,
    mime TEXT DEFAULT '',
    created_at TEXT
);
"""

_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def jload(s, default=None):
    if not s:
        return default if default is not None else {}
    try:
        return json.loads(s)
    except Exception:
        return default if default is not None else {}


def row_to_dict(row):
    return dict(row) if row is not None else None


class Database:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        with _lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def execute(self, sql, params=()):
        with _lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql, params=()):
        with _lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        with _lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self):
        with _lock:
            self._conn.close()
