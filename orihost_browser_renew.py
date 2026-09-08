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


def send_tg(msg: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        tg_lib.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg},
            timeout=15,
        )
    except Exception as e:
        print(f"  TG 发送失败: {e}")


def now_bj():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


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


# ---------- 页面工具（文本匹配按钮，面板是 React，文本最稳） ----------
def find_button_by_text(sb, *keywords, timeout=10):
    """在 button 和 a 里找文本包含关键词的第一个可见元素"""
    end = time.time() + timeout
    kws = [k.lower() for k in keywords]
    while time.time() < end:
        try:
            for el in sb.find_elements("button") + sb.find_elements("a"):
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


# ---------- 单台续期 ----------
def renew_one_server(sb, server_uuid: str) -> dict:
    sid = (server_uuid or "").split("-")[0][:8]
    print(f"\n  🖥 [{sid}] 打开服务器页...")
    # 面板路由用的是 8 位短 ID（如 /server/8651e616），填了完整 UUID 也只取前 8 位
    sb.open(f"{PANEL}/server/{sid}")
    time.sleep(8)

    src = page_text(sb)
    if "renew limit reached" in src:
        return {"status": "⏭️ 跳过", "message": "已达续期上限（Renew Limit Reached）"}
    if "expired renewal" in src or "suspended due" in src:
        print("  ⚠️ 服务器因过期被暂停，走续期流程恢复")

    # 1. 点 Renew Now
    print("  🔍 找 Renew Now 按钮...")
    renew_btn = find_button_by_text(sb, "renew now", timeout=20)
    if renew_btn is None:
        sb.save_screenshot(f"no_renew_btn_{sid}.png")
        return {"status": "❌ 续期失败", "message": "没找到 Renew Now 按钮（页面结构可能变了）"}
    try:
        renew_btn.click()
    except Exception:
        sb.execute_script("arguments[0].click();", renew_btn)
    time.sleep(4)

    # 2. 点 Read Article（会弹新标签）
    print("  🖱️ 点 Read Article...")
    read_btn = find_button_by_text(sb, "read article", timeout=15)
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

    # 3. 等倒计时走完（Claim 按钮出现）
    print("  ⏳ 等倒计时走完，找 Claim Renewal...")
    claim_btn = find_button_by_text(sb, "claim renewal", timeout=120)
    if claim_btn is None:
        sb.save_screenshot(f"no_claim_btn_{sid}.png")
        return {"status": "❌ 续期失败", "message": "120s 没等到 Claim Renewal（倒计时异常）"}

    # 4. 过 Turnstile（有才点，没有就跳过）
    try:
        has_ts = sb.execute_script(_HAS_TURNSTILE_JS)
    except Exception:
        has_ts = False
    if has_ts:
        if not handle_turnstile(sb):
            sb.save_screenshot(f"turnstile_fail_{sid}.png")
            return {"status": "❌ 续期失败", "message": "Turnstile 验证 6 次未通过"}
    else:
        print("  ℹ️ 未检测到验证组件")

    # 5. 点 Claim Renewal（等它从 disabled 变可点）
    print("  🖱️ 点 Claim Renewal...")
    claimed = False
    for _ in range(60):
        try:
            btns = [el for el in sb.find_elements("button") if el.is_displayed() and "claim renewal" in (el.text or "").lower()]
            if btns and btns[0].is_enabled():
                try:
                    btns[0].click()
                except Exception:
                    sb.execute_script("arguments[0].click();", btns[0])
                claimed = True
                break
        except Exception:
            pass
        time.sleep(2)
    if not claimed:
        return {"status": "❌ 续期失败", "message": "Claim 按钮一直不可点（倒计时/验证没完成）"}
    time.sleep(8)

    # 6. 读结果
    src = page_text(sb)
    if "renew limit reached" in src:
        return {"status": "⏭️ 跳过", "message": "已达续期上限（Renew Limit Reached）"}
    if any(k in src for k in ("renewed", "successfully renewed", "renewal successful", "extended")):
        return {"status": "✅ 续期成功", "message": "Claim 成功（页面确认）"}
    if "captcha" in src and "complete" in src:
        return {"status": "❌ 续期失败", "message": "提交后仍提示先完成验证"}
    sb.save_screenshot(f"claim_unknown_{sid}.png")
    return {"status": "⚠️ 未知结果", "message": "已点 Claim，但没读到明确成功提示，请人工看一眼面板"}


def fmt_msg(status, label, server_uuid, detail):
    sid = (server_uuid or "").split("-")[0][:8]
    return f"🖥 Orihost 浏览器续期\n{status}\n👤 {label}\n🆔 {sid}\n📌 {detail}\n⏰ {now_bj()}（北京）"


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
            for sv in acc["servers"]:
                try:
                    r = renew_one_server(sb, sv)
                except Exception as e:
                    r = {"status": "❌ 续期失败", "message": f"异常: {str(e)[:120]}"}
                info = {"label": label, "server": sv, "status": r["status"], "message": r.get("message", "")}
                results.append(info)
                print(f"  {info['status']} {info['message']}")
                send_tg(fmt_msg(info["status"], label, sv, info["message"]))
                time.sleep(random.randint(2, 5))

    ok = sum(1 for r in results if "成功" in r["status"])
    skip = sum(1 for r in results if "跳过" in r["status"])
    fail = len(results) - ok - skip
    print(f"\n{'=' * 42}\n📊 汇总：{ok} 成功 / {skip} 跳过 / {fail} 失败，共 {len(results)} 台\n{'=' * 42}")
    if fail:
        sys.exit(2)


if __name__ == "__main__":
    main()
