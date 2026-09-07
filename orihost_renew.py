#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Orihost 免费服务器自动续期（Jexactyl 面板）
# 核心：remember_web 长效 token 自动置换 session + XSRF，避免静态 Cookie 419/401 过期
# 流程：POST /api/client/servers/{uuid}/renew/begin -> 等待 dwell_seconds -> GET /api/client/renewal/complete

import os
import re
import sys
import time
import random
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote

# 优先用 curl_cffi（Chrome 指纹，过基础盾 + 自动维持 Session），缺失时回退 requests
try:
    from curl_cffi import requests as http_lib
    HAS_CFFI = True
    SESSION_KW = {"impersonate": "chrome120"}
except Exception:
    import requests as http_lib
    HAS_CFFI = False
    SESSION_KW = {}

try:
    import requests as tg_lib
except Exception:
    tg_lib = http_lib

PANEL = "https://panel.orihost.com"
# yanyumm1 实测的 remember cookie 名（Jexactyl 默认前缀 remember_web_ + hash）
DEFAULT_REMEMBER_NAME = "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d"

RENEWAL_MAX = int(os.environ.get("RENEWAL_MAX") or "21")
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS") or "5")
DWELL_EXTRA = int(os.environ.get("DWELL_EXTRA") or "2")

# ---------- 代理 ----------
# 优先级：ORIHOST_PROXY 显式指定 > 工作流 sing-box（IS_PROXY/PROXY_SERVER，由 NODE_LINK 节点链接转出） > 标准 HTTP(S)_PROXY
# vless/trojan 等节点链接请填到 Secrets 的 NODE_LINK（工作流 sing-box 步骤会转成本地代理），不要填 ORIHOST_PROXY
def _get_proxy():
    explicit = (os.environ.get("ORIHOST_PROXY") or os.environ.get("ORIHOST_GOST_PROXY") or "").strip()
    if explicit:
        scheme = explicit.split("://", 1)[0].lower() if "://" in explicit else ""
        if scheme in ("http", "https", "socks4", "socks5", "socks5h"):
            return {"http": explicit, "https": explicit}
        print(f"  ⚠️ ORIHOST_PROXY 格式不支持 ({scheme}://)，已忽略；节点链接请填 NODE_LINK")
    if os.environ.get("IS_PROXY", "").lower() == "true":
        srv = (os.environ.get("PROXY_SERVER") or "socks5://127.0.0.1:1080").strip()
        # setup_proxy.sh 的本地代理同时监听 http 1081；http 代理对 requests/curl_cffi 兼容最好，优先用它
        if srv.lower() in ("socks5://127.0.0.1:1080", "socks5h://127.0.0.1:1080"):
            srv = "http://127.0.0.1:1081"
        print(f"  🔗 使用 sing-box 代理: {srv}")
        return {"http": srv, "https": srv}
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        p = (os.environ.get(k) or "").strip()
        if p:
            return {"http": p, "https": p}
    return None

PROXIES = _get_proxy()

# ---------- Telegram ----------
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""
TG_CHAT_ID = os.environ.get("TG_CHAT_ID") or ""
# 兼容 yanyumm1 的 TG_BOT="chat_id,token" 写法
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


# ---------- 账号解析 ----------
def _split_ids(raw: str):
    return [s.strip() for s in (raw or "").replace(";", ",").split(",") if s.strip()]


def _detect_input_kind(value: str) -> str:
    """判定用户填的是 裸 remember token / 完整 Cookie 串"""
    v = (value or "").strip()
    if not v:
        return "empty"
    if "remember_web" in v:
        # 包含 remember_web 名：可能是 "name=value" 或完整 Cookie 串
        if ";" in v or "XSRF-TOKEN" in v or "jexactyl_session" in v:
            return "full_cookie"
        if "=" in v:
            return "remember_pair"
        return "remember_raw"
    if ";" in v and "=" in v:
        return "full_cookie"
    if "=" in v and len(v) < 300 and v.count("=") == 1 and ";" not in v:
        return "remember_pair"
    # 短纯 token（无 = 无 ;）视为裸 token
    return "remember_raw"


def _parse_full_cookie(cookie_str: str) -> dict:
    cookies = {}
    for item in cookie_str.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        k, v = item.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k or k.lower() in ("path", "expires", "domain", "max-age", "samesite", "secure", "httponly"):
            continue
        try:
            v = unquote(v)
        except Exception:
            pass
        cookies[k] = v
    return cookies


def load_accounts():
    """支持多账号：ORIHOST_REMEMBER_N / ORIHOST_COOKIE_N + ORIHOST_SERVER_IDS_N，向下兼容单账号写法"""
    accounts = []
    for i in range(1, 20):
        token_raw = (
            os.environ.get(f"ORIHOST_REMEMBER_{i}")
            or os.environ.get(f"ORIHOST_COOKIE_{i}")
            or ""
        ).strip()
        ids_raw = os.environ.get(f"ORIHOST_SERVER_IDS_{i}") or ""
        if not token_raw and not ids_raw:
            continue
        ids = _split_ids(ids_raw)
        if not token_raw:
            print(f"⚠️ 账号{i} token 为空，跳过")
            continue
        if not ids:
            print(f"⚠️ 账号{i} ORIHOST_SERVER_IDS_{i} 为空，跳过")
            continue
        accounts.append({"label": f"账号{i}", "auth": token_raw, "servers": ids})
        if not os.environ.get(f"ORIHOST_REMEMBER_{i+1}") and not os.environ.get(f"ORIHOST_COOKIE_{i+1}"):
            # 允许空洞后面还有？继续扫完 19 个，不提前 break 以免漏 _3
            pass

    if not accounts:
        # 单账号兼容：ORIHOST_REMEMBER / ORI_COOKIE(yanyumm1) / ORIHOST_COOKIE
        single_auth = (
            os.environ.get("ORIHOST_REMEMBER")
            or os.environ.get("ORI_COOKIE")
            or os.environ.get("ORIHOST_COOKIE")
            or ""
        ).strip()
        single_ids = _split_ids(
            os.environ.get("ORIHOST_SERVER_IDS")
            or os.environ.get("ORIHOST_SERVER_IDS_1")
            or ""
        )
        if single_auth and single_ids:
            accounts.append({"label": "默认账号", "auth": single_auth, "servers": single_ids})

    return accounts


# ---------- 会话（remember 自动置换） ----------
def make_session(auth_raw: str):
    """用 remember 长效 token 置换出最新 session + XSRF，返回 (session, xsrf)"""
    kind = _detect_input_kind(auth_raw)
    s = http_lib.Session(**SESSION_KW) if HAS_CFFI else http_lib.Session()
    if PROXIES:
        try:
            s.proxies.update(PROXIES)
        except Exception:
            pass
    try:
        s.headers.update({"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    except Exception:
        pass

    if kind == "full_cookie":
        for k, v in _parse_full_cookie(auth_raw).items():
            try:
                s.cookies.set(k, v, domain="panel.orihost.com")
            except Exception:
                pass
    elif kind == "remember_pair":
        name, val = auth_raw.split("=", 1)
        try:
            s.cookies.set(name.strip(), val.strip(), domain="panel.orihost.com")
        except Exception:
            pass
    else:  # remember_raw：裸 token，用默认 cookie 名
        try:
            s.cookies.set(DEFAULT_REMEMBER_NAME, auth_raw.strip(), domain="panel.orihost.com")
        except Exception:
            pass

    # 访问 dashboard 触发服务端签发新 session + XSRF，最多试 5 次
    last_err = None
    for _ in range(5):
        try:
            kw = {"timeout": 20}
            if HAS_CFFI:
                r = s.get(f"{PANEL}/dashboard", **kw)
            else:
                r = s.get(f"{PANEL}/dashboard", **kw)
            xsrf = None
            try:
                xsrf = s.cookies.get("XSRF-TOKEN")
            except Exception:
                xsrf = None
            if xsrf:
                return s, unquote(xsrf)
            # 有些版本 Set-Cookie 在响应头里但 jar 没收录，兜底从响应解析
            try:
                m = re.search(r"XSRF-TOKEN=([^;]+)", r.headers.get("Set-Cookie", "") or "")
                if m:
                    return s, unquote(m.group(1))
            except Exception:
                pass
            time.sleep(2)
        except Exception as e:
            last_err = e
            time.sleep(2)
    raise RuntimeError(f"置换 session 失败（remember 可能失效）：{last_err}")


def refresh_xsrf(s):
    try:
        s.get(f"{PANEL}/dashboard", timeout=15)
        xsrf = s.cookies.get("XSRF-TOKEN")
        if xsrf:
            return unquote(xsrf)
    except Exception:
        pass
    return None


def build_headers(xsrf: str, server_short: str) -> dict:
    h = {
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{PANEL}/server/{server_short}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if xsrf:
        h["X-XSRF-TOKEN"] = xsrf
    return h


# ---------- 业务接口 ----------
def short_id(uuid: str) -> str:
    return (uuid or "").strip().split("-")[0][:8]


def api_get(s, path: str, headers: dict):
    kw = {"headers": headers, "timeout": 20}
    if PROXIES and not HAS_CFFI:
        kw["proxies"] = PROXIES
    return s.get(f"{PANEL}{path}", **kw)


def api_post(s, path: str, headers: dict):
    kw = {"headers": headers, "timeout": 30}
    if PROXIES and not HAS_CFFI:
        kw["proxies"] = PROXIES
    return s.post(f"{PANEL}{path}", **kw)


def get_info(s, headers, server_uuid: str):
    """查剩余续期次数与到期天数，UUID 与短 ID 双试"""
    for ident in (server_uuid, short_id(server_uuid)):
        try:
            r = api_get(s, f"/api/client/servers/{ident}", headers)
            if not r.ok:
                continue
            attrs = r.json().get("attributes", {})
            renewal = attrs.get("renewal", 0)
            try:
                renewal = int(renewal)
            except Exception:
                renewal = 0
            days = None
            exp = attrs.get("expires_at")
            if exp:
                try:
                    dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                    days = round((dt - datetime.now(timezone.utc)).total_seconds() / 86400, 1)
                except Exception:
                    days = None
            return renewal, days
        except Exception:
            continue
    return None, None


def check_cooldown(s, headers, server_uuid: str) -> int:
    try:
        r = api_get(s, f"/api/client/servers/{server_uuid}/renew/cooldown", headers)
        if r.ok:
            return int(r.json().get("seconds", 0) or 0)
    except Exception:
        pass
    return 0


def do_renew_once(s, headers, server_uuid: str) -> dict:
    # 冷却等待（超过 5 分钟则本轮放弃，避免 Actions 超时）
    cd = check_cooldown(s, headers, server_uuid)
    if cd > 0:
        if cd > 300:
            return {"status": "cooldown", "message": f"冷却中 {cd}s，本轮跳过"}
        print(f"  ⏳ 冷却 {cd}s")
        time.sleep(cd + 1)

    try:
        r = api_post(s, f"/api/client/servers/{server_uuid}/renew/begin", headers)
    except Exception as e:
        return {"status": "error", "message": f"begin 请求失败: {e}"}

    if r.status_code == 419:
        return {"status": "reauth", "message": "419 CSRF 过期，需刷新 session"}
    if r.status_code == 401:
        return {"status": "reauth", "message": "401 未认证，需刷新 session"}
    if not r.ok:
        return {"status": "error", "message": f"begin HTTP {r.status_code}: {r.text[:150]}"}
    try:
        data = r.json()
    except Exception:
        return {"status": "error", "message": f"begin 解析失败: {r.text[:150]}"}

    ad_url = data.get("url") or ""
    wait = int(data.get("dwell_seconds") or 15)
    print(f"  📰 广告已获取，模拟阅读 {wait}s")
    if ad_url:
        try:
            s.get(ad_url, timeout=20)
        except Exception:
            pass
    time.sleep(wait + DWELL_EXTRA + random.randint(1, 3))

    for _ in range(3):
        try:
            r2 = api_get(s, "/api/client/renewal/complete", headers)
        except Exception as e:
            time.sleep(2)
            continue
        if r2.status_code in (401, 419):
            return {"status": "reauth", "message": f"complete {r2.status_code}，需刷新 session"}
        if r2.status_code in (200, 204):
            try:
                res = r2.json() if r2.text else {}
            except Exception:
                res = {}
            renewed = int(res.get("renewed_count", 0) or 0)
            skipped = int(res.get("skipped_count", 0) or 0)
            if renewed > 0:
                return {"status": "success", "message": f"续期成功 (+{renewed})"}
            if skipped > 0:
                return {"status": "skipped", "message": "已达本周期续期上限"}
            return {"status": "unknown", "message": f"未预期响应: {str(res)[:150]}"}
        # 其他错误刷新 XSRF 重试
        new_xsrf = refresh_xsrf(s)
        if new_xsrf:
            headers["X-XSRF-TOKEN"] = new_xsrf
        time.sleep(2)
    return {"status": "error", "message": "complete 失败（3 次重试）"}


def renew_server_loop(s, xsrf: str, server_uuid: str) -> dict:
    sid = short_id(server_uuid)
    headers = build_headers(xsrf, sid)
    renewal, days = get_info(s, headers, server_uuid)
    print(f"  📅 当前续期: {renewal} / 剩余 {days} 天")
    if renewal is not None and renewal >= RENEWAL_MAX:
        return {"status": "⏭️ 已满", "message": f"续期 {renewal}/{RENEWAL_MAX} 已满", "renewal": renewal, "days": days}

    count = 0
    last_msg = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"  🔄 [{sid}] 第 {attempt}/{MAX_ATTEMPTS} 次续期...")
        res = do_renew_once(s, headers, server_uuid)
        if res["status"] == "reauth":
            # remember 自动置换：重新访问 dashboard 拿新 XSRF 后重试本轮
            print("  🔑 session 过期，自动置换中...")
            new_xsrf = refresh_xsrf(s)
            if not new_xsrf:
                return {"status": "❌ 续期失败", "message": "session 刷新失败，remember 可能失效", "renewal": renewal, "days": days}
            headers["X-XSRF-TOKEN"] = new_xsrf
            res = do_renew_once(s, headers, server_uuid)
        if res["status"] == "success":
            count += 1
            last_msg = res["message"]
            time.sleep(random.randint(3, 7))
            renewal, days = get_info(s, headers, server_uuid)
            print(f"  ➡️ 续期后: {renewal} / {days} 天")
            if renewal is not None and renewal >= RENEWAL_MAX:
                break
            continue
        if res["status"] in ("skipped", "cooldown"):
            last_msg = res["message"]
            break
        last_msg = res["message"]
        print(f"  ❌ {last_msg}")
        break

    if count > 0:
        return {"status": "✅ 续期成功", "message": f"{last_msg}（本轮 +{count}）", "renewal": renewal, "days": days}
    if "上限" in last_msg or "冷却" in last_msg:
        return {"status": "⏭️ 跳过", "message": last_msg, "renewal": renewal, "days": days}
    return {"status": "❌ 续期失败", "message": last_msg or "未知错误", "renewal": renewal, "days": days}


def fmt_msg(status, label, server_uuid, detail, renewal=None, days=None):
    now = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    extra = ""
    if renewal is not None or days is not None:
        extra = f"\n📊 续期: {renewal} / 剩余 {days} 天"
    return f"🖥 Orihost 自动续期\n{status}\n👤 {label}\n🆔 {short_id(server_uuid)}{extra}\n📌 {detail}\n⏰ {now}（北京）"


def main():
    print("=" * 42)
    print(f" Orihost 自动续期（curl_cffi={'开' if HAS_CFFI else '关/回退requests'}，代理={'开' if PROXIES else '关'}）")
    print("=" * 42)
    accounts = load_accounts()
    if not accounts:
        print("❌ 未配置账号。请设置 ORIHOST_REMEMBER + ORIHOST_SERVER_IDS，或 ORIHOST_REMEMBER_1 + ORIHOST_SERVER_IDS_1 ...")
        sys.exit(1)
    print(f"📋 {len(accounts)} 个账号")
    results = []
    for acc in accounts:
        label = acc["label"]
        print(f"\n{'=' * 42}\n {label}：{len(acc['servers'])} 台\n{'=' * 42}")
        try:
            s, xsrf = make_session(acc["auth"])
            print("  🔑 session 置换成功")
        except Exception as e:
            print(f"  ❌ 登录失败: {e}")
            for sv in acc["servers"]:
                info = {"label": label, "server": sv, "status": "❌ 登录失败", "message": str(e)[:120]}
                results.append(info)
                send_tg(fmt_msg(info["status"], label, sv, info["message"]))
            continue
        for sv in acc["servers"]:
            print(f"\n  🖥 [{short_id(sv)}] ...")
            try:
                r = renew_server_loop(s, xsrf, sv)
                # 同一 session 下 XSRF 可能滚动，同步最新
                new_xsrf = None
                try:
                    new_xsrf = s.cookies.get("XSRF-TOKEN")
                    if new_xsrf:
                        xsrf = unquote(new_xsrf)
                except Exception:
                    pass
            except Exception as e:
                r = {"status": "❌ 续期失败", "message": str(e)[:120]}
            info = {"label": label, "server": sv, "status": r["status"], "message": r.get("message", "")}
            results.append(info)
            send_tg(fmt_msg(info["status"], label, sv, info["message"], r.get("renewal"), r.get("days")))
            time.sleep(random.randint(2, 5))

    ok = sum(1 for r in results if "成功" in r["status"])
    skip = sum(1 for r in results if "跳过" in r["status"] or "已满" in r["status"])
    fail = len(results) - ok - skip
    print(f"\n{'=' * 42}\n📊 汇总：{ok} 成功 / {skip} 跳过 / {fail} 失败，共 {len(results)} 台\n{'=' * 42}")
    if fail:
        sys.exit(2)


if __name__ == "__main__":
    main()
