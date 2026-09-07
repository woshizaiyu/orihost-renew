# Orihost 免费服务器自动续期

基于 Jexactyl 面板 API 的自动续期脚本，解决免费容器 7 天过期删机问题。
核心：`remember_web` 长效 token 自动置换 session + XSRF，不用频繁手动更新 Cookie。

## 文件结构

```text
orihost-renew/
├── orihost_renew.py            # 核心脚本（鉴权维持 + 批量续期 + TG 推送）
├── requirements.txt            # 依赖：curl_cffi + requests
├── .github/workflows/renew.yml# GitHub Actions 定时任务（每 3 天 + 手动触发）
└── README.md                   # 本说明文件
```

## 续期原理

1. 携带 `remember_web_xxx` 长效 token 访问 `https://panel.orihost.com/dashboard`，服务端签发最新 `session + XSRF-TOKEN`
2. `POST /api/client/servers/{uuid}/renew/begin` 获取广告链接 + 等待秒数 `dwell_seconds`
3. 访问广告并等待 `dwell_seconds + 2~5s`（模拟阅读）
4. `GET /api/client/renewal/complete` 领取续期天数
5. 遇 419/401 自动刷新 XSRF 重试一次；`cooldown` 超过 5 分钟本轮跳过，`skipped` 表示已达本周期上限

## 一、获取 remember token

1. 浏览器登录 `https://panel.orihost.com`
2. 按 `F12` → `Application（应用）` → `Cookies` → `https://panel.orihost.com`
3. 找到 `remember_web_xxxx` 那一项，复制它的 `值`（很长一串字符）
4. 只填这个值即可（推荐）。也兼容填 `name=value` 或整段 Cookie 字符串，脚本会自动识别

备选方法：`F12` → `Network（网络）` → 刷新页面 → 点任意 `activity` 请求 → `Request Headers` 里复制 `Cookie` 整段。

## 二、获取服务器 UUID

进面板点开你的服务器，看浏览器地址栏：

```text
https://panel.orihost.com/server/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
                                  └──────────── UUID，完整复制 ────────────┘
```

多台服务器用英文逗号分隔：`uuid1,uuid2`。

## 三、GitHub Actions 部署（推荐）

1. 新建仓库，把本目录文件推上去（保持 `orihost_renew.py` 在仓库根目录）
2. 仓库 `Settings → Secrets and variables → Actions` 按下表配置：

| 名称 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `ORIHOST_REMEMBER` | Secret | 是 | 第一步拿到的 remember token 值 |
| `ORIHOST_SERVER_IDS` | Variables | 是 | 服务器 UUID，逗号分隔 |
| `TG_BOT_TOKEN` | Secret | 否 | Telegram 机器人 token |
| `TG_CHAT_ID` | Secret | 否 | Telegram 聊天 ID |
| `ORIHOST_PROXY` | Secret | 否 | 代理，如 `http://127.0.0.1:1081`，解决 CI 的 IP 被 CF 拦时用 |

3. 去 `Actions → Orihost Auto Renew → Run workflow` 手动跑一次，TG 能收到推送即正常
4. 定时默认 `0 10 */3 * *`（每 3 天，北京时间 18:00），7 天有效期提前续是故意的，不要改成 7 天

### 多账号

| 账号 | Secret | Variables |
|---|---|---|
| 账号1 | `ORIHOST_REMEMBER_1` | `ORIHOST_SERVER_IDS_1` |
| 账号2 | `ORIHOST_REMEMBER_2` | `ORIHOST_SERVER_IDS_2` |
| 账号3 | `ORIHOST_REMEMBER_3` | `ORIHOST_SERVER_IDS_3` |

单账号用不带后缀的即可；多账号与单账号可混用，脚本会自动汇总。
旧变量名 `ORIHOST_COOKIE / ORIHOST_COOKIE_1 / ORI_COOKIE` 仍兼容（完整 Cookie 或裸 token 均可）。

## 四、本地运行（Windows）

```bat
pip install -r requirements.txt
set ORIHOST_REMEMBER=你的remember值
set ORIHOST_SERVER_IDS=你的服务器UUID
python orihost_renew.py
```

多台 / TG / 代理（cmd 示例）：

```bat
set ORIHOST_SERVER_IDS=uuid1,uuid2
set TG_BOT_TOKEN=123:abc
set TG_CHAT_ID=123456789
set ORIHOST_PROXY=http://127.0.0.1:7890
python orihost_renew.py
```

## 五、环境变量全表

| 变量 | 默认 | 说明 |
|---|---|---|
| `RENEWAL_MAX` | `21` | 单台续期次数上限，满了自动跳过 |
| `MAX_ATTEMPTS` | `5` | 单台单轮最多 begin/complete 次数 |
| `DWELL_EXTRA` | `2` | 阅读等待额外加秒（防风控） |
| `TG_BOT` | 空 | 兼容写法 `chat_id,token`，与 `TG_BOT_TOKEN/TG_CHAT_ID` 二选一 |
| `ORIHOST_GOST_PROXY` | 空 | 与 `ORIHOST_PROXY` 同效 |

## 六、常见问题

- **419 / session 刷新失败**：`remember_web` 已失效，重新登录按第一步重取 token 更新到 Secrets
- **401 未认证**：同上，多为 token 填错（多了空格或只复制了一半）
- **skipped / 已达上限**：正常现象，本周期续满了，下个周期 Actions 会再续
- **冷却中 xxxs 本轮跳过**：面板限流，超过 5 分钟脚本主动放弃，等 3 天后下一轮
- **TG 收不到**：先确认 `TG_BOT_TOKEN` 与 `TG_CHAT_ID` 都填了，且机器人已和你开过会话（先给机器人发一句话）
- **被 Cloudflare 拦截**：加 `ORIHOST_PROXY` 走代理，或换个时间手动重跑
- **汇总 0 成功 0 跳过 N 失败**：看日志第一行，`curl_cffi=关` 表示依赖没装好，重跑 Install 步骤

## 安全提醒

- token 等同于登录态，只放 GitHub Secrets，不要提交到代码里
- 本仓库为公开仓库，切勿把 token / UUID 写进代码、README 或 Actions 日志里
