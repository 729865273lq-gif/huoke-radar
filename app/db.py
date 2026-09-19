"""SQLite 存储层。"""
import os
import re
import sqlite3
from collections import defaultdict

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,          -- douyin | xiaohongshu
    url TEXT NOT NULL,
    nickname TEXT,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL,
    video_id TEXT,
    comment_id TEXT,
    author_name TEXT,
    author_uid TEXT,
    text TEXT,
    commented_at TEXT,
    fetched_at TEXT DEFAULT (datetime('now','localtime')),
    intent TEXT,
    UNIQUE(target_id, comment_id)
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id INTEGER NOT NULL,
    intent TEXT,
    score INTEGER DEFAULT 0,
    ask_about TEXT,
    dm_draft TEXT,
    status TEXT DEFAULT 'pending',   -- pending|sent|replied|ignored
    created_at TEXT DEFAULT (datetime('now','localtime')),
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS send_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER,
    platform TEXT,
    to_user TEXT,
    ok INTEGER,
    error TEXT,
    sent_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _conn():
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _conn()
    try:
        conn.executescript(SCHEMA)
        # 迁移：老库补 intent 列
        try:
            conn.execute("ALTER TABLE comments ADD COLUMN intent TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass
        conn.commit()
    finally:
        conn.close()


# ---------- targets ----------

def add_target(platform, url, nickname=""):
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO targets(platform, url, nickname) VALUES(?,?,?)",
            (platform, url, nickname),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_targets():
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM targets ORDER BY id DESC").fetchall()]
    finally:
        conn.close()


def list_targets_page(page=1, page_size=10):
    """分页返回同行：{total, page, page_size, items}。"""
    conn = _conn()
    try:
        total = conn.execute("SELECT COUNT(1) FROM targets").fetchone()[0]
        offset = (page - 1) * page_size
        rows = conn.execute(
            "SELECT * FROM targets ORDER BY id DESC LIMIT ? OFFSET ?",
            (page_size, offset)).fetchall()
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [dict(r) for r in rows],
        }
    finally:
        conn.close()


def delete_target(target_id):
    conn = _conn()
    try:
        conn.execute("DELETE FROM targets WHERE id=?", (target_id,))
        conn.commit()
    finally:
        conn.close()


def _norm_target_key(platform, url):
    """同平台 + 同一作者 → 同一 key（忽略链接里的各种参数）。"""
    url = (url or "").strip().rstrip("/")
    if platform == "douyin":
        m = re.search(r"/user/([A-Za-z0-9_-]+)", url)
        if m:
            return "douyin:user:" + m.group(1)
        m2 = re.search(r"/video/(\d+)", url)
        if m2:
            return "douyin:video:" + m2.group(1)
        return "douyin:raw:" + url.split("?")[0]
    if platform == "xiaohongshu":
        m = re.search(r"/user/profile/([0-9a-fA-F]+)", url)
        if m:
            return "xhs:" + m.group(1)
        return "xhs:raw:" + url.split("?")[0]
    if platform == "kuaishou":
        m = re.search(r"/profile/([0-9a-zA-Z_]+)", url)
        if m:
            return "ks:" + m.group(1)
        return "ks:raw:" + url.split("?")[0]
    return "raw:" + url.split("?")[0]


def find_duplicate_target(platform, url):
    """按规范化 key 找已存在的同行（添加时拦截重复用）。"""
    key = _norm_target_key(platform, url)
    conn = _conn()
    try:
        for r in conn.execute("SELECT * FROM targets"):
            d = dict(r)
            if _norm_target_key(d["platform"], d["url"]) == key:
                return d
        return None
    finally:
        conn.close()


def dedup_targets():
    """同平台同一作者去重，保留最早添加的，连带清理其评论与线索。"""
    conn = _conn()
    try:
        groups = defaultdict(list)
        for r in conn.execute("SELECT * FROM targets ORDER BY id"):
            d = dict(r)
            groups[_norm_target_key(d["platform"], d["url"])].append(d)

        removed_t = removed_c = removed_l = 0
        for v in groups.values():
            if len(v) < 2:
                continue
            keep = min(x["id"] for x in v)
            for t in v:
                if t["id"] == keep:
                    continue
                for cr in conn.execute(
                        "SELECT id FROM comments WHERE target_id=?",
                        (t["id"],)):
                    cur = conn.execute(
                        "DELETE FROM leads WHERE comment_id=?", (cr["id"],))
                    removed_l += cur.rowcount
                    cur = conn.execute(
                        "DELETE FROM comments WHERE id=?", (cr["id"],))
                    removed_c += cur.rowcount
                cur = conn.execute(
                    "DELETE FROM targets WHERE id=?", (t["id"],))
                removed_t += cur.rowcount
        conn.commit()
        unique_after = conn.execute(
            "SELECT COUNT(1) FROM targets").fetchone()[0]
        return {
            "removed_targets": removed_t,
            "removed_comments": removed_c,
            "removed_leads": removed_l,
            "unique_after": unique_after,
        }
    finally:
        conn.close()


# ---------- comments ----------

def add_comment(target_id, video_id, comment_id, author_name, author_uid,
                text, commented_at=""):
    """插入一条评论，重复返回 None。"""
    conn = _conn()
    try:
        try:
            cur = conn.execute(
                """INSERT INTO comments
                   (target_id, video_id, comment_id, author_name, author_uid, text, commented_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (target_id, video_id, comment_id, author_name, author_uid,
                 text, commented_at),
            )
            conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None
    finally:
        conn.close()


def get_comment(comment_id):
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def unprocessed_comment_ids(limit=200):
    """已入库但还没做过意图判断的评论 id。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id FROM comments WHERE intent IS NULL ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["id"] for r in rows]
    finally:
        conn.close()


def mark_comment_intent(comment_id, intent):
    conn = _conn()
    try:
        conn.execute(
            "UPDATE comments SET intent=? WHERE id=?", (intent, comment_id))
        conn.commit()
    finally:
        conn.close()


def list_recent_comments(limit=200):
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM comments ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    finally:
        conn.close()


# ---------- leads ----------

def add_lead(comment_id, intent, score, ask_about, dm_draft):
    conn = _conn()
    try:
        cur = conn.execute(
            """INSERT INTO leads(comment_id, intent, score, ask_about, dm_draft)
               VALUES(?,?,?,?,?)""",
            (comment_id, intent, score, ask_about, dm_draft),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_leads(page=1, page_size=20, q=None):
    """分页返回线索：{total, page, page_size, items}。q 可搜索评论/作者/同行/私信。"""
    conn = _conn()
    try:
        where = ""
        params = []
        if q:
            like = "%" + q + "%"
            where = (" WHERE c.text LIKE ? OR c.author_name LIKE ? "
                     "OR t.nickname LIKE ? OR l.dm_draft LIKE ?")
            params = [like, like, like, like]
        total = conn.execute(
            "SELECT COUNT(1) FROM leads l "
            "JOIN comments c ON c.id = l.comment_id "
            "JOIN targets t ON t.id = c.target_id" + where,
            params).fetchone()[0]
        offset = (page - 1) * page_size
        rows = conn.execute(
            """SELECT l.*, c.text AS comment_text, c.author_name, c.author_uid,
                      c.video_id, c.commented_at AS comment_time,
                      c.fetched_at AS fetched_at,
                      t.nickname AS target_nickname, t.platform
               FROM leads l
               JOIN comments c ON c.id = l.comment_id
               JOIN targets t ON t.id = c.target_id""" + where +
            " ORDER BY l.id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [dict(r) for r in rows],
        }
    finally:
        conn.close()


def delete_lead(lead_id):
    conn = _conn()
    try:
        conn.execute("DELETE FROM leads WHERE id=?", (lead_id,))
        conn.commit()
    finally:
        conn.close()


def get_lead(lead_id):
    """单条线索（含评论与来源信息）。"""
    conn = _conn()
    try:
        r = conn.execute(
            """SELECT l.*, c.text AS comment_text, c.author_name, c.author_uid,
                      c.video_id, c.commented_at AS comment_time,
                      c.fetched_at AS fetched_at,
                      t.nickname AS target_nickname, t.platform
               FROM leads l
               JOIN comments c ON c.id = l.comment_id
               JOIN targets t ON t.id = c.target_id
               WHERE l.id = ?""",
            (lead_id,),
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def update_lead_draft(lead_id, draft):
    conn = _conn()
    try:
        conn.execute("UPDATE leads SET dm_draft=? WHERE id=?", (draft, lead_id))
        conn.commit()
    finally:
        conn.close()


def update_lead_status(lead_id, status):
    conn = _conn()
    try:
        if status == "sent":
            conn.execute(
                "UPDATE leads SET status=?, sent_at=datetime('now','localtime') WHERE id=?",
                (status, lead_id),
            )
        else:
            conn.execute("UPDATE leads SET status=? WHERE id=?", (status, lead_id))
        conn.commit()
    finally:
        conn.close()


def log_send(lead_id, platform, to_user, ok, error=""):
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO send_log(lead_id, platform, to_user, ok, error) VALUES(?,?,?,?,?)",
            (lead_id, platform, to_user, 1 if ok else 0, error),
        )
        conn.commit()
    finally:
        conn.close()


def last_send_result(lead_id):
    """上一次发送结果：1=成功 0=失败 None=还没发过。"""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT ok FROM send_log WHERE lead_id=? ORDER BY id DESC LIMIT 1",
            (lead_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ---------- settings ----------

def get_setting(key, default=""):
    conn = _conn()
    try:
        r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default
    finally:
        conn.close()


def get_last_scan(platform):
    try:
        return float(get_setting("last_scan_%s" % platform, "0"))
    except ValueError:
        return 0.0


def set_last_scan(platform, ts):
    set_setting("last_scan_%s" % platform, str(ts))


def set_setting(key, value):
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()
