# Orihost 免费服务器自动续期

基于 Jexactyl 面板 API 的自动续期脚本，解决免费容器 7 天过期删机问题。
核心：`remember_web` 长效 token 自动置换 session + XSRF，不用频繁手动更新 Cookie。

## 文件结构

```text
orihost-renew/
├── orihost_browser_renew.py    # 主力：浏览器自动续期（Cookie免登+读文章+Turnstile+Claim）
├── orihost_renew.py            # 备用：纯 API 版（仅满额检测/诊断，claim 通不过验证）
├── requirements.txt            # 依赖：curl_cffi + requests + seleniumbase
├── .github/workflows/renew.yml# GitHub Actions 定时任务（每 3 天 + 手动触发）
└── README.md                   # 本说明文件
```

## 续期原理

面板前端扒出来的真实流程（`assets/bundle.*.js`）：

1. 点 `Renew Now` → `POST /api/client/servers/{id}/renew/begin` 返回文章链接 + `dwell_seconds`
2. 点 `Read Article` 新标签读文章（提前关闭会被警告），面板内倒计时
3. 倒计时走完出 Cloudflare Turnstile，必须点过验证，`Claim Renewal` 按钮才可点
4. 点 `Claim Renewal` → `GET /api/client/renewal/complete?cf-turnstile-response=xxx` 完成续期（+7 天）

结论：`complete` 强制要 Turnstile token，无 token 直接 500，所以主力跑**浏览器版**（真浏览器点验证，移植自 katabump 的过盾方案）；纯 API 版保留作满额检测和诊断用。

## 一、获取 remember token（填的是令牌，不是邮箱密码）

> 脚本不需要你的邮箱和密码，只需要登录态令牌。令牌失效了重新取一次即可，密码改了也不受影响。

1. 浏览器打开 `https://panel.orihost.com` 并登录（登录页如果有 `Remember me` 勾上）
2. 按 `F12` 打开开发者工具 → 顶部切到 `Application（Edge 显示“应用程序”）` → 左侧展开 `Cookies` → 点 `https://panel.orihost.com`
3. 右边列表里找到名字以 `remember_web_` 开头的那一行（后面跟一串 hash，如 `remember_web_59ba36...`）
4. 双击它的 `Value（值）` 那一格，全选复制（一长串无空格字符，几百个字符长度）。这就是要填的 `ORIHOST_REMEMBER`
5. 填的时候注意：只粘贴纯值，前后不要带空格、不要带引号、不要带 `remember_web_xxx=` 前缀（带了也能用，但纯值最稳）

格式长这样（已脱敏，只看形状，别照抄）：
- token：`eyJpdiI6...中间几百字符...In0=`，字母数字+符号组成，一整行无空格无换行
- 对错自查：长度几百字符、以 `eyJ` 开头是正常的；如果只有几十字符，你大概率复制的是别的 cookie，重找 `remember_web_` 开头那行

备选方法：`F12` → `Network（网络）` → 刷新页面 → 点任意 `activity` 请求 → `Request Headers` 里复制 `Cookie` 整段（脚本会自动从里面提取）。

## 二、获取服务器 ID

进面板点开你的服务器，看浏览器地址栏：

```text
https://panel.orihost.com/server/8651e616
                                  └─ 8 位短 ID，填这个就行 ─┘
```

多台用英文逗号分隔：`id1,id2`。
填完整 UUID（`670475f5-1206-...` 形如 8-4-4-4-12）也兼容，脚本会自动取前 8 位。

## 三、GitHub Actions 部署（推荐）

1. 新建仓库，把本目录文件推上去（保持 `orihost_renew.py` 在仓库根目录）
2. 进仓库 `Settings → Secrets and variables → Actions`，点 `Secrets` 页签 → `New repository secret`，按下表逐个建（保存后值不可见是正常的）：

   名字必须一字不差（大写+下划线），所有变量全部建在 `Secrets` 下。完整对照表：

| 名称 | 必填 | 说明 |
|---|---|---|
| `ORIHOST_REMEMBER` | 是 | 第一步拿到的 remember token 值 |
| `ORIHOST_SERVER_IDS` | 是 | 服务器短 ID（地址栏 `/server/` 后面那段），逗号分隔 |
| `TG_BOT_TOKEN` | 否 | Telegram 机器人 token |
| `TG_CHAT_ID` | 否 | Telegram 聊天 ID |
| `NODE_LINK` | 否 | 代理节点完整分享链接（vless/vmess/trojan/hysteria2/tuic/anytls/socks5），不填则直连 |
| `ORIHOST_PROXY` | 否 | 手动指定的 http(s)/socks 代理，如 `http://127.0.0.1:1081`；节点链接填 `NODE_LINK`，不要填这里 |

代理说明：直连优先。`NODE_LINK` 由工作流的 sing-box 步骤自动转成本地代理（`vless://` 这类链接只能填这里）；`ORIHOST_PROXY` 只接受 `http://` / `socks5://` 开头的代理地址。

3. 去 `Actions → Orihost Auto Renew → Run workflow` 手动跑一次，TG 能收到推送即正常
4. 定时默认 `0 10 */3 * *`（每 3 天，北京时间 18:00），7 天有效期提前续是故意的，不要改成 7 天

### 多账号

| 账号 | 在 Secrets 里建这两个 |
|---|---|
| 账号1 | `ORIHOST_REMEMBER_1` + `ORIHOST_SERVER_IDS_1` |
| 账号2 | `ORIHOST_REMEMBER_2` + `ORIHOST_SERVER_IDS_2` |
| 账号3 | `ORIHOST_REMEMBER_3` + `ORIHOST_SERVER_IDS_3` |

单账号用不带后缀的即可；多账号与单账号可混用，脚本会自动汇总。
旧变量名 `ORIHOST_COOKIE / ORIHOST_COOKIE_1 / ORI_COOKIE` 仍兼容（完整 Cookie 或裸 token 均可）。

## 四、本地运行（Windows）

```bat
pip install -r requirements.txt
set ORIHOST_REMEMBER=你的remember值
set ORIHOST_SERVER_IDS=你的服务器短ID
python orihost_renew.py
```

多台 / TG / 代理（cmd 示例）：

```bat
set ORIHOST_SERVER_IDS=id1,id2
set TG_BOT_TOKEN=123:abc
set TG_CHAT_ID=123456789
set ORIHOST_PROXY=http://127.0.0.1:7890
python orihost_renew.py
```

本地没有 sing-box 步骤，`NODE_LINK` 只在 Actions 里生效；本地要走代理请填 `ORIHOST_PROXY`（需是本机能连上的 http/socks 代理）。

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
- **面板显示 Renew Limit Reached / complete 报 500**：续期次数已满（免费服常见上限 7 次），脚本会自动判为跳过；等天数消耗、空出次数后定时任务会自动再续，不用管
- **冷却中 xxxs 本轮跳过**：面板限流，超过 5 分钟脚本主动放弃，等 3 天后下一轮
- **TG 收不到**：先确认 `TG_BOT_TOKEN` 与 `TG_CHAT_ID` 都填了，且机器人已和你开过会话（先给机器人发一句话）
- **被 Cloudflare 拦截**：把节点链接填到 `NODE_LINK` 走代理，或换个时间手动重跑
- **汇总 0 成功 0 跳过 N 失败**：看日志第一行，`curl_cffi=关` 表示依赖没装好，重跑 Install 步骤

## 安全提醒

- token 等同于登录态，只放 GitHub Secrets，不要提交到代码里
- 本仓库为公开仓库，切勿把 token / UUID 写进代码、README 或 Actions 日志里
