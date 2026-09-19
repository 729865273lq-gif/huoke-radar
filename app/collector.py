"""评论区采集：抖音 + 小红书。

思路：用 Playwright 打开目标博主主页/视频页，拦截页面自己发出的
视频列表 / 评论列表 JSON 接口，解析评论后去重入库。
通过滚动加载更多视频与更多评论页，覆盖更深。

参数 max_videos / comment_scrolls 由快速轮与深度轮分别控制。
"""
import base64
import re
import time

from . import config, db
from .browser import BrowserSession

PAGE_WAIT_MS = 6000   # 等页面数据加载
GAP_MS = 2000         # 每条视频之间的间隔，降低风控
PROFILE_SCROLL_MAX = 15


def fetch_new_comments(target, max_videos=10, comment_scrolls=2):
    """抓取一个 target（同行博主）的新评论并入库。返回新增评论 id 列表。"""
    platform = (target.get("platform") or "").lower()
    if platform == "douyin":
        return _fetch_douyin(target, max_videos, comment_scrolls)
    if platform == "xiaohongshu":
        return _fetch_xiaohongshu(target, max_videos, comment_scrolls)
    if platform == "kuaishou":
        return _fetch_kuaishou(target, max_videos, comment_scrolls)
    return []


def _ts(sec):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(sec)))
    except Exception:
        return ""


def _ts_recent(ts):
    """评论时间是否在保留期内。ts 未知(0)按保留处理；秒/毫秒自动识别。"""
    if not ts:
        return True
    try:
        ts = float(ts)
    except Exception:
        return True
    if ts > 10 ** 12:  # 毫秒
        ts = ts / 1000
    return (time.time() - ts) <= config.COMMENT_MAX_AGE_DAYS * 86400


# ---------------- 抖音 ----------------

def _fetch_douyin(target, max_videos, comment_scrolls):
    new_ids = []
    session = BrowserSession(headless=True)
    try:
        page = session.new_page()
        posted = {}
        author_uid = None

        def on_posted(resp):
            if "/aweme/v1/web/aweme/post/" in resp.url and resp.status == 200:
                try:
                    j = resp.json()
                except Exception:
                    return
                for a in (j.get("aweme_list") or []):
                    aid = str(a.get("aweme_id") or "")
                    if aid:
                        posted.setdefault(aid, a.get("desc") or "")

        def on_detail(resp):
            nonlocal author_uid
            if "/aweme/v1/web/aweme/detail/" in resp.url and resp.status == 200:
                try:
                    j = resp.json()
                except Exception:
                    return
                a = (j.get("aweme_detail") or {}).get("author") or {}
                if a.get("sec_uid"):
                    author_uid = a.get("sec_uid")

        def on_comments(resp):
            if "/aweme/v1/web/comment/list/" in resp.url and resp.status == 200:
                m = re.search(r"aweme_id=(\d+)", resp.url)
                vid = m.group(1) if m else ""
                try:
                    j = resp.json()
                except Exception:
                    return
                for c in (j.get("comments") or []):
                    if not _ts_recent(c.get("create_time") or 0):
                        continue  # 太老的评论不要
                    u = c.get("user") or {}
                    cid = db.add_comment(
                        target["id"], vid,
                        str(c.get("cid") or ""),
                        u.get("nickname") or "",
                        u.get("sec_uid") or u.get("uid") or "",
                        c.get("text") or "",
                        _ts(c.get("create_time") or 0),
                    )
                    if cid:
                        new_ids.append(cid)

        page.on("response", on_posted)
        page.on("response", on_detail)
        page.on("response", on_comments)

        # 打开目标主页（自动解析短链/视频页 → 作者主页）
        try:
            page.goto(target["url"], timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(PAGE_WAIT_MS)
        except Exception:
            return []

        if not posted and author_uid:
            try:
                page.goto("https://www.douyin.com/user/%s" % author_uid,
                          timeout=60000, wait_until="domcontentloaded")
                page.wait_for_timeout(PAGE_WAIT_MS)
            except Exception:
                return []

        # 滚动加载更多视频，直到凑够 max_videos
        for _ in range(PROFILE_SCROLL_MAX):
            if len(posted) >= max_videos:
                break
            try:
                page.mouse.wheel(0, 1400)
                page.wait_for_timeout(2500)
            except Exception:
                break

        aweme_ids = list(posted.keys())[:max_videos]

        # 逐视频读取评论（滚动翻页 comment_scrolls 次）
        for aid in aweme_ids:
            try:
                page.goto(
                    "https://www.douyin.com/video/%s" % aid,
                    timeout=60000, wait_until="domcontentloaded",
                )
                page.wait_for_timeout(PAGE_WAIT_MS)
                for _ in range(comment_scrolls):
                    page.mouse.wheel(0, 1200)
                    page.wait_for_timeout(2500)
            except Exception:
                continue
            time.sleep(GAP_MS / 1000)
    finally:
        session.close()
    return new_ids


# ---------------- 小红书 ----------------

def _fetch_xiaohongshu(target, max_videos, comment_scrolls):
    m = re.search(r"/user/profile/([0-9a-fA-F]+)", target["url"])
    if not m:
        return []
    new_ids = []
    # 小红书读评论需要登录，但用【独立采集档案】，避免采集的机器行为
    # 把发私信用的登录态连累掉（平台风控会吊销会话）
    session = BrowserSession(headless=True, role="xhs_collector")
    try:
        page = session.new_page()
        notes = []

        def on_posted(resp):
            if "/api/sns/web/v1/user_posted" in resp.url and resp.status == 200:
                try:
                    j = resp.json()
                except Exception:
                    return
                for n in (j.get("data", {}).get("notes") or []):
                    nid = n.get("note_id") or n.get("id")
                    if nid:
                        notes.append((str(nid), n.get("xsec_token") or ""))

        def on_comments(resp):
            if "/api/sns/web/v2/comment/page" in resp.url and resp.status == 200:
                m2 = re.search(r"note_id=([0-9a-fA-F]+)", resp.url)
                vid = m2.group(1) if m2 else ""
                try:
                    j = resp.json()
                except Exception:
                    return
                for c in (j.get("data", {}).get("comments") or []):
                    if not _ts_recent(c.get("create_time") or 0):
                        continue  # 太老的评论不要
                    u = c.get("user_info") or {}
                    cid = db.add_comment(
                        target["id"], vid,
                        str(c.get("id") or ""),
                        u.get("nickname") or "",
                        u.get("user_id") or "",
                        c.get("content") or "",
                        _ts((c.get("create_time") or 0) / 1000),
                    )
                    if cid:
                        new_ids.append(cid)

        page.on("response", on_posted)
        page.on("response", on_comments)
        try:
            page.goto(target["url"], timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(PAGE_WAIT_MS)
        except Exception:
            return []

        # 记录采集档案的登录状态（登录被风控踢掉时后台能看到）
        try:
            from .sender import _xhs_logged_in
            ok = _xhs_logged_in(page)
            db.set_setting("login_xhs_collector", "1" if ok else "0")
            if not ok:
                return []
        except Exception:
            pass

        for _ in range(PROFILE_SCROLL_MAX):
            if len(notes) >= max_videos:
                break
            try:
                page.mouse.wheel(0, 1400)
                page.wait_for_timeout(2500)
            except Exception:
                break

        for nid, xt in notes[:max_videos]:
            # 小红书笔记必须有 xsec_token 才能网页打开
            note_url = "https://www.xiaohongshu.com/explore/%s" % nid
            if xt:
                import urllib.parse
                note_url += "?xsec_token=" + urllib.parse.quote(xt)
            try:
                page.goto(
                    note_url,
                    timeout=60000, wait_until="domcontentloaded",
                )
                page.wait_for_timeout(PAGE_WAIT_MS)
                for _ in range(comment_scrolls):
                    page.mouse.wheel(0, 1200)
                    page.wait_for_timeout(2500)
            except Exception:
                continue
            time.sleep(GAP_MS / 1000)
    finally:
        session.close()
    return new_ids


# ---------------- 快手 ----------------

def _parse_ks_comments(j):
    """从快手 GraphQL 响应里挖评论列表（rootCommentsV2 结构）。"""
    out = []
    d = j.get("data") or {}
    v = d.get("visionCommentList")
    if not isinstance(v, dict):
        return out
    for list_key in ("rootCommentsV2", "rootComments", "subCommentsV2", "subComments"):
        for c in (v.get(list_key) or []):
            if not isinstance(c, dict):
                continue
            author = c.get("author") or {}
            text = (c.get("content") or c.get("contentText")
                    or c.get("text") or "").strip()
            if not text:
                continue
            ts = (c.get("timestamp") or c.get("createTime")
                  or c.get("cTime") or 0)
            if not _ts_recent(ts):
                continue  # 太老的评论不要
            out.append({
                "comment_id": str(c.get("commentId") or c.get("id") or ""),
                "author_name": (c.get("authorName")
                                or author.get("name") or ""),
                "author_uid": str(c.get("authorId")
                                  or author.get("id") or ""),
                "text": text,
                "ts": ts,
            })
    return out


def _ks_photo_id_from_img_url(url):
    """从快手封面图 URL 解码作品 photoId。

    形如 .../upic/2026/09/08/19/B{base64}_B{hash}
    其中 base64(去首字母B) 解出 {时间}_{作者id}_{photoId}_{..}_{..}
    """
    m = re.search(r"/upic/[^/]+/[^/]+/[^/]+/[^/]+/([A-Za-z0-9_]+)_B", url)
    if not m:
        return None
    enc = m.group(1)
    if enc.startswith("B"):
        enc = enc[1:]
    try:
        dec = base64.b64decode(enc, validate=False).decode("ascii", "replace")
    except Exception:
        return None
    parts = dec.split("_")
    if len(parts) >= 3 and parts[2].isdigit():
        return parts[2]
    return None


def _ks_profile_photo_ids(page):
    """从主页封面图里提取作品 id 列表。"""
    try:
        srcs = page.evaluate("""() => [...document.querySelectorAll('img')]
            .map(i => i.getAttribute('src') || '')
            .filter(s => s.includes('/upic/'))""")
    except Exception:
        return []
    ids = []
    for s in srcs:
        pid = _ks_photo_id_from_img_url(s)
        if pid and pid not in ids:
            ids.append(pid)
    return ids


def _fetch_kuaishou(target, max_videos, comment_scrolls):
    new_ids = []
    # 快手看别人作品列表必须登录，用发送档案；没登录就跳过
    session = BrowserSession(headless=True, role="sender")
    try:
        page = session.new_page()
        current_video = ""

        def on_graphql(resp):
            if "/graphql" not in resp.url or resp.status != 200:
                return
            try:
                j = resp.json()
            except Exception:
                return
            for c in _parse_ks_comments(j):
                cid = db.add_comment(
                    target["id"], current_video,
                    c["comment_id"],
                    c["author_name"],
                    c["author_uid"],
                    c["text"],
                    _ts(c["ts"]),
                )
                if cid:
                    new_ids.append(cid)

        page.on("response", on_graphql)

        try:
            page.goto(target["url"], timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(PAGE_WAIT_MS)
        except Exception:
            return []

        # 目标是视频页/短链时，跳转到作者主页
        if "/profile/" not in target["url"]:
            try:
                profile = page.evaluate("""() => {
                    const a = [...document.querySelectorAll('a[href]')]
                        .find(x => (x.getAttribute('href') || '').includes('/profile/'));
                    return a ? a.getAttribute('href') : '';
                }""")
                if profile:
                    url = ("https://www.kuaishou.com" + profile
                           if profile.startswith("/") else profile)
                    page.goto(url, timeout=60000, wait_until="domcontentloaded")
                    page.wait_for_timeout(PAGE_WAIT_MS)
            except Exception:
                pass

        # 滚动主页加载视频卡片
        for _ in range(PROFILE_SCROLL_MAX):
            ids = _ks_profile_photo_ids(page)
            if len(ids) >= max_videos:
                break
            try:
                page.mouse.wheel(0, 1400)
                page.wait_for_timeout(2500)
            except Exception:
                break

        photo_ids = ids[:max_videos]
        for pid in photo_ids:
            current_video = pid
            try:
                page.goto(
                    "https://www.kuaishou.com/short-video/%s" % pid,
                    timeout=60000, wait_until="domcontentloaded",
                )
                page.wait_for_timeout(PAGE_WAIT_MS)
                for _ in range(comment_scrolls):
                    page.mouse.wheel(0, 1200)
                    page.wait_for_timeout(2500)
            except Exception:
                continue
            time.sleep(GAP_MS / 1000)
    finally:
        session.close()
    return new_ids
