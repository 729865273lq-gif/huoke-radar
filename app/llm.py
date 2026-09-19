"""DeepSeek LLM 封装：评论意图识别 + 个性化私信生成。

无 API Key 或调用失败时，自动降级为关键词规则判断，保证工具不崩。
"""
import json
import re

import requests

from . import config


class LLMUnavailable(Exception):
    pass


def _chat(messages, temperature=0.2, max_tokens=500):
    if not config.DEEPSEEK_API_KEY:
        raise LLMUnavailable("未配置 DEEPSEEK_API_KEY，请在 .env 里填写")
    url = config.DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {
        "Authorization": "Bearer " + config.DEEPSEEK_API_KEY,
        "Content-Type": "application/json",
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


# 关键词兜底（无 Key 时用）
_CONSULT_WORDS = [
    "多少钱", "价格", "怎么买", "怎么合作", "求带", "私我", "联系方式",
    "咨询", "加盟", "代理", "报名", "想学", "怎么做", "靠谱吗", "了解一下",
    "加个", "微信", "vx", "怎么联系", "有群吗", "带带我", "求分享", "在哪买",
]


def classify_comment(text):
    """判断一条评论的意图。

    返回 {"intent": 咨询|购买|闲聊|负面|无关, "score": 0-100, "ask_about": str}
    """
    if not text or not text.strip():
        return {"intent": "无关", "score": 0, "ask_about": ""}
    try:
        prompt = (
            "你是获客助手。判断下面这条抖音/小红书评论区留言，只输出 JSON，不要解释。\n"
            'JSON 字段：{"intent": "咨询|购买|闲聊|负面|无关", '
            '"score": 0到100的购买意向分, "ask_about": "对方在问什么，没有就空字符串"}\n'
            "留言：%s" % text
        )
        raw = _chat(
            [
                {"role": "system", "content": "你只输出合法 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=200,
        )
        m = re.search(r"\{.*\}", raw, re.S)
        obj = json.loads(m.group(0))
        return {
            "intent": str(obj.get("intent", "无关")),
            "score": max(0, min(100, int(obj.get("score", 0)))),
            "ask_about": str(obj.get("ask_about", "")),
        }
    except Exception:
        return _rule_classify(text)


def _rule_classify(text):
    low = text.lower()
    if any(w in low for w in _CONSULT_WORDS):
        return {"intent": "咨询", "score": 60, "ask_about": text}
    return {"intent": "无关", "score": 0, "ask_about": ""}


def _sanitize_dm(text):
    """清理私信文案：去引号、敏感引流词、限长 50 字。"""
    out = (text or "").strip().strip('"').strip("'").strip()
    for bad in ["加微信", "加V", "加v", "vx", "微信", "二维码", "联系方式"]:
        out = out.replace(bad, "")
    return out[:50]


def classify_batch(texts, template=""):
    """批量判断一批评论（一次调用），返回与 texts 对齐的结果列表。

    每项 {"intent", "score", "ask_about", "dm"}，dm 在意图为咨询/购买时生成。
    template 为用户设置的统一话术模板，设置后 AI 按模板框架改写。
    """
    if not texts:
        return []
    if template and ("{" in template):
        dm_rule = (
            '若 intent 是咨询或购买，写一条针对该评论的个性化私信，'
            '【必须严格使用下面这个话术模板的句式与结构】，'
            '把对方评论里的具体信息填进大括号占位处'
            '（不超过50字、像人话、禁止出现 加微信/加V/vx/联系方式 等词、结尾引导对方回复）；'
            '否则空字符串。\n话术模板：%s' % template
        )
    elif template:
        dm_rule = (
            '若 intent 是咨询或购买，私信内容【必须原样照抄】下面这个话术，'
            '一个字都不要改、不要添加任何内容；否则空字符串。\n话术：%s' % template
        )
    else:
        dm_rule = (
            '若 intent 是咨询或购买，写一条针对该评论的个性化私信'
            '（不超过50字、像人话、禁止出现 加微信/加V/vx/联系方式 等词、结尾引导对方回复）；'
            '否则空字符串'
        )
    prompt = (
        "你是获客助手。下面有 %d 条抖音/小红书/快手评论区留言，编号 0~%d。\n"
        "对每条输出一个 JSON 对象，字段：\n"
        '{"idx": 编号, "intent": "咨询|购买|闲聊|负面|无关", '
        '"score": 0到100的购买意向分, '
        '"ask_about": "对方在问什么，没有就空字符串", '
        '"dm": "%s"}\n'
        "只输出一个 JSON 数组，不要任何解释。\n评论列表：\n"
        % (len(texts), len(texts) - 1, dm_rule)
    )
    for i, t in enumerate(texts):
        prompt += "%d. %s\n" % (i, t)
    raw = _chat(
        [
            {"role": "system", "content": "你只输出合法 JSON 数组。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=len(texts) * 160 + 200,
    )
    m = re.search(r"\[.*\]", raw, re.S)
    arr = json.loads(m.group(0))
    out = []
    for i in range(len(texts)):
        item = None
        for x in arr:
            if isinstance(x, dict) and str(x.get("idx", "")) == str(i):
                item = x
                break
        if not item and i < len(arr) and isinstance(arr[i], dict):
            item = arr[i]
        item = item or {}
        out.append({
            "intent": str(item.get("intent") or "无关"),
            "score": max(0, min(100, int(item.get("score") or 0))),
            "ask_about": str(item.get("ask_about") or ""),
            "dm": str(item.get("dm") or ""),
        })
    return out


def draft_dm(comment_text, ask_about, product_note="", template=""):
    """生成一条个性化私信（≤50 字、像人话、不带敏感引流词）。"""
    try:
        if template and ("{" in template):
            prompt = (
                "你是获客助手。有人在你同行的视频下评论了，你要给他发一条私信。\n"
                "要求：\n"
                "1) 严格使用下面这个话术模板的句式与结构，把对方评论里的具体信息填进去；\n"
                "2) 不超过50字，语气自然像真人；\n"
                "3) 禁止出现 加微信/加V/vx/联系方式/二维码 等词；\n"
                "4) 结尾引导对方回复。\n"
                "话术模板：%s\n"
                "对方评论：%s\n"
                "对方想问：%s\n" % (template, comment_text, ask_about)
            )
        elif template:
            # 固定话术：原样照抄
            return _sanitize_dm(template)
        else:
            prompt = (
                "你是获客助手。有人在你同行的视频下评论了，你要给他发一条私信。规则：\n"
                "1) 针对对方的评论和疑问，一句话，不超过50字；\n"
                "2) 语气自然像真人，不要销售腔；\n"
                "3) 禁止出现 加微信/加V/vx/联系方式/二维码 等词；\n"
                "4) 结尾引导对方回复你。\n"
                "对方评论：%s\n"
                "对方想问：%s\n" % (comment_text, ask_about)
            )
        if product_note:
            prompt += "你的业务补充：%s\n" % product_note
        prompt += "只输出私信正文，不要解释、不要引号。"
        out = _chat(
            [
                {"role": "system", "content": "你只输出私信正文。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=120,
        )
        out = out.strip().strip('"').strip("'").strip()
        return _sanitize_dm(out)
    except Exception:
        base = ask_about or comment_text[:20] or "这个"
        return "你好，看到你在问「%s」，方便的话回我一下，我把情况发你。" % base
