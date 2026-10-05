## Orihost 自动续期（单服版）

使用 GitHub Actions 自动给 Orihost 免费服务器续期（Jexactyl 面板，`panel.orihost.com/server/8651e616`）。
以 Hiden 骨架为基座自研，续期芯按本地录制（bundle.json + trace 抓包）重写，不依赖任何线上版本。

## 续期原理

面板真实流程（bundle.json 12:59 录制 + trace 抓包实测）：

1. 进服务器页 → 如有屏幕中央广告，先点 `要關閉` 关掉（右侧悬浮广告不影响，直接忽略）
2. 点 `Renew` → 弹出续期对话框（同时调 `GET .../renew/cooldown` 查冷却，`{"seconds":0}` 才可续）
3. 点 `Read Article` → 新标签打开文章（`POST .../renew/begin` 返回 `{url, dwell_seconds:15}`），模拟阅读后关闭
4. 回面板等倒计时走完（`Thanks for reading`）→ 过 Cloudflare Turnstile → 点 `Claim Renewal`（`GET /api/client/renewal/complete?cf-turnstile-response=`）完成续期（+7 天）

## 配置

在仓库 `Settings → Secrets and variables → Actions` 中添加以下 Secrets：

| Secret 名称 | 是否必填 | 说明 |
|---|---|---|
| `ORIHOST_REMEMBER` | ✅必填 | `remember_web_59ba36...` cookie 的值（纯 token，不含 `=` 前缀） |
| `ORIHOST_SERVER_IDS` | ❌可选 | 服务器 ID，默认 `8651e616` 单服，可不填 |
| `NODE_LINK` | ❌可选 | 代理节点地址，例如：vless:// vmess:// trojan:// hysteria2:// anytls://（不配置则直连） |
| `TG_BOT_TOKEN` | ❌可选 | Telegram Bot Token |
| `TG_CHAT_ID` | ❌可选 | Telegram Chat ID |

`ORIHOST_REMEMBER` 的获取（登录面板后 F12 → 应用程序/存储 → Cookies → `https://panel.orihost.com` → 找到以 `remember_web_` 开头的那行 → 复制它的值）：

- 正常形状：以 `eyJ` 开头、几百字符、无空格无换行的一整行；
- 如果只有几十字符，大概率复制错了行，重找 `remember_web_` 开头那行。

`NODE_LINK` 支持的代理协议与 Hiden 版一致（VLESS / VMess / Trojan / tuic / anytls / hysteria2 / SOCKS5），由工作流的 sing-box 步骤自动转为本地代理。

## 使用

### GitHub Actions 运行步骤

1. 把本目录文件推到你的仓库（保持 `app.py` 在仓库根目录）
2. 在仓库 Secrets 中配置 `ORIHOST_REMEMBER`（可选配 `TG_BOT_TOKEN`、`TG_CHAT_ID`、`NODE_LINK`）
3. Actions 菜单里手动触发 `workflow_dispatch` 跑一次，TG 能收到推送即正常
4. 定时默认每 3 天一次（`0 10 */3 * *`，北京时间 18:00），7 天有效期提前续是故意的，不要改成 7 天

### 本地运行（Windows）

```bat
pip install playwright requests requests[socks]
python -m playwright install chrome
set ORIHOST_REMEMBER=你的remember值
python app.py
```

本地走代理：`set ORIHOST_PROXY=http://127.0.0.1:7890`（`NODE_LINK` 只在 Actions 里生效）。

## 常见问题

- **登录失败（被踢回登录页）**：`remember_web` 已失效，重新登录按上一步重取 token 更新到 Secrets；
- **冷却中本轮跳过**：`cooldown` 接口返回 `seconds>0`，等 3 天后下一轮；
- **续期次数已达上限**：面板显示 Renew Limit Reached，脚本判跳过，等天数消耗后定时任务会自动再续；
- **Claim 超时**：多为 Turnstile 未通过或广告重新弹出挡住按钮，下载 Actions 的 `orihost-shots` 截图确认后手动重跑一次；
- **TG 收不到**：确认 `TG_BOT_TOKEN` 与 `TG_CHAT_ID` 都填了，且机器人已和你开过会话（先给机器人发一句话）。

---

**⚠️ 免责声明**：本脚本仅供学习交流使用，使用者需遵守 [Orihost](https://orihost.com) 的服务条款。因使用本脚本造成的任何问题，作者不承担任何责任。
