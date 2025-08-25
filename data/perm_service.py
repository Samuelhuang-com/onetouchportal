# data/perm_service.py
from typing import Dict, Set
from data.db import get_conn

def _get_user_id(loginname: str) -> int | None:
    with get_conn() as c:
        row = c.execute("SELECT id FROM users WHERE loginname=? AND status=1", (loginname,)).fetchone()
        return row["id"] if row else None

def _get_role_id_of_user(uid: int) -> int | None:
    with get_conn() as c:
        row = c.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
        if not row: return None
        rr = c.execute("SELECT id FROM roles WHERE name=?", (row["role"],)).fetchone()
        return rr["id"] if rr else None

def _get_group_ids(uid: int) -> Set[int]:
    with get_conn() as c:
        rows = c.execute("SELECT group_id FROM group_members WHERE user_id=?", (uid,)).fetchall()
        return {r["group_id"] for r in rows}

def get_effective_permissions(loginname: str) -> Dict[str, bool]:
    uid = _get_user_id(loginname)
    if uid is None: return {}
    result: Dict[str, bool] = {}
    with get_conn() as c:
        # 1) 角色
        rid = _get_role_id_of_user(uid)
        if rid:
            for r in c.execute("SELECT perm_key,value FROM role_permissions WHERE role_id=?", (rid,)):
                result[r["perm_key"]] = bool(r["value"])
        # 2) 群組（OR）
        for gid in _get_group_ids(uid):
            for r in c.execute("SELECT perm_key,value FROM group_permissions WHERE group_id=?", (gid,)):
                if bool(r["value"]):
                    result[r["perm_key"]] = True
        # 3) 個人覆寫（最高優先）
        for r in c.execute("SELECT perm_key,value FROM user_permissions WHERE user_id=?", (uid,)):
            result[r["perm_key"]] = bool(r["value"])
    return result

def has_permission(loginname: str, key: str) -> bool:
    if not loginname: return False
    with get_conn() as c:
        u = c.execute("SELECT role FROM users WHERE loginname=? AND status=1", (loginname,)).fetchone()
        if not u: return False
        if u["role"] == "admin":  # admin 全開
            return True
    return get_effective_permissions(loginname).get(key, False)
