"""流水线：采集 → 判意图 → 拟私信 → 建线索。

调度模型：每个平台有独立扫描间隔（抖音 12 小时、小红书 1 天、快手 1 天），
到期的平台才采集；手动「立即抓一轮」无视间隔全量扫。
AI 筛选采用批量模式：一次调用同时判断 20 条评论（意图+私信一起出），
比逐条判断快约 10 倍。
"""
import time

from . import collector, config, db, llm


def _process_comments(comment_ids, product_note):
    """批量判断一批评论的意图并生成线索。"""
    template = db.get_setting("dm_template", "")
    rows = []
    for cid in comment_ids:
        c = db.get_comment(cid)
        if c and (c.get("text") or "").strip():
            rows.append(c)

    for i in range(0, len(rows), config.CLASSIFY_BATCH_SIZE):
        chunk = rows[i:i + config.CLASSIFY_BATCH_SIZE]
        try:
            results = llm.classify_batch([c["text"] for c in chunk],
                                          template=template)
        except Exception:
            # 批量失败降级为逐条
            results = [llm.classify_comment(c["text"]) for c in chunk]
        for c, r in zip(chunk, results):
            if not isinstance(r, dict):
                r = {}
            intent = str(r.get("intent") or "无关")
            score = int(r.get("score") or 0)
            # 无论是否成线索，都记录意图（避免重复判断）
            db.mark_comment_intent(c["id"], intent)
            if intent in ("咨询", "购买") or score >= 60:
                dm = llm._sanitize_dm(r.get("dm") or "")
                if not dm:
                    dm = llm.draft_dm(
                        c["text"], r.get("ask_about") or "",
                        product_note, template=template)
                db.add_lead(c["id"], intent, score,
                            r.get("ask_about") or "", dm)


def run_platform(platform, deep=True, on_progress=None):
    """采集一个平台的所有启用同行。返回新增评论数。"""
    total = 0
    product_note = db.get_setting("product_note", "")
    max_videos = config.DEEP_VIDEOS if deep else config.QUICK_VIDEOS
    comment_scrolls = (config.COMMENT_SCROLLS_DEEP
                       if deep else config.COMMENT_SCROLLS_QUICK)
    targets = [t for t in db.list_targets()
               if (t.get("platform") or "").lower() == platform
               and t.get("enabled")]
    for idx, t in enumerate(targets):
        if on_progress:
            try:
                on_progress(platform, idx + 1, len(targets))
            except Exception:
                pass
        try:
            new_ids = collector.fetch_new_comments(
                t, max_videos, comment_scrolls) or []
        except Exception:
            continue
        _process_comments(new_ids, product_note)
        total += len(new_ids)

    # 兜底：补判"已入库但没判断意图"的评论
    _process_comments(db.unprocessed_comment_ids(limit=300), product_note)
    return total


def run_due_platforms(on_progress=None):
    """定时入口：按各平台扫描间隔，只采集到期的平台。"""
    results = {}
    now = time.time()
    for platform, minutes in config.PLATFORM_SCAN_INTERVALS.items():
        if now - db.get_last_scan(platform) >= minutes * 60:
            try:
                n = run_platform(platform, deep=True, on_progress=on_progress)
                db.set_last_scan(platform, now)
                results[platform] = n
            except Exception:
                pass
    return results


def run_all(deep=True, on_progress=None):
    """手动入口：无视间隔，全平台各扫一轮。"""
    results = {}
    now = time.time()
    for platform in config.PLATFORM_SCAN_INTERVALS.keys():
        try:
            n = run_platform(platform, deep=deep, on_progress=on_progress)
            db.set_last_scan(platform, now)
            results[platform] = n
        except Exception:
            pass
    return results
