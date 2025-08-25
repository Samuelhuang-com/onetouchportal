# data/db.py
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterable, Dict, Any
import json
from datetime import datetime

# --- 路徑 ---
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DIR = os.path.join(ROOT_DIR, "data")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "auth.db")


# --- 連線 ---
@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- 建表 ---
def init_db():
    schema = r"""
    PRAGMA journal_mode=WAL;

    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      loginname     TEXT UNIQUE NOT NULL,
      password      TEXT NOT NULL,
      display_name  TEXT,
      role          TEXT DEFAULT 'guest',  -- admin / manager / user / guest
      status        INTEGER DEFAULT 1,     -- 1 啟用, 0 停用
      created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
      updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS permission_keys (
      key TEXT PRIMARY KEY
    );

    CREATE TABLE IF NOT EXISTS user_permissions (
      user_id   INTEGER NOT NULL,
      perm_key  TEXT    NOT NULL,
      value     INTEGER NOT NULL CHECK (value IN (0,1)),
      PRIMARY KEY (user_id, perm_key),
      FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
      FOREIGN KEY (perm_key) REFERENCES permission_keys(key) ON DELETE CASCADE
    );

    -- 角色
    CREATE TABLE IF NOT EXISTS roles (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE NOT NULL,
      display_name TEXT,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS role_permissions (
      role_id INTEGER NOT NULL,
      perm_key TEXT NOT NULL,
      value   INTEGER NOT NULL CHECK (value IN (0,1)),
      PRIMARY KEY (role_id, perm_key),
      FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE,
      FOREIGN KEY (perm_key) REFERENCES permission_keys(key) ON DELETE CASCADE
    );

    -- 群組
    CREATE TABLE IF NOT EXISTS groups (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE NOT NULL,
      display_name TEXT,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS group_members (
      group_id INTEGER NOT NULL,
      user_id  INTEGER NOT NULL,
      PRIMARY KEY (group_id, user_id),
      FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
      FOREIGN KEY (user_id)  REFERENCES users(id)  ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS group_permissions (
      group_id INTEGER NOT NULL,
      perm_key TEXT NOT NULL,
      value    INTEGER NOT NULL CHECK (value IN (0,1)),
      PRIMARY KEY (group_id, perm_key),
      FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
      FOREIGN KEY (perm_key) REFERENCES permission_keys(key) ON DELETE CASCADE
    );

    -- 稽核軌跡
    CREATE TABLE IF NOT EXISTS audit_logs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      actor_login TEXT,
      action TEXT,
      target TEXT,
      details TEXT,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """
    with get_conn() as c:
        c.executescript(schema)


# --- 工具 ---
def upsert_permission_keys(keys: Iterable[str]):
    keys = [k for k in keys if k]
    if not keys:
        return
    with get_conn() as c:
        c.executemany(
            "INSERT OR IGNORE INTO permission_keys(key) VALUES (?)",
            [(k,) for k in keys],
        )


def audit_log(actor: str, action: str, target: str, details: Dict[str, Any]):
    with get_conn() as c:
        c.execute(
            "INSERT INTO audit_logs(actor_login,action,target,details,created_at) VALUES (?,?,?,?,?)",
            (
                actor or "",
                action,
                target,
                json.dumps(details, ensure_ascii=False),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )


def seed_default_roles():
    """不存在才建立預設角色"""
    defaults = [
        ("guest", "訪客"),
        ("user", "一般使用者"),
        ("manager", "主管"),
        ("admin", "系統管理員"),
    ]
    with get_conn() as c:
        for name, disp in defaults:
            c.execute(
                "INSERT OR IGNORE INTO roles(name, display_name) VALUES(?,?)",
                (name, disp),
            )


def set_role_perms(role_name: str, perms: Dict[str, int]):
    with get_conn() as c:
        r = c.execute("SELECT id FROM roles WHERE name=?", (role_name,)).fetchone()
        if not r:
            return
        rid = r["id"]
        for k, v in perms.items():
            c.execute(
                """
            INSERT INTO role_permissions(role_id,perm_key,value)
            VALUES (?,?,?)
            ON CONFLICT(role_id,perm_key) DO UPDATE SET value=excluded.value
            """,
                (rid, k, 1 if v else 0),
            )
