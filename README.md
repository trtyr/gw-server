# gw-server

Google Workspace 的 **MCP 服务器**：把 Gmail / Drive / Calendar / Docs / Sheets / Slides /
Chat / Tasks / Contacts 做成可发现的 MCP 工具，pi（或任何 MCP 客户端）直接挂上就用。

```text
pi (mcp-adapter) ──MCP Streamable HTTP + Bearer──> https://gw.trtyr.top (EdgeOne)
                                                      └回源--> sg: gw-server (systemd :80)
                                                                  |-- ~/.gw-server/ (tokens + OAuth client)
                                                                  `--> *.googleapis.com
```

- 生产：sg 43.163.80.102，systemd 常驻 `gw-server.service`，监听 :80，EdgeOne 回源 HTTP→用户侧 HTTPS
- 数据目录：`~/.gw-server/`（tokens/ + credentials.json），与任何其他工具的命名空间解耦
- OAuth：自建 client（`~/.gw-server/credentials.json`，标准授权码流程，refresh 走自己
  的 client_secret）；未配置时回退 legacy cloud 模式（geminicli 公共扩展，仅限旧 13 scopes）

## MCP 工具面（45 个）

| 分组 | 工具 |
| --- | --- |
| 核心 (3) | accounts_list / whoami / gapi（任意 Google REST 逃生舱） |
| Gmail (10) | search / read / send / reply / draft_create / draft_list / draft_send / modify_labels / labels_list / attachment_get |
| Drive (10) | search / list / read（Docs 自动导出）/ download / upload / update / create_folder / move / trash / share |
| Calendar (6) | events / create / update / delete / calendars_list / freebusy |
| Docs (3) | read / create / append |
| Sheets (4) | read / list / write / append |
| Slides (1) | read |
| Chat (3) | spaces_list / messages_list / send |
| Tasks (4) | lists / list / add / complete |
| People (1) | search |

多账号：`email` 参数可省略——单账号自动选定。

## pi 接入

`~/.pi/agent/mcp-adapter.json`：

```json
{
  "mcpServers": {
    "gw": {
      "url": "https://gw.trtyr.top/mcp",
      "auth": "bearer",
      "bearerToken": "<GW_TOKEN>",
      "lifecycle": "keep-alive"
    }
  }
}
```

生产 Bearer 存 engram credentials 域（name=`gw_api_token`）。

## 运行

```bash
export GW_TOKEN=$(openssl rand -hex 32)   # 必填，缺失启动即退出
export GW_HOST=0.0.0.0 GW_PORT=80         # 本地调试默认 127.0.0.1:8787
export GW_PUBLIC_URL=https://gw.trtyr.top # OAuth 回调基址（无头服务器必配）
uv run gw-server
```

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `GW_TOKEN` | （必填） | 服务 Bearer |
| `GW_HOST` / `GW_PORT` | `127.0.0.1` / `8787` | 监听地址（生产 0.0.0.0:80 + systemd AmbientCapabilities） |
| `GW_CONFIG_DIR` | `~/.gw-server` | 数据根目录 |
| `GW_TOKENS_DIR` | `<config>/tokens` | OAuth token 存储 |
| `GW_CREDENTIALS` | `<config>/credentials.json` | 自建 OAuth client（`installed` 格式） |
| `GW_AUTH_MODE` | 自动 | 强制 `local`/`cloud` |
| `GW_PUBLIC_URL` | `http://localhost:<port>` | OAuth 回调基址 |

## 登录（服务端化，无浏览器服务器可用）

```bash
# pi 或 curl 发起（Bearer）：
GET /oauth/login?email=user@gmail.com
# → 返回授权链接，在任意设备浏览器打开、同意授权
# → Google 回调 {GW_PUBLIC_URL}/oauth/callback，token 直接落盘服务器
```

Mac 本地亦可用 CLI：`uv run python -m gw_server.login --email user@gmail.com`（localhost 回调）。

注意：cloud 模式（公共 client）的 scope 变更会触发 Google「此应用已被阻止」——
敏感 scope 组合一律走自建 client。

## 调试端点（HTTP，非 MCP）

- `GET /healthz`（免认证）
- `GET /accounts`（Bearer）
- `POST /exec`（Bearer）— REST 代理调试口

## 部署（sg）

```bash
rsync -az --exclude .venv --exclude .git -e "ssh -p 2222" ./ tencent-sg:/opt/gw-server/
ssh -p 2222 tencent-sg 'cd /opt/gw-server && ~/.local/bin/uv sync'
scp -P 2222 -r ~/.gw-server tencent-sg:/root/
# systemd unit: /etc/systemd/system/gw-server.service（GW_TOKEN=credentials域 gw_api_token）
systemctl enable --now gw-server
```

安全组：TCP 80（EdgeOne 回源）+ TCP 2222（SSH）。
