"""获客雷达 · 主入口：本地网页后台。"""
import os
import threading
import time
from contextlib import asynccontextmanager

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import collector, config, db, pipeline, sender

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(BASE_DIR, "static", "index.html")

db.init_db()


class TargetIn(BaseModel):
    platform: str
    url: str
    nickname: str = ""


class LeadUpdateIn(BaseModel):
    status: str = None
    dm_draft: str = None


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _resolve_short_url(url):
    """把 xhslink / v.douyin.com / v.kuaishou.com 短链解析成真实地址。"""
    if not any(k in url for k in ("xhslink.com", "v.douyin.com", "v.kuaishou.com")):
        return url
    try:
        resp = requests.get(url, headers={"User-Agent": _UA},
                            timeout=15, allow_redirects=True)
        final = resp.url or url
        if final != url:
            return final
    except Exception:
        pass
    return url


class LoginIn(BaseModel):
    platform: str


_login_busy = False
_login_lock = threading.Lock()
login_cache = {
    "douyin": db.get_setting("login_douyin", "") == "1",
    "xiaohongshu": db.get_setting("login_xiaohongshu", "") == "1",
    "kuaishou": db.get_setting("login_kuaishou", "") == "1",
    "shipinhao": db.get_setting("login_shipinhao", "") == "1",
}

_collect_lock = threading.Lock()
_collect_state = {"running": False, "last": None, "progress": ""}

_PLATFORM_CN = {"douyin": "抖音", "xiaohongshu": "小红书", "kuaishou": "快手"}


def _progress(platform, done, total):
    _collect_state["progress"] = "%s %s/%s" % (
        _PLATFORM_CN.get(platform, platform), done, total)
    try:
        db.set_setting("collect_progress", _collect_state["progress"])
    except Exception:
        pass


def _do_collect_all(deep):
    """手动全量扫（无视平台间隔）。"""
    if not _collect_lock.acquire(blocking=False):
        return False
    _collect_state["running"] = True
    _collect_state["progress"] = ""
    try:
        results = pipeline.run_all(deep=deep, on_progress=_progress)
        _collect_state["last"] = {
            "results": results,
            "time": time.strftime("%H:%M:%S"),
        }
        _collect_state["progress"] = ""
        print("[collect] 手动全量轮完成: %s" % results, flush=True)
        return True
    finally:
        _collect_state["running"] = False
        _collect_lock.release()


def _do_collect_scheduled():
    """定时轮：只扫到期的平台。"""
    if not _collect_lock.acquire(blocking=False):
        return False
    _collect_state["running"] = True
    _collect_state["progress"] = ""
    try:
        results = pipeline.run_due_platforms(on_progress=_progress)
        _collect_state["last"] = {
            "results": results,
            "time": time.strftime("%H:%M:%S"),
        }
        _collect_state["progress"] = ""
        if results:
            print("[collect] 定时轮完成: %s" % results, flush=True)
        return True
    finally:
        _collect_state["running"] = False
        _collect_lock.release()


def _scheduler_loop():
    time.sleep(8)  # 等服务器起来
    while True:
        try:
            _do_collect_scheduled()
        except Exception:
            pass
        time.sleep(config.QUICK_INTERVAL_MINUTES * 60)


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    yield


app = FastAPI(title="获客雷达", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def index():
    with open(INDEX_HTML, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "llm_configured": bool(config.DEEPSEEK_API_KEY),
        "douyin_logged_in": login_cache.get("douyin", False),
        "xiaohongshu_logged_in": login_cache.get("xiaohongshu", False),
        "kuaishou_logged_in": login_cache.get("kuaishou", False),
        "shipinhao_logged_in": login_cache.get("shipinhao", False),
        "xhs_collector_logged_in": db.get_setting("login_xhs_collector", "") == "1",
    }


# ---------- 登录 ----------

@app.post("/api/login")
def login(body: LoginIn):
    global _login_busy
    platform = body.platform.strip().lower()
    if platform not in ("douyin", "xiaohongshu", "kuaishou", "shipinhao",
                        "xiaohongshu_collector"):
        raise HTTPException(400, "不支持的 platform")
    with _login_lock:
        if _login_busy:
            raise HTTPException(409, "登录窗口已打开，请先在弹出的浏览器里扫码")
        _login_busy = True

    def _run():
        global _login_busy, login_cache
        try:
            if platform == "xiaohongshu_collector":
                ok = sender.ensure_login("xiaohongshu", role="xhs_collector")
                db.set_setting("login_xhs_collector", "1" if ok else "0")
                print("[login] 小红书采集号登录结果=%s" % ok, flush=True)
            else:
                ok = sender.ensure_login(platform)
                login_cache[platform] = bool(ok)
                db.set_setting("login_%s" % platform, "1" if ok else "0")
                print("[login] %s 登录流程结束，结果=%s" % (platform, ok), flush=True)
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            _login_busy = False

    threading.Thread(target=_run, daemon=True).start()
    return {
        "started": True,
        "message": "已弹出登录窗口，请用手机扫码（最长等待 3 分钟）",
    }


# ---------- 同行管理 ----------

@app.get("/api/targets")
def list_targets(page: int = 1, page_size: int = 10):
    return db.list_targets_page(page=page, page_size=page_size)


@app.post("/api/targets")
def create_target(t: TargetIn):
    platform = t.platform.strip().lower()
    if platform not in ("douyin", "xiaohongshu", "kuaishou"):
        raise HTTPException(400, "platform 只能是 douyin / xiaohongshu / kuaishou")
    if not t.url.strip():
        raise HTTPException(400, "请填写主页链接")
    resolved = _resolve_short_url(t.url.strip())
    dup = db.find_duplicate_target(platform, resolved)
    if dup:
        name = dup.get("nickname") or dup.get("url") or ""
        raise HTTPException(409, "这个同行已经在列表里了（%s）" % name[:40])
    tid = db.add_target(platform, resolved, t.nickname.strip())
    return {"id": tid}


@app.delete("/api/targets/{tid}")
def remove_target(tid: int):
    db.delete_target(tid)
    return {"ok": True}


@app.post("/api/targets/dedup")
def dedup_targets():
    """同平台同一作者去重。"""
    return db.dedup_targets()


@app.post("/api/logout")
def logout(body: LoginIn):
    """退出某平台登录（换账号用）。"""
    platform = body.platform.strip().lower()
    if platform not in ("douyin", "xiaohongshu", "kuaishou"):
        raise HTTPException(400, "不支持的 platform")
    with _login_lock:
        if _login_busy:
            raise HTTPException(409, "有登录流程进行中，请稍候")

    def _run():
        global _login_busy, login_cache
        try:
            sender.logout(platform)
            login_cache[platform] = False
            db.set_setting("login_%s" % platform, "0")
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            _login_busy = False

    threading.Thread(target=_run, daemon=True).start()
    return {"started": True,
            "message": "正在退出…完成后重新点「登录」扫码换新账号"}


# ---------- 私信话术模板 ----------

class TemplateIn(BaseModel):
    template: str = ""


class FollowupsIn(BaseModel):
    m2: str = ""
    m3: str = ""


@app.get("/api/dm-template")
def get_dm_template():
    return {"template": db.get_setting("dm_template", "")}


@app.post("/api/dm-template")
def set_dm_template(body: TemplateIn):
    db.set_setting("dm_template", (body.template or "").strip())
    return {"ok": True}


@app.get("/api/dm-followups")
def get_dm_followups():
    return {
        "m2": db.get_setting("dm_followup_m2", ""),
        "m3": db.get_setting("dm_followup_m3", ""),
    }


@app.post("/api/dm-followups")
def set_dm_followups(body: FollowupsIn):
    db.set_setting("dm_followup_m2", (body.m2 or "").strip())
    db.set_setting("dm_followup_m3", (body.m3 or "").strip())
    return {"ok": True}


# ---------- 采集 ----------

@app.post("/api/collect")
def collect_now():
    """立即全量抓一轮（后台执行，完成后线索自动出现）。"""
    if _collect_state["running"]:
        return {"started": False, "message": "已有抓取任务在运行，请稍候"}
    threading.Thread(target=_do_collect_all, args=(True,), daemon=True).start()
    return {"started": True, "message": "全量抓取已在后台开始，完成后线索会自动出现"}


@app.get("/api/collect/status")
def collect_status():
    return _collect_state


# ---------- 线索 ----------

@app.get("/api/leads")
def list_leads(page: int = 1, page_size: int = 20, q: str = None):
    return db.list_leads(page=page, page_size=page_size, q=q)


@app.delete("/api/leads/{lid}")
def delete_lead(lid: int):
    db.delete_lead(lid)
    return {"ok": True}


@app.patch("/api/leads/{lid}")
def update_lead(lid: int, body: LeadUpdateIn):
    if body.status is not None:
        if body.status not in ("pending", "sent", "replied", "ignored"):
            raise HTTPException(400, "非法状态")
        db.update_lead_status(lid, body.status)
    if body.dm_draft is not None:
        db.update_lead_draft(lid, body.dm_draft)
    return {"ok": True}


_send_lock = threading.Lock()


@app.post("/api/leads/{lid}/send")
def send_lead(lid: int):
    if not _send_lock.acquire(blocking=False):
        raise HTTPException(429, "上一条私信正在发送中，请等它完成（约 30 秒）再点下一条")
    try:
        lead = db.get_lead(lid)
        if not lead:
            raise HTTPException(404, "线索不存在")
        platform = lead["platform"]
        to_user = lead["author_uid"] or lead["author_name"]
        text = lead["dm_draft"] or ""
        try:
            sender.send_dm(platform, to_user, text,
                           nickname=lead["author_name"] or "")
        except NotImplementedError:
            raise HTTPException(501, "发送模块开发中，下个版本接入")
        except sender.SendLimitExceeded as e:
            raise HTTPException(429, str(e))
        except Exception as e:
            msg = str(e)
            if "可能关闭了陌生人私信" in msg:
                # 聊天窗打不开常是页面卡顿（重试就好）——连续两次都失败才标记「私信受限」
                if db.last_send_result(lid) == 0:
                    db.update_lead_status(lid, "dm_blocked")
            if "未登录" in msg:
                login_cache[platform] = False
                db.set_setting("login_%s" % platform, "0")
            db.log_send(lid, platform, to_user, False, str(e))
            raise HTTPException(500, "发送失败：%s" % e)
        login_cache[platform] = True
        db.set_setting("login_%s" % platform, "1")
        db.update_lead_status(lid, "sent")
        db.log_send(lid, platform, to_user, True)
        return {"ok": True}
    finally:
        _send_lock.release()


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=config.PORT, reload=False)
