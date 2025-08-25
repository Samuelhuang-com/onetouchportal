# In database.py

import sqlite3
from contextlib import contextmanager
from typing import Iterable

# 將 DB_PATH 修改為您的資料庫檔案名稱
DB_PATH = "approvals.db"

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    schema = r"""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      loginname TEXT UNIQUE NOT NULL,
      password TEXT NOT NULL,
      display_name TEXT,
      role TEXT DEFAULT 'guest',
      status INTEGER DEFAULT 1,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS permission_keys (
      key TEXT PRIMARY KEY
    );
    CREATE TABLE IF NOT EXISTS user_permissions (
      user_id INTEGER NOT NULL,
      perm_key TEXT NOT NULL,
      value INTEGER NOT NULL CHECK (value IN (0,1)),
      PRIMARY KEY (user_id, perm_key),
      FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
      FOREIGN KEY (perm_key) REFERENCES permission_keys(key) ON DELETE CASCADE
    );
    """
    with get_conn() as c:
        c.executescript(schema)

def upsert_permission_keys(keys: Iterable[str]):
    with get_conn() as c:
        c.executemany(
            "INSERT OR IGNORE INTO permission_keys(key) VALUES(?)",
            [(k,) for k in keys]
        )
