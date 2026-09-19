"""私信发送（半自动）：Playwright 驱动已登录账号，发一条私信。

- ensure_login(platform)：弹出有头浏览器让用户扫码登录。
- send_dm(platform, to_user, text)：打开对方主页 → 点私信 → 输入 → 发送。
- 登录态用「页面 DOM 是否还有登录按钮」判断（cookie 不可靠，游客也有）。
- 内置每日额度（DAILY_SEND_LIMIT）与固定间隔。
"""
import os
import re
import time

from . import config, db
from .browser import BrowserSession

LOGIN_WAIT_SECONDS = 180  # 扫码最长等待
DEBUG_DIR = os.path.join(config.BASE_DIR, "data", "debug")


class SendLimitExceeded(Exception):
    pass


def _count_key(platform):
    return "sent_count_%s_%s" % (platform, time.strftime("%Y-%m-%d"))


def today_sent_count(platform):
    try:
        return int(db.get_setting(_count_key(platform), "0"))
    except ValueError:
        return 0


def check_quota(platform):
    if today_sent_count(platform) >= config.DAILY_SEND_LIMIT:
        raise SendLimitExceeded(
            "今日发送已达上限（%s 条），明天再发，或调大 .env 里的 DAILY_SEND_LIMIT"
            % config.DAILY_SEND_LIMIT
        )


# ---------- 输入框查找与失败现场存档 ----------

def _norm_text(s):
    """比较用：去掉所有空白字符。"""
    import re
    return re.sub(r"\s+", "", str(s or ""))


def _profile_name(page):
    """从抖音个人页标题提取当前昵称（对方可能改过名，不能用库里旧名）。"""
    try:
        t = (page.title() or "").strip()
        for suffix in ("的抖音 - 抖音", "的个人主页 - 抖音", "的主页 - 抖音", " - 抖音"):
            if t.endswith(suffix):
                name = t[:-len(suffix)].strip()
                if name:
                    return name
    except Exception:
        pass
    return ""


def _find_im_editor(page):
    """在 IM 面板的聊天层里找消息输入框（抖音自研 editor-kit）。

    聊天层可能堆叠多个，返回最上层（最后一个）可见的编辑器。
    """
    found = None
    for frame in [page.main_frame] + [f for f in page.frames if f != page.main_frame]:
        for sel in (
            '[data-stack-layer="chat"] div[contenteditable="true"]',
            '#imSaasContainerId div[contenteditable="true"]',
            '[class*="messageEditor"] [contenteditable="true"]',
            '[class*="messageEditor"]',
            '[data-stack-layer="chat"] textarea',
        ):
            try:
                for e in frame.query_selector_all(sel):
                    if e.is_visible():
                        found = e  # 取最后一个可见的
            except Exception:
                continue
    return found


def _js_click(page, el):
    """JS 方式点击，绕过指针遮挡问题。"""
    try:
        page.evaluate("el => el.click()", el)
        return True
    except Exception:
        return False


def _find_input(page):
    """通用输入框查找（小红书用）：排除搜索框。"""
    for frame in [page.main_frame] + [f for f in page.frames if f != page.main_frame]:
        for sel in (
            'div[contenteditable="true"]',
            'div[role="textbox"]',
            'textarea',
        ):
            try:
                for e in frame.query_selector_all(sel):
                    if e.is_visible():
                        return e
            except Exception:
                continue
    return None


def _dump(page, tag):
    """失败时把页面截图/HTML/文字存到 data/debug，便于定位。"""
    os.makedirs(DEBUG_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        page.screenshot(path=os.path.join(DEBUG_DIR, "%s-%s.png" % (tag, stamp)))
    except Exception:
        pass
    try:
        with open(os.path.join(DEBUG_DIR, "%s-%s.html" % (tag, stamp)),
                  "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass
    try:
        with open(os.path.join(DEBUG_DIR, "%s-%s-text.txt" % (tag, stamp)),
                  "w", encoding="utf-8") as f:
            f.write(page.inner_text("body")[:20000])
    except Exception:
        pass
    return stamp


def _click_chat_entry(page, name):
    """在 IM 面板里点会话列表中名字完全匹配的条目（返回是否点到）。"""
    try:
        return bool(page.evaluate(
            """([name]) => {
                const root = document.querySelector('#imSaasContainerId');
                if (!root) return false;
                const cands = [...root.querySelectorAll('div,li,span')]
                    .filter(e => (e.textContent || '').trim() === name && e.offsetParent !== null);
                if (!cands.length) return false;
                cands[cands.length - 1].click();
                return true;
            }""",
            arg=[name],
        ))
    except Exception:
        return False


def _im_chat_text(page):
    """聊天面板所有层文字拼起来（名字可能在标题层/列表层/消息层任何一层）。"""
    try:
        layers = page.query_selector_all('[data-stack-layer="chat"]')
        return "\n".join(l.inner_text() for l in layers) if layers else ""
    except Exception:
        return ""


# ---------- 登录态判断（DOM 为准） ----------

def _visible_login_buttons(page):
    """页面上可见的「登录」按钮数量，>0 视为未登录。"""
    try:
        return page.evaluate(
            """() => {
                const nodes = [...document.querySelectorAll('button,div,span,a')];
                return nodes.filter(e => {
                    const t = (e.textContent || '').trim();
                    return (t === '登录' || t === '登录/注册') && e.offsetParent !== null;
                }).length;
            }"""
        )
    except Exception:
        return 1


def _douyin_logged_in(page):
    try:
        if "验证码" in (page.title() or "") or "安全验证" in (page.title() or ""):
            return False
    except Exception:
        pass
    return _visible_login_buttons(page) == 0


def _xhs_logged_in(page):
    try:
        if "website-login" in (page.url or ""):
            return False
        t = page.title() or ""
        if "安全限制" in t or "验证" in t:
            return False
    except Exception:
        pass
    return _visible_login_buttons(page) == 0


def _ks_visible_login_buttons(page):
    """快手页面上可见的含「登录」的元素数（>0 视为未登录）。"""
    try:
        return page.evaluate(
            """() => {
                const nodes = [...document.querySelectorAll('button,div,span,a')];
                return nodes.filter(e => {
                    const t = (e.textContent || '').trim();
                    return t.includes('登录') && t.length <= 12 && e.offsetParent !== null;
                }).length;
            }"""
        )
    except Exception:
        return 1


def _ks_page_ready(page):
    """快手页面是否真正加载（不是挑战 JSON 页）。"""
    try:
        body = page.inner_text("body") or ""
        if len(body.strip()) < 300 and '"result"' in body:
            return False
        return ("快手" in body) and len(body) > 100
    except Exception:
        return False


def _ks_login_cookie(context):
    """快手真正的登录 cookie：返回 (域名, 名字, 值) 或 None。

    只在 kuaishou.com 主域上认 passToken / 非空 userId，
    避免把扫码过程中的临时 cookie 误判成登录成功。
    """
    try:
        for c in context.cookies():
            name = c.get("name") or ""
            domain = c.get("domain") or ""
            value = (c.get("value") or "").strip()
            if not value:
                continue
            if name == "passToken" and "kuaishou.com" in domain:
                return (domain, name, value)
            if name == "userId" and domain in (".kuaishou.com", "www.kuaishou.com"):
                return (domain, name, value)
    except Exception:
        pass
    return None


def _ks_has_login_cookie(context):
    return _ks_login_cookie(context) is not None


def _ks_logged_in(page, context=None):
    if context is not None and _ks_has_login_cookie(context):
        return True
    try:
        body = page.inner_text("body") or ""
        if len(body.strip()) < 300 and '"result"' in body:
            return False  # 挑战页，未知状态，按未登录处理
    except Exception:
        pass
    try:
        if "验证" in (page.title() or ""):
            return False
    except Exception:
        pass
    return _ks_visible_login_buttons(page) == 0


def is_logged_in(platform):
    platform = platform.lower()
    session = BrowserSession(headless=False, role="sender")
    try:
        page = session.new_page()
        if platform == "douyin":
            try:
                page.goto("https://www.douyin.com/", timeout=30000,
                          wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
            except Exception:
                pass
            return _douyin_logged_in(page)
        if platform == "kuaishou":
            try:
                page.goto("https://www.kuaishou.com/", timeout=30000,
                          wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
            except Exception:
                pass
            return _ks_logged_in(page, context=session.context)
        try:
            page.goto("https://www.xiaohongshu.com/", timeout=30000,
                      wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
        except Exception:
            pass
        return _xhs_logged_in(page)
    finally:
        session.close()


def _shipinhao_logged_in(page):
    """视频号助手：登录后会自动跳转到 /platform 后台。"""
    try:
        url = page.url or ""
        if "channels.weixin.qq.com" in url and "/login" not in url:
            return True
        t = page.title() or ""
        if "视频号助手" in t and "登录" not in t and "扫码" not in t:
            return True
    except Exception:
        pass
    return False


def logout(platform):
    """退出某平台登录：访问登出接口 + 清掉该平台域名的 cookie。

    只清指定平台的 cookie，不影响同档案里其他平台的登录。
    """
    platform = platform.lower()
    session = BrowserSession(headless=True, role="sender")
    try:
        page = session.new_page()
        if platform == "douyin":
            try:
                page.goto(
                    "https://www.douyin.com/passport/web/logout/"
                    "?aid=6383&device_platform=webapp",
                    timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
            except Exception:
                pass
        elif platform == "kuaishou":
            try:
                page.goto("https://www.kuaishou.com/", timeout=30000,
                          wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
            except Exception:
                pass
        elif platform == "xiaohongshu":
            try:
                page.goto("https://www.xiaohongshu.com/", timeout=30000,
                          wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
            except Exception:
                pass
        # 兜底：清掉该平台域名的所有 cookie（不碰其他平台）
        try:
            for c in session.context.cookies():
                domain = c.get("domain") or ""
                if (platform == "douyin" and "douyin" in domain) or \
                   (platform == "kuaishou" and ("kuaishou" in domain or "kwai" in domain)) or \
                   (platform == "xiaohongshu" and "xiaohongshu" in domain):
                    session.context.clear_cookies(name=c.get("name"))
        except Exception:
            pass
        print("[logout] %s 登录已清除" % platform, flush=True)
    finally:
        session.close()


def ensure_login(platform, role="sender"):
    """弹出浏览器等用户扫码登录。返回是否登录成功。"""
    platform = platform.lower()
    print("[login] 正在启动浏览器窗口（%s / %s）..." % (platform, role), flush=True)
    session = BrowserSession(headless=False, role=role)
    try:
        page = session.new_page()

        # 微信视频号助手：官方网页后台，扫码后自动跳转
        if platform == "shipinhao":
            try:
                page.goto("https://channels.weixin.qq.com/login.html",
                          timeout=60000, wait_until="domcontentloaded")
            except Exception:
                pass
            page.bring_to_front()
            print("[login-sph] 等待微信扫码（最长 4 分钟）...", flush=True)
            for _ in range(240):
                if _shipinhao_logged_in(page):
                    print("[login-sph] 已进入视频号助手后台", flush=True)
                    return True
                page.wait_for_timeout(1000)
            return False
        if platform == "douyin":
            try:
                page.goto("https://www.douyin.com/", timeout=60000,
                          wait_until="domcontentloaded")
            except Exception:
                pass
            page.bring_to_front()
            page.wait_for_timeout(2500)
            if _douyin_logged_in(page):
                return True
            try:
                btn = page.query_selector('button:has-text("登录")')
                if btn:
                    btn.click()
                    page.wait_for_timeout(1500)
            except Exception:
                pass
            for _ in range(LOGIN_WAIT_SECONDS):
                if _douyin_logged_in(page):
                    return True
                page.wait_for_timeout(1000)
            return False

        # 快手
        if platform == "kuaishou":
            try:
                page.goto("https://www.kuaishou.com/", timeout=60000,
                          wait_until="domcontentloaded")
            except Exception:
                pass
            page.bring_to_front()
            # 等挑战页自动解决、真实页面出现（最多 60 秒）
            for _ in range(60):
                if _ks_page_ready(page):
                    break
                page.wait_for_timeout(1000)
            page.wait_for_timeout(2000)
            if _ks_has_login_cookie(session.context):
                return True
            for sel in ('text=立即登录', 'span:has-text("登录")', 'text=登录'):
                try:
                    btn = page.query_selector(sel)
                    if btn:
                        btn.click()
                        page.wait_for_timeout(2000)
                        break
                except Exception:
                    continue
            print("[login-ks] 二维码已弹，等待扫码...", flush=True)
            for i in range(LOGIN_WAIT_SECONDS):
                hit = _ks_login_cookie(session.context)
                if hit:
                    print("[login-ks] 检测到登录 cookie: %s %s" % (hit[0], hit[1]), flush=True)
                    # 关键：跳回首页用 DOM 验证真的已登录（登录按钮消失）
                    try:
                        page.goto("https://www.kuaishou.com/", timeout=30000,
                                  wait_until="domcontentloaded")
                        page.wait_for_timeout(4000)
                    except Exception:
                        pass
                    if _ks_visible_login_buttons(page) == 0:
                        print("[login-ks] DOM 确认已登录，完成", flush=True)
                        return True
                    print("[login-ks] DOM 仍显示未登录，继续等待...", flush=True)
                page.wait_for_timeout(1000)
            return False

        # 小红书
        try:
            page.goto("https://www.xiaohongshu.com/", timeout=60000,
                      wait_until="domcontentloaded")
        except Exception:
            pass
        page.bring_to_front()
        page.wait_for_timeout(2500)
        if _xhs_logged_in(page):
            return True
        try:
            btn = page.query_selector('text=登录')
            if btn:
                btn.click()
                page.wait_for_timeout(1500)
        except Exception:
            pass
        for _ in range(LOGIN_WAIT_SECONDS):
            if _xhs_logged_in(page):
                return True
            page.wait_for_timeout(1000)
        return False
    finally:
        session.close()


# ---------- 发送 ----------

def send_dm(platform, to_user, text, nickname=""):
    """发一条私信。to_user 为对方 sec_uid / user_id 或主页链接。"""
    platform = platform.lower()
    check_quota(platform)
    if not (text or "").strip():
        raise ValueError("私信内容为空")
    session = BrowserSession(headless=False, role="sender")
    try:
        page = session.new_page()
        if platform == "douyin":
            _goto_douyin_home(page)
            if not _douyin_logged_in(page):
                raise RuntimeError("抖音未登录：请先在后台点「登录抖音」扫码")
            _send_douyin(session, page, to_user, text, nickname)
        elif platform == "kuaishou":
            _goto_ks_home(page)
            if not _ks_logged_in(page, context=session.context):
                raise RuntimeError("快手未登录：请先在后台点「登录快手」扫码")
            _send_kuaishou(session, page, to_user, text)
        else:
            _goto_xhs_home(page)
            if not _xhs_logged_in(page):
                raise RuntimeError("小红书未登录：请先在后台点「登录小红书」扫码")
            _send_xhs(session, page, to_user, text)
        db.set_setting(_count_key(platform), str(today_sent_count(platform) + 1))
        time.sleep(1.5)
        return True
    finally:
        session.close()


def _goto_douyin_home(page):
    try:
        page.goto("https://www.douyin.com/", timeout=60000,
                  wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
    except Exception:
        pass


def _goto_ks_home(page):
    try:
        page.goto("https://www.kuaishou.com/", timeout=60000,
                  wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
    except Exception:
        pass


def _send_kuaishou(session, page, to_user, text):
    if re.match(r"^[0-9a-zA-Z_]{16,}$", str(to_user or "")):
        url = "https://www.kuaishou.com/profile/%s" % to_user
    else:
        url = to_user
    page.goto(url, timeout=60000, wait_until="domcontentloaded")
    page.wait_for_timeout(4500)
    btn = None
    for sel in ('text=私信', 'button:has-text("私信")', '[class*="sendMessage"]',
                '[class*="privateMsg"]'):
        btn = page.query_selector(sel)
        if btn:
            break
    if not btn:
        _dump(page, "ks-nobtn")
        raise RuntimeError("找不到「私信」按钮：对方可能关闭了私信，或页面没加载完")
    btn.click()
    editor = None
    for _ in range(30):
        page.wait_for_timeout(1000)
        editor = _find_input(page)
        if editor:
            break
    if not editor:
        _dump(page, "ks-noeditor")
        raise RuntimeError("聊天输入框没加载出来（已自动截图存档，等我修复）")
    editor.click()
    page.wait_for_timeout(300)
    try:
        editor.fill(text)
    except Exception:
        editor.type(text, delay=30)
    page.wait_for_timeout(500)
    page.keyboard.press("Enter")
    page.wait_for_timeout(3000)
    try:
        body_text = page.inner_text("body")
    except Exception:
        body_text = ""
    if text[:12] not in (body_text or ""):
        _dump(page, "ks-unconfirmed")
        raise RuntimeError("消息已输入并回车，但无法确认是否发出——请到弹出的浏览器窗口里检查后再决定是否重发")


def _goto_xhs_home(page):
    try:
        page.goto("https://www.xiaohongshu.com/", timeout=60000,
                  wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
    except Exception:
        pass


def _send_douyin(session, page, to_user, text, nickname=""):
    if to_user and str(to_user).startswith("MS4w"):
        url = "https://www.douyin.com/user/%s" % to_user
    else:
        url = to_user
    page.goto(url, timeout=60000, wait_until="domcontentloaded")
    page.wait_for_timeout(4500)
    # 对方可能改过昵称：从个人页标题读当前名字，兜底搜索/候选匹配/防点错核对全用它
    live_name = _profile_name(page) or nickname
    btn = None
    for sel in ('button:has-text("私信")', '[data-e2e="user-chat-btn"]', 'text=私信'):
        btn = page.query_selector(sel)
        if btn:
            break
    if not btn:
        # 私密账号：需要先关注才能私信 → 自动点关注再试
        follow = page.query_selector('button:text-is("关注")')
        if follow:
            try:
                follow.click()
            except Exception:
                _js_click(page, follow)
            print("[send] 检测到私密账号，已自动点关注", flush=True)
            page.wait_for_timeout(3500)
            for sel in ('button:has-text("私信")', '[data-e2e="user-chat-btn"]',
                        'text=私信'):
                btn = page.query_selector(sel)
                if btn:
                    break
    if not btn:
        _dump(page, "douyin-nobtn")
        raise RuntimeError("找不到「私信」按钮：对方可能关闭了私信，或页面没加载完")
    try:
        btn.click()
    except Exception:
        _js_click(page, btn)
    # 等聊天编辑器（有时直接进聊天，有时只停在会话列表）
    editor = None
    for _ in range(15):
        page.wait_for_timeout(1000)
        editor = _find_im_editor(page)
        if editor:
            break
    # 陌生人场景：面板可能出现「打招呼」入口（只能发一条），点它开启会话
    if not editor:
        hello = page.query_selector('text=打招呼')
        if hello:
            try:
                hello.click()
            except Exception:
                _js_click(page, hello)
            for _ in range(10):
                page.wait_for_timeout(1000)
                editor = _find_im_editor(page)
                if editor:
                    break
    # 兜底：面板停在会话列表 → 搜索对方昵称，点「发消息」开起会话
    if not editor and live_name:
        search = page.query_selector(
            '#imSaasContainerId input[placeholder="搜索"]')
        if search:
            try:
                search.click()
                search.fill(live_name)
                page.wait_for_timeout(4000)
            except Exception:
                pass
            # 搜索结果里的用户卡片带「发消息」按钮，优先点它
            for _ in range(2):
                msg_btn = page.query_selector('text=发消息')
                if msg_btn:
                    try:
                        msg_btn.click()
                    except Exception:
                        _js_click(page, msg_btn)
                    page.wait_for_timeout(3000)
                    editor = _find_im_editor(page)
                    if editor:
                        break
                page.wait_for_timeout(2000)
            # 再不行：逐个尝试点昵称匹配的候选
            if not editor:
                for attempt in range(4):
                    clicked = page.evaluate(
                        """([nick, attempt]) => {
                            const root = document.querySelector('#imSaasContainerId');
                            if (!root) return false;
                            const cands = [...root.querySelectorAll('div,li,span')]
                                .filter(e => (e.textContent || '').trim().includes(nick) &&
                                             e.offsetParent !== null);
                            if (!cands.length) return false;
                            let target = cands.find(e => (e.textContent || '').trim() === nick);
                            if (!target) target = cands[cands.length - 1 - attempt];
                            if (!target) return false;
                            target.click();
                            return true;
                        }""",
                        arg=[live_name, attempt],
                    )
                    if not clicked:
                        break
                    page.wait_for_timeout(3000)
                    editor = _find_im_editor(page)
                    if editor:
                        # 核对打开的会话是对方本人
                        chat_text = ""
                        try:
                            layers = page.query_selector_all(
                                '[data-stack-layer="chat"]')
                            if layers:
                                # 名字可能在任何一层（标题层/列表层/消息层），拼起来看
                                chat_text = "\n".join(
                                    l.inner_text() for l in layers)
                        except Exception:
                            pass
                        if (chat_text and _norm_text(live_name)[:6] in _norm_text(chat_text)) or not chat_text:
                            break
                        editor = None  # 点错了，试下一个候选
    if not editor:
        _dump(page, "douyin-noeditor")
        # 面板打开过但始终没有聊天输入框：大概率对方关闭了陌生人私信（平台限制，不是程序 bug）
        panel_open = False
        try:
            panel_open = bool(page.query_selector('#imSaasContainerId'))
        except Exception:
            pass
        if panel_open:
            raise RuntimeError("对方可能关闭了陌生人私信：聊天窗口始终没打开（已截图存档）")
        raise RuntimeError("聊天输入框没加载出来（已自动截图存档，等我修复）")
    # 最终核对（防点错人）——用当前昵称比对（对方改名也能对上）
    if live_name:
        chat_text = _im_chat_text(page)
        if chat_text and _norm_text(live_name)[:6] not in _norm_text(chat_text):
            # 面板可能停在上一个会话：试着点列表里对方本人的会话条目，点开后再核对
            fixed = False
            for _ in range(2):
                if not _click_chat_entry(page, live_name):
                    break
                page.wait_for_timeout(2500)
                editor = _find_im_editor(page)
                if not editor:
                    break
                chat_text = _im_chat_text(page)
                if not chat_text or _norm_text(live_name)[:6] in _norm_text(chat_text):
                    fixed = True
                    break
            if not fixed:
                _dump(page, "douyin-wrong-chat")
                raise RuntimeError("私信窗口打开的是别人的会话，且列表里找不到对方本人（对方可能关闭了陌生人私信），已中止")
    try:
        editor.click()
    except Exception:
        _js_click(page, editor)
    page.wait_for_timeout(300)
    try:
        editor.fill(text)
    except Exception:
        editor.type(text, delay=30)
    page.wait_for_timeout(500)
    page.keyboard.press("Enter")
    page.wait_for_timeout(3000)
    # 校验消息真的出现在对话里
    try:
        chat_text = page.inner_text('[data-stack-layer="chat"]')
    except Exception:
        chat_text = ""
    if text[:12] not in (chat_text or ""):
        _dump(page, "douyin-unconfirmed")
        raise RuntimeError("消息已输入并回车，但无法确认是否发出——请到弹出的浏览器窗口里检查后再决定是否重发")


def _send_xhs(session, page, to_user, text):
    if re.match(r"^[0-9a-fA-F]{20,}$", str(to_user or "")):
        url = "https://www.xiaohongshu.com/user/profile/%s" % to_user
    else:
        url = to_user
    page.goto(url, timeout=60000, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    btn = None
    for sel in ('.xhs-user-im-btn', '[class*="user-im-btn"]',
                'text=发私信', 'text=私信', 'button:has-text("发私信")'):
        btn = page.query_selector(sel)
        if btn:
            break
    if not btn:
        _dump(page, "xhs-nobtn")
        raise RuntimeError("找不到「私信」按钮：对方可能关闭了私信")
    btn.click()
    page.wait_for_timeout(3000)
    # 聊天窗口可能开在新标签页
    target = page
    pages = session.context.pages
    if len(pages) > 1:
        target = pages[-1]
        target.wait_for_timeout(2000)
    box = None
    for _ in range(12):
        target.wait_for_timeout(1000)
        box = _find_input(target)
        if box:
            break
    if not box:
        _dump(target, "xhs-noinput")
        raise RuntimeError("找不到聊天输入框（已自动截图存档，等我修复选择器）")
    box.click()
    try:
        box.fill(text)
    except Exception:
        box.type(text)
    target.wait_for_timeout(500)
    target.keyboard.press("Enter")
    target.wait_for_timeout(2500)
