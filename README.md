# gw-server

Google Workspace 网关服务：把 google-workspace 技能收敛为常驻 HTTP 服务，客户端只需
Bearer token + endpoint，不再直连 Google（解决无代理环境不可用问题）。

架构（目标）：

```text
pi (Mac/北京/手机) --https--> gw.trtyr.top (Caddy TLS) --> gw-server (sg, 本服务)
```

## 运行

```bash
export GW_TOKEN=$(openssl rand -hex 32)   # 客户端 Bearer，必填
export GW_PORT=8787                        # 默认 127.0.0.1:8787
uv run gw-server
```

其他环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `GW_TOKEN` | （必填） | 服务 Bearer，缺失启动即退出 |
| `GW_HOST` / `GW_PORT` | `127.0.0.1` / `8787` | 监听地址（生产前置 Caddy） |
| `GOOGLE_WORKSPACE_TOKENS_DIR` | `~/.pi/google-workspace/tokens` | 与 node 技能共用 token |
| `GOOGLE_WORKSPACE_CREDENTIALS` | `~/.pi/google-workspace/credentials.json` | local 模式刷新所需 |
| `GW_AUTH_MODE` | 自动 | 强制 `local`/`cloud`（默认按 token 文件 `__authMode`） |
| `GW_CLOUD_FN_URL` | geminicli 公共扩展 | cloud 模式 refresh 端点 |

## 接口

### GET /healthz（免认证）

```json
{"ok": true, "service": "gw-server", "version": "0.1.0", "accounts": 1}
```

### GET /accounts（Bearer）

列出已知账号（不含 token 值）：`email / authMode / scopes / expiryDate / expired`。

### POST /exec（Bearer）

```json
{
  "email": "user@gmail.com",
  "service": "gmail",
  "path": "/users/me/messages",
  "params": {"maxResults": 5},
  "method": "GET"
}
```

- `service+path` 便捷模式：服务端按 Google REST 规则拼 URL
  （gmail/drive/calendar/docs/sheets/slides/people/chat/tasks/oauth2/admin，`version` 可覆盖）
- `url` 完整模式：直接给 `https://*.googleapis.com/...`，与 service/path 互斥
- `body`：JSON 请求体（POST/PATCH）；`headers`：附加头（Authorization/Cookie 等被丢弃）
- `timeout`：秒，默认 30，上限 300

响应（信封式，Google 侧错误 HTTP 仍为 200，业务结果看 `ok`/`status`）：

```json
{"ok": true, "email": "...", "status": 200, "data": {...}, "elapsedMs": 123}
```

认证层失败：无/错 Bearer → HTTP 401。

## 快速自测

```bash
curl -s localhost:8787/healthz
curl -s -o /dev/null -w '%{http_code}\n' localhost:8787/accounts          # 401
curl -s -H "Authorization: Bearer $GW_TOKEN" localhost:8787/accounts
curl -s -H "Authorization: Bearer $GW_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"email":"user@gmail.com","service":"oauth2","path":"/userinfo"}' \
  localhost:8787/exec
```
