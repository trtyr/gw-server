# gw-server

Google Workspace 的 **MCP 服务器**：把 Gmail / Drive / Calendar / Docs / Chat 等 Google
能力做成可发现的 MCP 工具，pi（或任何 MCP 客户端）直接挂上就用，不再需要
googleapis / node CLI / REST 细节。

```text
pi (mcp-adapter) ──MCP Streamable HTTP + Bearer──> gw-server /mcp ──> *.googleapis.com
                                                       |-- OAuth tokens (自动刷新)
                                                       `-- login.py (纯 Python 登录)
```

## MCP 工具面

| 工具 | 说明 |
| --- | --- |
| `accounts_list` | 列出已登录账号（无敏感值） |
| `whoami` | 账号身份（email/姓名） |
| `gmail_search` | Gmail 搜索（query 为 Gmail 语法） |
| `gmail_read` | 读单封邮件（含正文提取） |
| `gmail_send` | 发送纯文本邮件 |
| `drive_search` | Drive 文件搜索 |
| `calendar_events` | 主日历日程（time_min/time_max 可选，默认未来 7 天） |
| `gapi` | 通用逃生舱：调任意 Google Workspace REST 端点 |

多账号模型：`email` 参数可省略——只有一个账号时自动选定，多个账号时提示可选列表。

## pi 接入

`~/.pi/agent/mcp-adapter.json`：

```json
{
  "mcpServers": {
    "gw": {
      "url": "https://gw.trtyr.top/mcp",
      "auth": "bearer",
      "bearerToken": "<GW_TOKEN>"
    }
  }
}
```

## 运行

```bash
export GW_TOKEN=$(openssl rand -hex 32)   # 客户端 Bearer，必填（缺失启动即退出）
export GW_HOST=127.0.0.1 GW_PORT=8787     # 生产由 Caddy 反代
uv run gw-server
```

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `GW_TOKEN` | （必填） | 服务 Bearer |
| `GW_HOST` / `GW_PORT` | `127.0.0.1` / `8787` | 监听地址 |
| `GOOGLE_WORKSPACE_TOKENS_DIR` | `~/.pi/google-workspace/tokens` | token 存储（与旧 node 技能同格式） |
| `GOOGLE_WORKSPACE_CREDENTIALS` | `~/.pi/google-workspace/credentials.json` | local 模式刷新所需 |
| `GW_AUTH_MODE` | 自动 | 强制 `local`/`cloud`（默认按 token `__authMode`） |
| `GW_CLOUD_FN_URL` | geminicli 公共扩展 | cloud 模式 refresh 端点 |

## 登录（纯 Python，node 退役）

在**有浏览器的机器**上执行一次：

```bash
uv run python -m gw_server.login --email user@gmail.com
```

浏览器完成 Google 授权后 token 自动落盘（含身份校验：登录账号必须与 `--email`
一致）。token 文件与旧 node 技能 100% 兼容，可直接拷贝到服务器（scp 即迁移）。

## 调试端点（HTTP，非 MCP）

- `GET /healthz`（免认证）— 存活探针
- `GET /accounts`（Bearer）— 账号列表
- `POST /exec`（Bearer）— REST 代理调试口（`email` + `service`/`path` 或 `url`）

## Roadmap

- [ ] sg 部署 + Caddy TLS + systemd 常驻
- [ ] 自建 Google OAuth client → 服务端登录闭环（浏览器回调直接落盘服务器，
      现需在有浏览器的机器登录后拷贝 token）
