# 2dfan 自动签到

使用 [Selenium](https://www.selenium.dev/) + [undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver) 自动化 Chrome 完成 2dfan.com 签到，自动处理 Cloudflare 挑战，以及 Turnstile / 阿里云滑块人机验证。支持多账号、GitHub Actions 定时运行、Telegram 结果推送。

> **为什么不用 headless 浏览器？** Cloudflare 对 headless Chrome（包括无头模式下的各类 stealth 补丁）检测非常严格。undetected-chromedriver 通过补丁 chromedriver 消除自动化特征，配合**有头模式**运行通过率最高。因此云端使用 [Xvfb](https://en.wikipedia.org/wiki/Xvfb) 虚拟显示器让 Chrome 以有头模式运行，而不是开启 headless。

## 本地使用

### 1. 安装依赖

```bash
uv sync
```

本地运行需安装 [Chrome](https://www.google.com/chrome/)（undetected-chromedriver 会自动下载并补丁匹配版本的 chromedriver，首次运行需联网）。

### 2. 配置

复制 `.env.example` 为 `.env`：

```env
# 账号配置，支持多账号（JSON 数组）
ACCOUNTS=[{"user_id":"123","session":"xxx"},{"user_id":"456","session":"yyy"}]

# Telegram 推送（可选，多个 chat_id 用英文逗号分隔）
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=123456789

# 无头模式（可选，默认有头；仅本机调试时使用）
HEADLESS=false
```

**账号信息获取：**
- `user_id` — 2dfan.com 个人主页 URL 中的数字
- `session` — 浏览器 Cookie 中 `_project_hgc_session` 的值

### 3. 运行

```bash
uv run python main.py
```

单个账号失败不影响其他账号；任一账号失败时程序以退出码 1 结束。

## Telegram 推送配置（可选）

1. 在 Telegram 找 [@BotFather](https://t.me/BotFather)，发送 `/newbot` 创建机器人，获得 **bot token**。
2. 向你的机器人先发一条消息（机器人需要被主动对话后才能回复）。
3. 获取 **chat_id**：找 [@userinfobot](https://t.me/userinfobot) 发送任意消息即可得到；频道则填 `@频道用户名`，并把机器人拉为频道管理员。
4. 本地填入 `.env`，或在 GitHub Secrets 中配置（见下）。需要推送给多人/多群时，`TELEGRAM_CHAT_ID` 用英文逗号分隔。

## GitHub Actions 部署

1. 将本目录推送到 GitHub 仓库（需包含 `.github/workflows/checkin.yml`）：

   ```bash
   cd script
   git init
   git add .
   git commit -m "init: 2dfan 自动签到"
   git branch -M main
   git remote add origin https://github.com/<你的用户名>/<仓库名>.git
   git push -u origin main
   ```

2. 在仓库 **Settings → Secrets and variables → Actions → New repository secret** 添加：

   | Secret 名 | 值 | 必填 |
   |---|---|---|
   | `ACCOUNTS` | 与本地相同的账号 JSON 数组 | 是 |
   | `TELEGRAM_BOT_TOKEN` | Telegram 机器人 token | 否 |
   | `TELEGRAM_CHAT_ID` | 接收消息的 chat_id（多个逗号分隔） | 否 |

3. 工作流默认每天 **北京时间 09:10** 自动运行（对应 UTC 1:10），也可在 **Actions → 2dfan 自动签到 → Run workflow** 手动触发。工作流通过 `xvfb-run` 启动有头 Chrome；签到失败时会自动上传 `debug_*.png/.html` 快照为 Artifact，便于排查。

> 提示：Cloudflare 的判定与 IP 信誉强相关，GitHub 机房 IP 触发人机验证的概率高于家庭宽带。若云端验证仍频繁失败，可调整 workflow 中的 cron 多跑一次，或改在本机/家用服务器定时运行。

## 免责声明

本项目仅供学习和个人使用，使用者需自行承担一切风险。作者不对因使用本工具而导致的任何损失或账号问题负责，也不保证工具的持续可用性。使用本项目即表示你同意遵守 2dfan.com 的服务条款。

## License

[MIT](LICENSE)
