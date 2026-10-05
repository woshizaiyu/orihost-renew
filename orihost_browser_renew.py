#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Orihost 浏览器自动续期（SeleniumBase + 真浏览器）
# 背景：面板 claim 接口强制要求 Cloudflare Turnstile token（GET /api/client/renewal/complete?cf-turnstile-response=xxx），
#       纯 HTTP 调不通（无 token 直接 500），必须用真浏览器点验证。
# 流程：Cookie 免登 → 服务器页 → Renew Now → Read Article（新标签读文章）→ 倒计时 → 点 Turnstile → Claim Renewal
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


def handle_turnstile(sb) -> bool:
    print("🔍 处理 Cloudflare Turnstile 验证...")
    time.sleep(2)
    try:
        if sb.execute_script(_SOLVED_JS):
            print("✅ 已静默通过")
            return True
    except Exception:
        pass
    for _ in range(3):
        try:
            sb.execute_script(_EXPAND_JS)
        except Exception:
            pass
        time.sleep(0.5)
    for attempt in range(6):
        try:
            if sb.execute_script(_SOLVED_JS):
                print(f"✅ Turnstile 通过（第 {attempt} 次尝试）")
                return True
        except Exception:
            pass
        print(f"🖱️ 第 {attempt + 1} 次调用 uc_gui_click_captcha...")
        try:
            sb.uc_gui_click_captcha()
        except Exception as e:
            print(f"⚠️ uc_gui_click_captcha 调用异常: {e}")
        for _ in range(16):
            time.sleep(0.5)
            try:
                if sb.execute_script(_SOLVED_JS):
                    print(f"✅ Turnstile 通过（第 {attempt + 1} 次尝试）")
                    return True
            except Exception:
                pass
        print(f"⚠️ 第 {attempt + 1} 次未通过，重试...")
    print("  ❌ Turnstile 6 次均失败")
    return False


# ---------- 广告拦截（CI 环境广告 iframe 可能遮挡按钮） ----------
_REMOVE_ADS_JS = """
(function() {
    // 移除广告 iframe
    document.querySelectorAll('iframe').forEach(function(f) {
        var src = f.src || '';
        if (src.includes('n6wxm.com') || src.includes('nap5k.com') || 
            src.includes('5gvci.com') || src.includes('jhnwr.com') ||
            src.includes('my.rtmark.net') || src.includes('vignette') ||
            src.includes('tag.min.js') || src.includes('ad') ||
            src.includes('advert') || src.includes('popup') ||
            src.includes('overlay') || src.includes('modal')) {
            f.remove();
        }
    });
    // 移除固定定位的广告覆盖层
    document.querySelectorAll('div').forEach(function(d) {
        var s = window.getComputedStyle(d);
        if ((s.position === 'fixed' || s.position === 'absolute') && 
            s.zIndex > 100 && d.offsetHeight > 100) {
            var text = d.innerText || '';
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
def find_button_by_text(sb, *keywords, timeout=10):
    """在 button、a、div 里找文本包含关键词的第一个可见元素"""
    end = time.time() + timeout
    kws = [k.lower() for k in keywords]
    while time.time() < end:
        try:
            for el in sb.find_elements("button") + sb.find_elements("a") + sb.find_elements("div"):
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
    try:
        return (sb.get_page_source() or "").lower()
    except Exception:
        return ""


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

    # 1. 点 Renew Now（兼容 Google 翻译后的中文文案）
    print("  🔍 找 Renew Now 按钮...")
    remove_ads(sb)
    time.sleep(1)
    renew_btn = find_button_by_text(sb, "renew now", "renew", "更新", "续期", timeout=20)
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

    # 2. 点 Read Article（会弹新标签，兼容翻译后的中文文案）
    print("  🖱️ 点 Read Article...")
    read_btn = find_button_by_text(sb, "read article", "read", "阅读文章", "阅读", timeout=15)
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
        print(f"  📰 文章页已打开，停留 {ARTICLE_WAIT}s（提前关闭会被警告）...")
        sb.driver.switch_to.window(article_handle)
        time.sleep(ARTICLE_WAIT)
        sb.driver.close()
        sb.driver.switch_to.window(list(before)[0])
        time.sleep(4)

    # 3. 等倒计时走完（Claim 按钮出现，兼容翻译后的中文文案）
    print("  ⏳ 等倒计时走完，找 Claim Renewal...")
    claim_btn = find_button_by_text(sb, "claim renewal", "claim", "认领", "领取", timeout=120)
    if claim_btn is None:
        sb.save_screenshot(f"no_claim_btn_{sid}.png")
        return {"status": "❌ 续期失败", "message": "120s 没等到 Claim Renewal（倒计时异常）"}

    # 4. 等 Turnstile 出现（倒计时走完后才弹出）
    # 参考 eooce/katabump-renew：widget 常被父容器 overflow:hidden 裁剪，
    # 等待轮询中必须同步做 _EXPAND_JS，否则 _HAS_TURNSTILE_JS 一直 false
    #（本次 CI 日志就是“未检测到验证组件”→ Claim 永久 disabled）。
    # 另见 .tmp/out/apilog.jsonl：begin 返回 dwell_seconds=15，倒计时走完才出验证。
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
            return {"status": "❌ 续期失败", "message": "Turnstile 验证 6 次未通过"}
    else:
        # 参考 XCQ0607/katabump：shadow-DOM 下 input 可能还没挂载，
        # 但 checkbox iframe 已可点；仍尝试一次过盾，而不是直接放弃 Claim。
        print("  ℹ️ 未检测到验证组件，尝试直接过盾一次（shadow-DOM 兜底）...")
        try:
            handle_turnstile(sb)
        except Exception:
            pass

    # 5. 点 Claim Renewal（用 JS 点击避免被遮挡）
    # 参考 XCQ0607/katabump + oyz/FreezeHost：
    # ① React 按钮的 disabled 可能是属性/类/aria 三种形态，is_enabled() 不可信，
    #    必须 scrollIntoView + 去广告遮挡后 JS 强点；
    # ② 点完看页面文案（captcha/renewed）决定是补过盾还是成功，不只看 disabled；
    # ③ 轮询上限走 CLAIM_TIMEOUT（默认 150s），之前硬编码 30*2=60s 太短。
    print("  🖱️ 点 Claim Renewal...")
    claimed = False
    deadline = time.time() + max(CLAIM_TIMEOUT, 60)
    attempt_n = 0
    while time.time() < deadline:
        attempt_n += 1
        try:
            remove_ads(sb)
            try:
                sb.execute_script(_EXPAND_JS)
            except Exception:
                pass
            els = []
            for tag in ("button", "a"):
                try:
                    els += sb.find_elements(tag)
                except Exception:
                    pass
            btns = [el for el in els
                    if el.is_displayed() and any(k in (el.text or "").lower()
                                                 for k in ("claim renewal", "claim", "认领", "领取"))]
            if btns:
                btn = btns[0]
                try:
                    sb.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                except Exception:
                    pass
                time.sleep(0.5)
                try:
                    if btn.is_enabled():
                        sb.execute_script("arguments[0].click();", btn)
                    else:
                        # disabled 时先补一次过盾（验证刚完成、按钮还没刷新是常见竞态），
                        # 仍不可点则去掉 disabled 强点一次，由后端/页面文案做最终裁判。
                        if sb.execute_script(_SOLVED_JS):
                            sb.execute_script(
                                "arguments[0].removeAttribute('disabled');"
                                "arguments[0].classList.remove('disabled');"
                                "arguments[0].click();", btn)
                        elif attempt_n % 5 == 0:
                            print(f"  …Claim 仍不可点 ({attempt_n} 次)，已解验证="
                                  f"{bool(sb.execute_script(_SOLVED_JS))}，继续等倒计时/验证")
                except Exception:
                    pass
                # 每次点击后看结果文案：成功 / 仍要验证 / 继续轮询
                time.sleep(2)
                src_now = page_text(sb)
                if any(k in src_now for k in ("renewed", "successfully renewed", "renewal successful", "extended")):
                    claimed = True
                    break
                if "please complete the captcha" in src_now or ("captcha" in src_now and "complete" in src_now):
                    print("  ⚠️ 页面提示先完成验证，补过盾一次...")
                    try:
                        handle_turnstile(sb)
                    except Exception:
                        pass
                    continue
                # 按钮已可点且点过一次，算 claimed，交给第 6 步统一判读
                try:
                    if btn.is_enabled():
                        claimed = True
                        break
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(2)
    if not claimed:
        sb.save_screenshot(f"claim_disabled_{sid}.png")
        return {"status": "❌ 续期失败", "message": f"Claim 按钮一直不可点（倒计时/验证没完成，已等{max(CLAIM_TIMEOUT, 60)}s）"}
    time.sleep(8)

    # 6. 读结果
    src = page_text(sb)
    if "renew limit reached" in src:
        return {"status": "⏭️ 跳过", "message": "已达续期上限（Renew Limit Reached）"}
    if any(k in src for k in ("renewed", "successfully renewed", "renewal successful", "extended")):
        ss_path = f"renew_success_{sid}.png"
        sb.save_screenshot(ss_path)
        print(f"  📸 截图: {ss_path}")
        after_exp = ""
        if precheck and precheck.get("api_session"):
            try:
                _, _, exp = api_get_info(precheck["api_session"], precheck["api_xsrf"], server_uuid)
                if exp:
                    after_exp = exp[:10]
            except Exception:
                pass
        return {"status": "✅ 续期成功", "message": "Claim 成功（页面确认）", "screenshot": ss_path, "expires_at": after_exp, "after_exp": after_exp}
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
