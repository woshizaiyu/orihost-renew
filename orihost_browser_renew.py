#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Orihost 浏览器自动续期（SeleniumBase + 真浏览器）
# 背景：面板 claim 接口强制要求 Cloudflare Turnstile token（GET /api/client/renewal/complete?cf-turnstile-response=xxx），
#       纯 HTTP 调不通（无 token 直接 500），必须用真浏览器点验证。
# 流程：Cookie 免登 → 服务器页 → Renew Now → Read Article（新标签读文章，
#       面板倒计时，文章页保持打开）→ Thanks-for-reading → 点 Turnstile 复选框
#       → Token 出来后点 Claim Renewal → 页面文案 + API 双重确认
# 证据：.tmp/orihost-20261005-121149 录制截图（S000009/S000015/S000016/S000020）
# 参考：katabump-renew-main（同款 Turnstile 处理 + xvfb 无头方案）

import os
import sys
import time
import random
import requests as tg_lib
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote
from seleniumbase import SB

PANEL = "https://panel.orihost.com"
# Laravel 默认 remember cookie 名（yanyumm1 实测 Orihost 可用）
DEFAULT_REMEMBER_NAME = "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d"
# 文章页停留秒数（面板 dwell=15，多留 buffer；“过早关闭文章页会被警告”）
ARTICLE_WAIT = int(os.environ.get("ARTICLE_WAIT") or "30")
# Claim 按钮轮询上限
CLAIM_TIMEOUT = int(os.environ.get("CLAIM_TIMEOUT") or "150")

# ---------- 代理 ----------
# 优先级：ORIHOST_PROXY 显式指定 > 工作流 sing-box（IS_PROXY/PROXY_SERVER，由 NODE_LINK 转出）
def _get_proxy():
    explicit = (os.environ.get("ORIHOST_PROXY") or os.environ.get("ORIHOST_GOST_PROXY") or "").strip()
    if explicit:
        scheme = explicit.split("://", 1)[0].lower() if "://" in explicit else ""
        if scheme in ("http", "https", "socks4", "socks5", "socks5h"):
            return explicit
        print(f"  ⚠️ ORIHOST_PROXY 格式不支持 ({scheme}://)，节点链接请填 NODE_LINK")
    if os.environ.get("IS_PROXY", "").lower() == "true":
        srv = (os.environ.get("PROXY_SERVER") or "socks5://127.0.0.1:1080").strip()
        print(f"  🔗 使用 sing-box 代理: {srv}")
        return srv
    return ""

PROXY_STR = _get_proxy()
IS_PROXY = bool(PROXY_STR)

# ---------- Telegram ----------
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""
TG_CHAT_ID = os.environ.get("TG_CHAT_ID") or ""
if (not TG_BOT_TOKEN or not TG_CHAT_ID) and os.environ.get("TG_BOT"):
    try:
        _cid, _tok = os.environ["TG_BOT"].split(",", 1)
        TG_CHAT_ID = TG_CHAT_ID or _cid.strip()
        TG_BOT_TOKEN = TG_BOT_TOKEN or _tok.strip()
    except Exception:
        pass


ORIHOST_EMAIL = os.environ.get("ORIHOST_EMAIL") or ""


def mask_email(email: str) -> str:
    if "@" in email:
        name, domain = email.split("@", 1)
        if len(name) > 4:
            return f"{name[:2]}****{name[-2:]}@{domain}"
        return f"{name}@{domain}"
    return email[:2] + "****"


def send_tg(msg: str, screenshot_path: str = ""):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        if screenshot_path and os.path.exists(screenshot_path):
            with open(screenshot_path, "rb") as f:
                img_data = f.read()
            boundary = f"----Boundary{abs(hash(msg))}"
            body_parts = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
                f"{TG_CHAT_ID}\r\n"
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="caption"\r\n\r\n'
                f"{msg}\r\n"
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="photo"; filename="s.png"\r\n'
                f"Content-Type: image/png\r\n\r\n"
            ).encode() + img_data + f"\r\n--{boundary}--\r\n".encode()
            tg_lib.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
                data=body_parts,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                timeout=30,
            )
        else:
            tg_lib.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                timeout=15,
            )
    except Exception as e:
        print(f"  TG 发送失败: {e}")


def now_bj():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


# ---------- cron 自我调度（参考 oyz/FreezeHost） ----------
def updateCronSchedule(after_expires_at: str):
    """续期成功后按 expires_at-1天 改写 renew.yml 的 cron 为一次性定时并 push"""
    import subprocess

    if os.environ.get("DRY_RUN", "").lower() == "true":
        print("  ℹ️ DRY_RUN 演练，跳过 cron 回写")
        return False
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        print("  ℹ️ 非 CI 环境，跳过 cron 回写")
        return False

    gh_token = os.environ.get("GH_TOKEN", "")
    if not gh_token:
        print("  ℹ️ 未提供 GH_TOKEN，跳过 cron 回写")
        return False

    try:
        from datetime import datetime as dt
        t = dt.fromisoformat(after_expires_at.replace("Z", "+00:00"))
        next_run = t - timedelta(days=1)
        if next_run.timestamp() <= datetime.now(timezone.utc).timestamp():
            next_run = datetime.now(timezone.utc) + timedelta(hours=12)

        p2 = lambda n: str(n).zfill(2)
        new_cron = f"10 10 {next_run.day} {next_run.month} *"
        next_str = f"{next_run.year}-{p2(next_run.month)}-{p2(next_run.day)} {p2(next_run.hour)}:{p2(next_run.minute)}"

        wf = os.path.join(os.getcwd(), ".github", "workflows", "renew-browser.yml")
        if not os.path.exists(wf):
            wf = os.path.join(os.getcwd(), ".github", "workflows", "renew.yml")
        if not os.path.exists(wf):
            print("  ⚠️ 未找到 workflow 文件，跳过 cron 回写")
            return False

        with open(wf, "r", encoding="utf-8") as f:
            old = f.read()
        import re
        m = re.search(r"^(\s*- cron: )'[^']*'(.*)$", old, re.M)
        if not m:
            print("  ⚠️ renew.yml 无 cron 行，跳过 cron 回写")
            return False

        updated = old.replace(m.group(0), f"{m.group(1)}'{new_cron}'  # auto: 下一次 {next_str} UTC")
        with open(wf, "w", encoding="utf-8") as f:
            f.write(updated)

        env = os.environ.copy()
        env["GIT_ASKPASS"] = "echo"
        env["GIT_USERNAME"] = "github-actions[bot]"
        env["GIT_PASSWORD"] = gh_token

        subprocess.run(["git", "pull", "--rebase"], env=env, capture_output=True, timeout=30)
        subprocess.run(["git", "add", wf], env=env, capture_output=True, timeout=10)
        subprocess.run(
            ["git", "commit", "-m", "自动调整下次续期时间", "-m", f"下次运行: {next_str} UTC"],
            env=env, capture_output=True, timeout=10,
        )
        subprocess.run(["git", "push"], env=env, capture_output=True, timeout=30)
        print(f"  ✅ cron 已回写: {new_cron}（下一次 {next_str} UTC）")
        return True
    except Exception as e:
        print(f"  ⚠️ cron 回写失败: {e}")
        return False


# ---------- 账号解析（与 orihost_renew.py 同一套变量名） ----------
def _split_ids(raw: str):
    return [s.strip() for s in (raw or "").replace(";", ",").split(",") if s.strip()]


def parse_auth_cookies(auth_raw: str):
    """把用户填的 token 还原成 [(name, value)]，支持裸 token / name=value / 完整 Cookie 串"""
    v = (auth_raw or "").strip()
    if "remember_web" in v and (";" in v or "XSRF-TOKEN" in v or "jexactyl_session" in v):
        out = []
        for item in v.split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            k, val = item.split("=", 1)
            k, val = k.strip(), val.strip()
            if not k or k.lower() in ("path", "expires", "domain", "max-age", "samesite", "secure", "httponly"):
                continue
            try:
                val = unquote(val)
            except Exception:
                pass
            out.append((k, val))
        return out
    if "=" in v and "remember_web" in v:
        name, val = v.split("=", 1)
        return [(name.strip(), val.strip())]
    return [(DEFAULT_REMEMBER_NAME, v)]


def load_accounts():
    accounts = []
    for i in range(1, 20):
        token_raw = (os.environ.get(f"ORIHOST_REMEMBER_{i}") or os.environ.get(f"ORIHOST_COOKIE_{i}") or "").strip()
        ids = _split_ids(os.environ.get(f"ORIHOST_SERVER_IDS_{i}") or "")
        if not token_raw and not ids:
            continue
        if not token_raw or not ids:
            print(f"⚠️ 账号{i} 配置不完整，跳过")
            continue
        accounts.append({"label": f"账号{i}", "auth": token_raw, "servers": ids})
    if not accounts:
        single_auth = (os.environ.get("ORIHOST_REMEMBER") or os.environ.get("ORI_COOKIE") or os.environ.get("ORIHOST_COOKIE") or "").strip()
        single_ids = _split_ids(os.environ.get("ORIHOST_SERVER_IDS") or os.environ.get("ORIHOST_SERVER_IDS_1") or "")
        if single_auth and single_ids:
            accounts.append({"label": "默认账号", "auth": single_auth, "servers": single_ids})
    return accounts


# ---------- Turnstile 处理（移植自 katabump，经实测有效） ----------
_EXPAND_JS = """
(function() {
    var ts = document.querySelector('input[name="cf-turnstile-response"]');
    if (!ts) return 'no-turnstile';
    var el = ts;
    for (var i = 0; i < 20; i++) {
        el = el.parentElement;
        if (!el) break;
        var s = window.getComputedStyle(el);
        if (s.overflow === 'hidden' || s.overflowX === 'hidden' || s.overflowY === 'hidden')
            el.style.overflow = 'visible';
        el.style.minWidth = 'max-content';
    }
    document.querySelectorAll('iframe').forEach(function(f){
        if (f.src && f.src.includes('challenges.cloudflare.com')) {
            f.style.width = '300px'; f.style.height = '65px';
            f.style.minWidth = '300px';
            f.style.visibility = 'visible'; f.style.opacity = '1';
        }
    });
    return 'done';
})()
"""

_SOLVED_JS = """
(function(){
    var i = document.querySelector('input[name="cf-turnstile-response"]');
    return !!(i && i.value && i.value.length > 20);
})()
"""

_HAS_TURNSTILE_JS = """
(function(){
    if (document.querySelector('input[name="cf-turnstile-response"]')) return true;
    var fs = document.querySelectorAll('iframe');
    for (var i = 0; i < fs.length; i++) {
        if (fs[i].src && fs[i].src.includes('challenges.cloudflare.com')) return true;
    }
    return false;
})()
"""


def click_turnstile_checkbox(sb) -> bool:
    """真鼠标点 Turnstile 复选框（截图实证：弹窗内是交互式 checkbox，
    不点它 token 永远出不来，之前 handle_turnstile 只等不点就是卡死根因）。
    widget 宽 300 高 65，复选框在左侧 → 点 iframe 中心偏左。"""
    try:
        from selenium.webdriver.common.action_chains import ActionChains
    except Exception as e:
        print(f"  ⚠️ ActionChains 不可用: {e}")
        return False
    try:
        frames = sb.find_elements('iframe[src*="challenges.cloudflare.com"]')
    except Exception:
        frames = []
    for fr in frames:
        try:
            if not fr.is_displayed():
                continue
            sb.execute_script("arguments[0].scrollIntoView({block:'center'});", fr)
            time.sleep(0.8)
            ActionChains(sb.driver).move_to_element_with_offset(fr, -110, 0).click().perform()
            print("  🖱️ 已点 Turnstile 复选框")
            return True
        except Exception:
            continue
    # 兜底：直接点 .cf-turnstile 容器中心
    try:
        box = sb.find_element(".cf-turnstile")
        sb.execute_script("arguments[0].scrollIntoView({block:'center'});", box)
        time.sleep(0.8)
        ActionChains(sb.driver).move_to_element(box).click().perform()
        print("  🖱️ 已点 Turnstile 容器")
        return True
    except Exception as e:
        print(f"  ⚠️ 点复选框失败: {str(e)[:100]}")
        return False


def handle_turnstile(sb) -> bool:
    print("🔍 处理 Cloudflare Turnstile 验证...")
    time.sleep(2)
    try:
        if sb.execute_script(_SOLVED_JS):
            print("✅ 已静默通过")
            return True
    except Exception:
        pass
    try:
        sb.execute_script(_EXPAND_JS)
    except Exception:
        pass
    for attempt in range(3):
        click_turnstile_checkbox(sb)
        for _ in range(40):  # 点完等 token，最长 ~40s（含转圈验证时间）
            time.sleep(1)
            try:
                if sb.execute_script(_SOLVED_JS):
                    print(f"✅ Turnstile 通过（第 {attempt + 1} 次点击）")
                    return True
            except Exception:
                pass
        print(f"⚠️ 第 {attempt + 1} 次点击未出 token，重试...")
    # 最后兜底：uc_gui_click_captcha（katabump 同款）
    print("🖱️ 兜底调用 uc_gui_click_captcha...")
    try:
        sb.uc_gui_click_captcha()
    except Exception as e:
        print(f"⚠️ uc_gui_click_captcha 调用异常: {e}")
    for _ in range(20):
        time.sleep(1)
        try:
            if sb.execute_script(_SOLVED_JS):
                print("✅ Turnstile 通过（uc 兜底）")
                return True
        except Exception:
            pass
    print("  ❌ Turnstile 未通过")
    return False


# ---------- 广告拦截（CI 环境广告 iframe 可能遮挡按钮） ----------
_REMOVE_ADS_JS = """
(function() {
    // 移除广告 iframe：关键词命中，或跨域且不在白名单（面板自身/验证/谷歌除外）。
    // 广告域名天天换，关键词列不完；面板正常只需要同源 + Turnstile iframe。
    var allow = ['challenges.cloudflare.com', 'panel.orihost.com', 'orihost.com',
                 'www.google.com', 'www.gstatic.com', 'recaptcha'];
    document.querySelectorAll('iframe').forEach(function(f) {
        var src = f.src || '';
        if (!src) return;
        if (src.includes('challenges.cloudflare.com')) return;  // 验证框，绝不能动
        var bad = /n6wxm|nap5k|5gvci|jhnwr|rtmark|vignette|tag\.min\.js|\bad\b|advert|popup|overlay|modal/i.test(src);
        var sameOrigin = src.indexOf(location.origin) === 0 || src.charAt(0) === '/';
        var allowed = allow.some(function(a){ return src.includes(a); });
        if (bad || (!sameOrigin && !allowed)) {
            f.remove();
        }
    });
    // 移除固定定位的广告覆盖层（续期弹窗除外：它含 Turnstile iframe，
    // 不能按 "innerHTML 含 iframe" 误删；认准 Renew/Claim/Thanks 字样就跳过）
    document.querySelectorAll('div').forEach(function(d) {
        var s = window.getComputedStyle(d);
        if ((s.position === 'fixed' || s.position === 'absolute') && 
            s.zIndex > 100 && d.offsetHeight > 100) {
            var text = d.innerText || '';
            if (text.includes('Renew your server') || text.includes('Claim Renewal') ||
                text.includes('Thanks for reading') || text.includes('Read Article')) {
                return;
            }
            if (text.includes('ad') || text.includes('广告') || 
                text.includes('close') || text.includes('关闭') ||
                text.includes('✕') || text.includes('×') ||
                d.innerHTML.includes('iframe')) {
                d.remove();
            }
        }
    });
    return 'done';
})()
"""


def remove_ads(sb):
    """移除广告 iframe 和覆盖层，点击关闭按钮"""
    try:
        sb.execute_script(_REMOVE_ADS_JS)
    except Exception:
        pass
    # 点击广告关闭按钮（"要關閉" / "关闭" / "Close"）
    for el in sb.find_elements("span") + sb.find_elements("button") + sb.find_elements("a"):
        try:
            txt = (el.text or "").strip()
            if txt in ("要關閉", "关闭", "Close", "✕", "×"):
                el.click()
                print("  🚫 关闭广告弹窗")
                time.sleep(1)
                break
        except Exception:
            continue


# ---------- 页面工具（文本匹配按钮，面板是 React，文本最稳） ----------
def find_button_by_text(sb, *keywords, timeout=10, tags=("button", "a", "div")):
    """在指定标签里找文本包含关键词的第一个可见元素。
    血泪教训：找 Claim/Read 这类词必须只查 button/a——弹窗描述文案 div 里
    就有 "to claim your renewal" / "Click Read Article"，查 div 必误匹配，
    导致没流转到 Thanks-for-reading 就往下走、干等验证组件。"""
    end = time.time() + timeout
    kws = [k.lower() for k in keywords]
    while time.time() < end:
        try:
            els = []
            for tag in tags:
                try:
                    els += sb.find_elements(tag)
                except Exception:
                    pass
            for el in els:
                try:
                    if not el.is_displayed():
                        continue
                    txt = (el.text or "").strip().lower()
                    if txt and any(k in txt for k in kws):
                        return el
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(1)
    return None


def page_text(sb) -> str:
    """只读可见文本（innerText），不读 page_source。
    血泪教训：page_source 含 JS 包，'extended' 等词常驻会导致误判成功。"""
    try:
        t = sb.execute_script("return (document.body && document.body.innerText) || '';")
        if t:
            return str(t).lower()
    except Exception:
        pass
    try:
        return (sb.get_page_source() or "").lower()
    except Exception:
        return ""


_RENEWAL_MODAL_JS = """
(function(){
  // 续期弹窗 = 同时含两处文案的最小 div（body 也含这些词，必须取最小的）
  var best = null;
  document.querySelectorAll('div').forEach(function(el){
    var t = el.innerText || '';
    if (t.includes('Renew your server') && t.includes('Claim Renewal')) {
      if (!best || t.length < (best.innerText || '').length) best = el;
    }
  });
  return best;
})()
"""


def dismiss_overlays(sb):
    """关 cookie 横幅（Got it）与广告弹窗（Close/×），绝不碰续期弹窗本身。
    截图实证：'Download is ready / Tap to proceed' 广告（带 Ad 角标，常驻中央盖住
    验证框）+ 底部 'We use cookies ... Got it' 横幅。
    血泪教训：旧版用 xpath ancestor::* 判“弹窗内则跳过”——body 也是祖先且全页
    文案都含 Claim Renewal，结果全页面的 Close/Got it 一个都没点过。
    新版：先定位续期弹窗最小 div，只跳过它内部的按钮；广告常在跨域 iframe 里，
    top-document 够不着，所以每个非验证 iframe 里再扫一遍（iframe 里不可能有
    续期弹窗，× 也可以点）。"""
    try:
        modal = sb.execute_script(_RENEWAL_MODAL_JS)
    except Exception:
        modal = None

    def inside_modal(el) -> bool:
        if modal is None:
            return False
        try:
            return bool(sb.execute_script(
                "return arguments[0] === arguments[1] || arguments[0].contains(arguments[1]);",
                modal, el))
        except Exception:
            return False

    def sweep(allow_x: bool):
        try:
            btns = sb.find_elements("button")
        except Exception:
            return
        for el in btns:
            try:
                if not el.is_displayed():
                    continue
                txt = (el.text or "").strip()
                if txt not in ("Close", "Got it", "关闭", "知道了") and not (allow_x and txt in ("×", "✕", "x", "X")):
                    continue
                if not allow_x and inside_modal(el):
                    continue  # 续期弹窗内部，不碰
                el.click()
                print(f"  🚫 关闭遮挡弹窗: {txt or '×'}")
                time.sleep(1)
            except Exception:
                continue

    sweep(allow_x=False)  # 主文档：不动 ×（续期弹窗自己的 × 就是 ×）
    # 广告 iframe 里再扫（Turnstile 的 challenges  iframe 除外）
    try:
        frames = sb.execute_script(
            "return Array.from(document.querySelectorAll('iframe'))"
            ".filter(f => f.src && !f.src.includes('challenges.cloudflare.com'));")
    except Exception:
        frames = []
    for fr in frames or []:
        try:
            sb.driver.switch_to.frame(fr)
            sweep(allow_x=True)
        except Exception:
            pass
        finally:
            try:
                sb.driver.switch_to.default_content()
            except Exception:
                pass


def try_passthrough_shortlink(sb):
    """cuty.io/cuttty 系短链穿透（best-effort）：点 Continue/Proceed 走到正文。
    直链文章页（albeu.com 等）无按钮则直接返回。调用前需已切到文章标签。"""
    try:
        url = (sb.get_current_url() or "").lower()
    except Exception:
        return
    if not any(d in url for d in ("cuty.io", "cuttty", "cutt", "short", "linkvertise", "ouo.io")):
        return
    print(f"  🔗 短链页，尝试穿透: {url[:60]}")
    for _ in range(3):
        btn = find_button_by_text(sb, "continue", "proceed", "click here to continue",
                                  "继续", "前往", "下一步", "get link", timeout=8)
        if btn is None:
            break
        try:
            btn.click()
        except Exception:
            try:
                sb.execute_script("arguments[0].click();", btn)
            except Exception:
                break
        time.sleep(4)


# ---------- Cookie 免登 ----------
def cookie_login(sb, auth_raw: str) -> bool:
    print("🍪 Cookie 免登...")
    sb.open(PANEL + "/")
    time.sleep(3)
    try:
        sb.delete_all_cookies()
    except Exception:
        pass
    for name, val in parse_auth_cookies(auth_raw):
        try:
            sb.driver.add_cookie({"name": name, "value": val, "domain": "panel.orihost.com", "path": "/"})
        except Exception as e:
            print(f"  ⚠️ cookie 写入失败 {name}: {e}")
    sb.open(PANEL + "/dashboard")
    time.sleep(6)
    src = page_text(sb)
    if "login" in (sb.get_current_url() or "").lower() and ("sign in" in src or "password" in src and "dashboard" not in src):
        print("  ❌ Cookie 登录失败（仍在登录页），remember 可能失效")
        return False
    print("  ✅ 已登录")
    return True


# ---------- API 预检（合并自 orihost_renew.py） ----------
def _get_session(auth_raw: str):
    """用 remember token 置换 session + XSRF，返回 (session, xsrf)"""
    import requests as req_lib

    kind = "raw"
    v = (auth_raw or "").strip()
    if "remember_web" in v and (";" in v or "XSRF-TOKEN" in v or "jexactyl_session" in v):
        kind = "full"
    elif "=" in v and "remember_web" in v:
        kind = "pair"

    s = req_lib.Session()
    if PROXY_STR:
        s.proxies.update({"http": PROXY_STR, "https": PROXY_STR})

    if kind == "full":
        for item in v.split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            k, val = item.split("=", 1)
            k, val = k.strip(), val.strip()
            if not k or k.lower() in ("path", "expires", "domain", "max-age", "samesite", "secure", "httponly"):
                continue
            try:
                val = unquote(val)
            except Exception:
                pass
            s.cookies.set(k, val, domain="panel.orihost.com")
    elif kind == "pair":
        name, val = v.split("=", 1)
        s.cookies.set(name.strip(), val.strip(), domain="panel.orihost.com")
    else:
        s.cookies.set(DEFAULT_REMEMBER_NAME, v, domain="panel.orihost.com")

    for _ in range(5):
        try:
            s.get(f"{PANEL}/dashboard", timeout=20)
            xsrf = s.cookies.get("XSRF-TOKEN")
            if xsrf:
                return s, unquote(xsrf)
            time.sleep(2)
        except Exception:
            time.sleep(2)
    return None, None


def api_get_info(s, xsrf: str, server_uuid: str):
    """查续期次数与到期天数，返回 (renewal, days, expires_at)"""
    h = {
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{PANEL}/server/{short_id(server_uuid)}",
    }
    if xsrf:
        h["X-XSRF-TOKEN"] = xsrf
    for ident in (server_uuid, short_id(server_uuid)):
        try:
            r = s.get(f"{PANEL}/api/client/servers/{ident}", headers=h, timeout=20)
            if not r.ok:
                continue
            attrs = r.json().get("attributes", {})
            renewal = int(attrs.get("renewal", 0) or 0)
            exp = attrs.get("expires_at", "")
            days = None
            if exp:
                try:
                    dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                    days = round((dt - datetime.now(timezone.utc)).total_seconds() / 86400, 1)
                except Exception:
                    pass
            return renewal, days, exp
        except Exception:
            continue
    return None, None, ""


def api_check_cooldown(s, xsrf: str, server_uuid: str) -> int:
    """查冷却秒数"""
    h = {
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }
    if xsrf:
        h["X-XSRF-TOKEN"] = xsrf
    try:
        r = s.get(f"{PANEL}/api/client/servers/{server_uuid}/renew/cooldown", headers=h, timeout=15)
        if r.ok:
            return int(r.json().get("seconds", 0) or 0)
    except Exception:
        pass
    return 0


def short_id(uuid: str) -> str:
    return (uuid or "").strip().split("-")[0][:8]


# ---------- 单台续期 ----------
def renew_one_server(sb, server_uuid: str, precheck=None) -> dict:
    sid = short_id(server_uuid)
    print(f"\n  🖥 [{sid}] 打开服务器页...")

    # API 预检：满额/冷却直接跳过，不开浏览器
    if precheck:
        renewal, days, expires_at = precheck.get("info", (None, None, ""))
        cooldown = precheck.get("cooldown", 0)
        if renewal is not None and renewal >= 21:
            return {"status": "⏭️ 跳过", "message": f"续期 {renewal}/21 已满", "expires_at": expires_at}
        if cooldown > 300:
            return {"status": "⏭️ 跳过", "message": f"冷却中 {cooldown}s，本轮跳过", "expires_at": expires_at}
        if days is not None:
            print(f"  📅 预检: 续期 {renewal} / 剩余 {days} 天")

    # 面板路由用的是 8 位短 ID（如 /server/8651e616），填了完整 UUID 也只取前 8 位
    sb.open(f"{PANEL}/server/{sid}")
    time.sleep(8)
    try:
        sb.wait_for_ready_state_complete(timeout=20)
    except Exception:
        pass
    time.sleep(3)

    src = page_text(sb)
    if "renew limit reached" in src:
        return {"status": "⏭️ 跳过", "message": "已达续期上限（Renew Limit Reached）"}
    if "expired renewal" in src or "suspended due" in src:
        print("  ⚠️ 服务器因过期被暂停，走续期流程恢复")

    # 1. 点 Renew Now（只查 button/a；兼容 Google 翻译后的中文文案）
    print("  🔍 找 Renew Now 按钮...")
    remove_ads(sb)
    time.sleep(1)
    renew_btn = find_button_by_text(sb, "renew now", "renew", "更新", "续期", timeout=20, tags=("button", "a"))
    if renew_btn is None:
        sb.save_screenshot(f"no_renew_btn_{sid}.png")
        try:
            src = sb.get_page_source() or ""
            print(f"  🔍 页面源码前500字符: {src[:500]}")
        except Exception:
            pass
        return {"status": "❌ 续期失败", "message": "没找到 Renew Now 按钮（页面结构可能变了）"}
    try:
        renew_btn.click()
    except Exception:
        sb.execute_script("arguments[0].click();", renew_btn)
    time.sleep(4)

    # 2. 点 Read Article（会弹新标签，只查 button/a；兼容翻译后的中文文案）
    print("  🖱️ 点 Read Article...")
    article_handle = None
    panel_handle = None
    read_btn = find_button_by_text(sb, "read article", "read", "阅读文章", "阅读", timeout=15, tags=("button", "a"))
    if read_btn is None:
        # 可能已经在 reading 状态（倒计时中），直接往下走
        print("  ℹ️ 没找到 Read Article，可能已在倒计时，直接等待")
    else:
        before = set(sb.driver.window_handles)
        try:
            read_btn.click()
        except Exception:
            sb.execute_script("arguments[0].click();", read_btn)
        # 等新标签出现
        article_handle = None
        for _ in range(10):
            time.sleep(1)
            after = set(sb.driver.window_handles)
            new = after - before
            if new:
                article_handle = list(new)[0]
                break
        if article_handle is None:
            return {"status": "❌ 续期失败", "message": "文章页没弹出来（弹窗被拦，请加 --disable-popup-blocking）"}
        print(f"  📰 文章页已打开，切回面板等倒计时 {ARTICLE_WAIT}s（文章页保持打开，提前关会被警告）...")
        panel_handle = list(before)[0]
        sb.driver.switch_to.window(panel_handle)
        # 倒计时在面板页跑：每 2s 清一次广告/cookie 遮挡，文章标签页全程不关；
        # 中途去文章页看两次（短链就地穿透 + 滚一屏装作阅读），每次看完立刻切回面板
        dwell_end = time.time() + ARTICLE_WAIT
        dwell_start = time.time()
        peeked = 0
        while time.time() < dwell_end:
            try:
                remove_ads(sb)
            except Exception:
                pass
            try:
                dismiss_overlays(sb)
            except Exception:
                pass
            elapsed = time.time() - dwell_start
            if peeked < 2 and elapsed > 5 + peeked * 10:
                try:
                    sb.driver.switch_to.window(article_handle)
                    try:
                        try_passthrough_shortlink(sb)
                    except Exception:
                        pass
                    try:
                        sb.execute_script("window.scrollBy(0, 600);")
                    except Exception:
                        pass
                    time.sleep(3)
                    peeked += 1
                except Exception:
                    pass
                try:
                    sb.driver.switch_to.window(panel_handle)
                except Exception:
                    pass
            time.sleep(2)
        print("  ⏱ 倒计时结束，文章页先不关（弹窗要求 Keep it open，提前关不流转）...")
        try:
            sb.driver.switch_to.window(panel_handle)
        except Exception:
            pass
        time.sleep(4)

    # 3. 等弹窗流转到 Thanks-for-reading（双信号：文案 + 真 Claim 按钮，只查 button/a）
    # 截图实证：dwell 满足后弹窗文案变为 "Thanks for reading! Click Claim Renewal..."，
    # 同时 Turnstile 复选框渲染在弹窗内。Claim 灰色是常态，亮的前提是先点复选框。
    print("  ⏳ 等倒计时走完，找 Claim Renewal...")
    claim_btn = None
    thanks_seen = False
    end3 = time.time() + 120
    while time.time() < end3:
        try:
            if "thanks for reading" in page_text(sb):
                thanks_seen = True
                break
        except Exception:
            pass
        time.sleep(2)
    if thanks_seen:
        print("  ✅ 弹窗已流转（Thanks for reading）")
    else:
        print("  ⚠️ 120s 没读到 Thanks for reading，还找一下 Claim 按钮再定...")
    claim_btn = find_button_by_text(sb, "claim renewal", "claim", "认领", "领取", timeout=30, tags=("button", "a"))
    if claim_btn is None:
        try:
            sb.driver.switch_to.window(panel_handle)
        except Exception:
            pass
        sb.save_screenshot(f"no_claim_btn_{sid}.png")
        return {"status": "❌ 续期失败", "message": "没等到 Thanks-for-reading / Claim 按钮（dwell 没满足或文章页被提前关了）"}

    # 4. 点 Turnstile 复选框（必须！不点 token 出不来，Claim 点了也白点）
    # widget 在 Thanks-for-reading 后才渲染；60s 还没出现就先往下走（点后按需补）。
    print("  ⏳ 等 Turnstile 验证出现...")
    has_ts = False
    for i in range(60):
        try:
            try:
                sb.execute_script(_EXPAND_JS)
            except Exception:
                pass
            if sb.execute_script(_HAS_TURNSTILE_JS):
                has_ts = True
                break
        except Exception:
            pass
        if i % 10 == 9:
            print(f"  …仍在等验证组件 ({i + 1}s)")
        time.sleep(1)
    if has_ts:
        if not handle_turnstile(sb):
            sb.save_screenshot(f"turnstile_fail_{sid}.png")
            return {"status": "❌ 续期失败", "message": "Turnstile 验证未通过（复选框点了 3 次没出 token）"}
    else:
        print("  ℹ️ 未检测到验证组件，先点 Claim（验证码按需出现，下一步补过盾）...")

    # 5. 点 Claim Renewal（用 JS 点击避免被遮挡）
    # 参考 XCQ0607/katabump + oyz/FreezeHost，叠加本次实战修正：
    # ① React 按钮的 disabled 可能是属性/类/aria 三种形态，is_enabled() 不可信，
    #    截图实证 Claim 灰色不可点是常态——不管 disabled 与否都 JS 点一次，
    #    由可见文案 + API 做最终裁判；
    # ② 点完看可见文案（captcha/renewed）决定是补过盾还是成功；
    # ③ Turnstile 经常点了 Claim 才渲染，点后按需过盾；
    # ④ 轮询上限走 CLAIM_TIMEOUT（默认 150s）；
    # ⑤ claimed 只在“可见成功文案”后置位，is_enabled 绝不算成功
    #    （上次误报根源：is_enabled 恒真 + page_source 含 JS 常驻词）。
    print("  🖱️ 点 Claim Renewal...")
    claimed = False
    deadline = time.time() + max(CLAIM_TIMEOUT, 60)
    while time.time() < deadline:
        try:
            remove_ads(sb)
        except Exception:
            pass
        try:
            dismiss_overlays(sb)
        except Exception:
            pass
        try:
            sb.execute_script(_EXPAND_JS)
        except Exception:
            pass
        btn = None
        try:
            els = []
            for tag in ("button", "a"):
                try:
                    els += sb.find_elements(tag)
                except Exception:
                    pass
            for el in els:
                try:
                    if not el.is_displayed():
                        continue
                    if any(k in (el.text or "").lower() for k in ("claim renewal", "claim", "认领", "领取")):
                        btn = el
                        break
                except Exception:
                    continue
        except Exception:
            pass
        if btn is None:
            time.sleep(2)
            continue
        try:
            sb.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        except Exception:
            pass
        time.sleep(0.5)
        try:
            sb.execute_script(
                "arguments[0].removeAttribute('disabled');"
                "arguments[0].removeAttribute('aria-disabled');"
                "arguments[0].classList.remove('disabled');"
                "arguments[0].click();", btn)
        except Exception:
            pass
        time.sleep(3)
        # 点完看 Turnstile 是否按需冒出来（点了 Claim 才渲染是常见形态）
        try:
            if sb.execute_script(_HAS_TURNSTILE_JS) and not sb.execute_script(_SOLVED_JS):
                print("  🔍 点后出现验证组件，过盾...")
                if handle_turnstile(sb):
                    try:
                        sb.execute_script("arguments[0].click();", btn)
                    except Exception:
                        pass
                    time.sleep(3)
        except Exception:
            pass
        src_now = page_text(sb)
        if any(k in src_now for k in ("renewed", "successfully renewed", "renewal successful")):
            claimed = True
            break
        if "please complete the captcha" in src_now or ("captcha" in src_now and "complete" in src_now):
            print("  ⚠️ 页面提示先完成验证，补过盾一次...")
            try:
                if handle_turnstile(sb):
                    try:
                        sb.execute_script("arguments[0].click();", btn)
                    except Exception:
                        pass
                    time.sleep(3)
            except Exception:
                pass
            continue
        # 注意："Current renewal in: 14 days" 这行字在 Thanks-for-reading 状态下也常驻
        # （录制截图实证），绝不能拿它当跳过信号；走完全流程，让 API 做最终裁判。
        time.sleep(2)
    # Claim 点完（无论成败）再关文章页，dwell/流转期间它必须开着
    print("  📰 关闭文章页...")
    try:
        if article_handle:
            sb.driver.switch_to.window(article_handle)
            sb.driver.close()
    except Exception:
        pass
    try:
        if panel_handle:
            sb.driver.switch_to.window(panel_handle)
        else:
            sb.driver.switch_to.window(sb.driver.window_handles[0])
    except Exception:
        pass
    time.sleep(2)
    if not claimed:
        sb.save_screenshot(f"claim_disabled_{sid}.png")
        return {"status": "❌ 续期失败", "message": f"Claim 点了但没出现成功确认（已等{max(CLAIM_TIMEOUT, 60)}s，请看截图人工确认）"}
    time.sleep(8)

    # 6. 读结果：可见文案 + API 双重确认，API 是最终裁判
    # （只信 innerText；'extended' 一词 JS 包常驻，已从成功关键词里剔除）
    src = page_text(sb)
    if "renew limit reached" in src:
        return {"status": "⏭️ 跳过", "message": "已达续期上限（Renew Limit Reached）"}
    before_renewal, before_full = None, ""
    if precheck and isinstance(precheck.get("info"), tuple) and len(precheck["info"]) == 3:
        before_renewal, _, before_full = precheck["info"]
        before_full = str(before_full or "")
    after_renewal, after_full = None, ""
    if precheck and precheck.get("api_session"):
        try:
            after_renewal, _, exp = api_get_info(precheck["api_session"], precheck["api_xsrf"], server_uuid)
            after_full = str(exp or "")
        except Exception:
            pass
    after_exp = (after_full or "")[:10]
    before_exp = (before_full or "")[:10]
    api_confirmed = (
        after_renewal is not None and before_renewal is not None and after_renewal > before_renewal
    ) or (
        bool(after_full) and bool(before_full) and after_full > before_full
    )
    page_ok = any(k in src for k in ("renewed", "successfully renewed", "renewal successful"))
    if page_ok and (api_confirmed or after_renewal is None):
        ss_path = f"renew_success_{sid}.png"
        sb.save_screenshot(ss_path)
        print(f"  📸 截图: {ss_path}")
        return {"status": "✅ 续期成功", "message": "Claim 成功（页面+API 双确认）", "screenshot": ss_path, "expires_at": after_exp, "after_exp": after_exp, "before_exp": before_exp}
    if page_ok and not api_confirmed:
        sb.save_screenshot(f"claim_unconfirmed_{sid}.png")
        return {"status": "⚠️ 未知结果", "message": "页面像成功了但 API 到期时间没变，不敢报成功，请人工看一眼面板"}
    if api_confirmed:
        ss_path = f"renew_success_{sid}.png"
        sb.save_screenshot(ss_path)
        print(f"  📸 截图: {ss_path}")
        return {"status": "✅ 续期成功", "message": "Claim 成功（API 到期时间已延后）", "screenshot": ss_path, "expires_at": after_exp, "after_exp": after_exp, "before_exp": before_exp}
    if "captcha" in src and "complete" in src:
        return {"status": "❌ 续期失败", "message": "提交后仍提示先完成验证"}
    sb.save_screenshot(f"claim_unknown_{sid}.png")
    return {"status": "⚠️ 未知结果", "message": "已点 Claim，但没读到明确成功提示，请人工看一眼面板"}


def fmt_msg(status, label, server_uuid, detail, before_exp="", after_exp=""):
    sid = (server_uuid or "").split("-")[0][:8]
    lines = ["🎰 Orihost 续期报告", "", status]
    if ORIHOST_EMAIL:
        lines.append(f"📧 账号: {mask_email(ORIHOST_EMAIL)}")
    lines.append(f"🆔 服务器: {sid}")
    if before_exp:
        lines.append(f"⏱ 续期前到期时间: {before_exp}")
    if after_exp:
        lines.append(f"⏱ 续期后到期时间: {after_exp}")
    if detail:
        lines.append(f"📌 {detail}")
    lines.append(f"⏰ {now_bj()}")
    return "\n".join(lines)


# ---------- 主入口 ----------
def main():
    print("#" * 42)
    print("   Orihost 浏览器自动续期" + ("（代理开）" if IS_PROXY else "（直连）"))
    print("#" * 42)
    accounts = load_accounts()
    if not accounts:
        print("❌ 未配置账号。请设置 ORIHOST_REMEMBER + ORIHOST_SERVER_IDS ...")
        sys.exit(1)

    sb_kwargs = {"uc": True, "headless": False,
                 "chromium_arg": "--disable-popup-blocking,--disable-notifications"}
    if IS_PROXY:
        print(f"🔗 挂载代理: {PROXY_STR}")
        sb_kwargs["proxy"] = PROXY_STR
    else:
        print("🌐 未使用代理，直连访问")

    results = []
    print("🚀 启动浏览器...")
    with SB(**sb_kwargs) as sb:
        try:
            sb.open("https://api.ip.sb/ip")
            print(f"📍 当前出口IP: {sb.get_text('body')}")
        except Exception:
            pass
        for acc in accounts:
            label = acc["label"]
            print(f"\n{'=' * 42}\n {label}：{len(acc['servers'])} 台\n{'=' * 42}")
            if not cookie_login(sb, acc["auth"]):
                for sv in acc["servers"]:
                    info = {"label": label, "server": sv, "status": "❌ 登录失败", "message": "Cookie 免登失败，remember 可能失效"}
                    results.append(info)
                    send_tg(fmt_msg(info["status"], label, sv, info["message"]))
                continue

            # API 预检：满额/冷却直接跳过，不开浏览器
            api_session, api_xsrf = _get_session(acc["auth"])
            for sv in acc["servers"]:
                precheck = None
                if api_session and api_xsrf:
                    info = api_get_info(api_session, api_xsrf, sv)
                    cd = api_check_cooldown(api_session, api_xsrf, sv)
                    precheck = {"info": info, "cooldown": cd, "api_session": api_session, "api_xsrf": api_xsrf}
                try:
                    r = renew_one_server(sb, sv, precheck)
                except Exception as e:
                    r = {"status": "❌ 续期失败", "message": f"异常: {str(e)[:120]}"}
                info = {"label": label, "server": sv, "status": r["status"], "message": r.get("message", "")}
                results.append(info)
                print(f"  {info['status']} {info['message']}")
                ss = r.get("screenshot", "")
                send_tg(fmt_msg(info["status"], label, sv, info["message"], r.get("before_exp", ""), r.get("after_exp", "")), ss)
                if "成功" in r["status"] and r.get("expires_at"):
                    updateCronSchedule(r["expires_at"])
                time.sleep(random.randint(2, 5))

    ok = sum(1 for r in results if "成功" in r["status"])
    skip = sum(1 for r in results if "跳过" in r["status"])
    fail = len(results) - ok - skip
    print(f"\n{'=' * 42}\n📊 汇总：{ok} 成功 / {skip} 跳过 / {fail} 失败，共 {len(results)} 台\n{'=' * 42}")
    if fail:
        sys.exit(2)


if __name__ == "__main__":
    main()
