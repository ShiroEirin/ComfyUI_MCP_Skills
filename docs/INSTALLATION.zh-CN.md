# 安装与配置

本文面向首次部署 ComfyUI MCP Skills 的用户，覆盖本地 stdio、Snow/Claude Code 兼容模式、权限 Toolset 和 Streamable HTTP。

## 1. 前置条件

- Python 3.10+；CI 当前验证 3.10–3.13，更高版本尚未纳入支持矩阵。
- 可访问的 ComfyUI 实例。
- Git；源码开发推荐安装 [uv](https://docs.astral.sh/uv/)。
- 一个独立的数据目录，用于保存 `config.json`、工作流、控制平面数据库和上传文件。

ComfyUI MCP Skills 不是 ComfyUI 自定义节点，不需要复制到 `custom_nodes`。

## 2. 安装

### 2.1 从 GitHub 安装

当前 PyPI 尚未发布该包。普通使用可直接安装 GitHub `main`：

```bash
python -m pip install "git+https://github.com/ShiroEirin/ComfyUI_MCP_Skills.git@main"
```

确认包版本和入口已安装：

```bash
python -c "import comfyui_mcp_skills; print(comfyui_mcp_skills.__version__)"
```

`comfyui-mcp` 是纯 stdio MCP 进程，不提供 `--help` 交互参数。它会立即初始化 `COMFYUI_MCP_DIR` 中的控制平面并等待协议输入，因此应由 MCP Host 启动，不要把直接运行或 `--help` 当作安装检查。

### 2.2 源码开发安装

```bash
git clone https://github.com/ShiroEirin/ComfyUI_MCP_Skills.git
cd ComfyUI_MCP_Skills
uv sync --locked --extra dev
```

之后通过 `uv run` 使用入口：

```bash
uv run comfyui-mcp
```

## 3. 创建数据目录

建议将服务源码与运行数据分开：

```text
D:/comfyui-mcp-workspace/
├── config.json
├── data/
└── uploads/
```

创建 `config.json`：

```json
{
  "default_server": "local",
  "servers": [
    {
      "id": "local",
      "name": "Local ComfyUI",
      "url": "http://127.0.0.1:8188",
      "enabled": true
    }
  ]
}
```

字段说明：

| 字段 | 要求 |
|---|---|
| `id` | 稳定服务器标识，只使用 ASCII 字母、数字、下划线和连字符 |
| `name` | 展示名称 |
| `url` | ComfyUI HTTP Origin，不包含凭据 |
| `enabled` | 是否参与发现和执行 |
| `default_server` | 未显式指定服务器时使用的默认 ID |

工作流目录结构：

```text
data/
└── local/
    └── txt2img/
        ├── schema.json
        └── workflow.json
```

- `workflow.json`：ComfyUI API 格式工作流，不是浏览器导出的 UI workflow。
- `schema.json`：Agent 可见的参数 schema，以及参数到节点输入的绑定。

已有 `comfyui-skill` 项目可直接复用现有 `config.json` 和 `data/`。

## 4. 本地 stdio 配置

### 4.1 已安装命令

```json
{
  "mcpServers": {
    "comfyui": {
      "command": "comfyui-mcp",
      "env": {
        "COMFYUI_MCP_DIR": "D:/comfyui-mcp-workspace"
      }
    }
  }
}
```

### 4.2 从源码运行

```json
{
  "mcpServers": {
    "comfyui": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "D:/github/ComfyUI_MCP_Skills",
        "comfyui-mcp"
      ],
      "cwd": "D:/github/ComfyUI_MCP_Skills",
      "env": {
        "COMFYUI_MCP_DIR": "D:/comfyui-mcp-workspace"
      }
    }
  }
}
```

`COMFYUI_MCP_DIR` 是服务数据目录，不是源码目录，也不是 ComfyUI 安装目录。

## 5. Snow 与 Claude Code 兼容模式

OMP 可直接使用 canonical MCP 工具名，例如：

```text
comfyui.job.get
comfyui.run.local.txt2img
```

部分 Snow、Claude Code 或 OpenAI/Anthropic 兼容网关只允许工具名匹配：

```text
[A-Za-z0-9_-]+
```

如果模型 API 返回 `Invalid tools[n].name`，添加：

```json
{
  "env": {
    "COMFYUI_MCP_DIR": "D:/comfyui-mcp-workspace",
    "COMFYUI_MCP_PORTABLE_TOOL_NAMES": "1"
  }
}
```

服务将暴露：

```text
comfyui_job_get
comfyui_run_local_txt2img
```

这只改变 MCP Host 看见的名称。授权、能力搜索和内部 canonical 分发保持一致。投影发生碰撞时，服务拒绝列出工具，不会静默调用错误工作流。

Snow 配置还需要把服务器设为启用状态：

```json
"enabled": true
```

## 6. stdio 权限与 Toolset

不配置权限变量时，stdio 使用安全默认值：

```text
COMFYUI_MCP_PRINCIPAL_ID=local-stdio
COMFYUI_MCP_TOOLSET=execution
COMFYUI_MCP_SCOPES=comfyui:execute
```

默认值由服务内部提供，无需写入配置。

可选 Toolset：

| Toolset | 可用 scopes | 主要用途 |
|---|---|---|
| `execution` | `comfyui:execute` | 工作流执行、Job、Asset、Experiment、路由 |
| `authoring` | `comfyui:observe,comfyui:author` | 工作流理解、Revision 和依赖检查 |
| `operations` | `comfyui:observe,comfyui:operate` | Server、Queue、Log 和运行时控制 |
| `admin` | `comfyui:configure,comfyui:provision,comfyui:audit` | 独立管理面 |

显式配置必须同时提供主体、Toolset 和 scopes：

```json
{
  "env": {
    "COMFYUI_MCP_DIR": "D:/comfyui-mcp-workspace",
    "COMFYUI_MCP_PRINCIPAL_ID": "local-operator",
    "COMFYUI_MCP_TOOLSET": "operations",
    "COMFYUI_MCP_SCOPES": "comfyui:observe,comfyui:operate",
    "COMFYUI_MCP_ENABLE_HIGH_RISK": "1"
  }
}
```

除 `execution` 外的 stdio Toolset 必须设置 `COMFYUI_MCP_ENABLE_HIGH_RISK=1`。

## 7. 本地资产上传

stdio 默认只允许上传：

```text
<COMFYUI_MCP_DIR>/uploads
```

扩展授权根目录：

Windows PowerShell：

```powershell
$env:COMFYUI_MCP_UPLOAD_ROOTS = "D:/media;E:/shared-assets"
```

Linux/macOS：

```bash
export COMFYUI_MCP_UPLOAD_ROOTS="/srv/media:/mnt/shared-assets"
```

分隔符使用操作系统的 `PATH` 分隔符。裸文件名和 `clipspace/...` 视为 ComfyUI 服务端引用，不会被当前工作目录中的同名文件替换。

## 8. 独立 Admin 管理面

Admin 不与普通执行工具混合。启动前必须显式开启：

Windows PowerShell：

```powershell
$env:COMFYUI_MCP_ENABLE_ADMIN = "1"
$env:COMFYUI_MCP_DIR = "D:/comfyui-mcp-workspace"
$env:COMFYUI_MCP_ADMIN_ACTOR = "maintainer"
comfyui-mcp-admin
```

源码运行：

```powershell
uv run comfyui-mcp-admin
```

Admin 覆盖 Workflow、Server、Config、Dependency、Approval、Provisioning 和 Audit。危险操作采用预览、摘要绑定、确认和审计约束。不要把 Admin 进程配置给不受信任 Agent。

## 9. Streamable HTTP 部署

### 9.1 静态 Bearer Token

Windows PowerShell：

```powershell
$env:COMFYUI_MCP_DIR = "D:/comfyui-mcp-workspace"
$env:COMFYUI_MCP_AUTH_MODE = "static"
$env:COMFYUI_MCP_TOKENS = '{"replace-with-secret":{"principal_id":"agent-prod","scopes":["comfyui:execute"]}}'
$env:COMFYUI_MCP_TOOLSET = "execution"
$env:COMFYUI_MCP_ALLOWED_HOSTS = "mcp.example.com"
$env:COMFYUI_MCP_ALLOWED_ORIGINS = "https://agent.example.com"
$env:COMFYUI_MCP_HOST = "0.0.0.0"
$env:COMFYUI_MCP_PORT = "8765"
$env:COMFYUI_MCP_PUBLIC_URL = "https://mcp.example.com/mcp"
comfyui-mcp-http
```

HTTP MCP 地址为：

```text
https://mcp.example.com/mcp
```

`principal_id` 是 Job、Asset、Plan、幂等键和审计记录的稳定所有者。轮换 Token 时保留相同的 `principal_id`。

### 9.2 RFC 7662 Token Introspection

```powershell
$env:COMFYUI_MCP_AUTH_MODE = "introspection"
$env:COMFYUI_MCP_INTROSPECTION_URL = "https://id.example.com/oauth2/introspect"
$env:COMFYUI_MCP_INTROSPECTION_CLIENT_ID = "comfyui-mcp"
$env:COMFYUI_MCP_INTROSPECTION_CLIENT_SECRET = "replace-with-secret"
$env:COMFYUI_MCP_INTROSPECTION_AUDIENCE = "https://mcp.example.com"
```

约束：

- Introspection URL 必须是无嵌入凭据、无 fragment 的 HTTPS URL。
- 返回必须包含 `active=true`、有效 `sub`、匹配的 `aud` 和至少一个支持的 scope。
- Introspection 失败按未授权处理，不降级到静态 Token。

### 9.3 HTTP 端点

| 端点 | 用途 |
|---|---|
| `POST /mcp` | MCP Streamable HTTP |
| `POST /assets?...` | 流式上传原始媒体正文 |
| `POST /assets/fetch` | 从精确 HTTPS Host 白名单抓取媒体 |

远程抓取白名单：

```powershell
$env:COMFYUI_MCP_FETCH_HOSTS = "cdn.example.com,objects.example.com"
```

### 9.4 默认边界

| 配置 | 默认值 |
|---|---:|
| MCP JSON 正文 | 1 MiB |
| Fetch JSON 正文 | 64 KiB |
| 上传 | 25 MiB |
| 每分钟请求 | 120 |
| 普通请求并发 | 32 |
| 订阅流并发 | 8 |
| 每主体订阅流 | 2 |
| 动态工作流工具 | 8（可配置 1–128） |
| 限流模式 | process（external 启用共享 SQLite 后端） |

对应变量：

```text
COMFYUI_MCP_MAX_JSON_BYTES
COMFYUI_MCP_MAX_FETCH_JSON_BYTES
COMFYUI_MCP_MAX_UPLOAD_BYTES
COMFYUI_MCP_REQUESTS_PER_MINUTE
COMFYUI_MCP_MAX_CONCURRENT_REQUESTS
COMFYUI_MCP_MAX_SUBSCRIPTION_STREAMS
COMFYUI_MCP_MAX_SUBSCRIPTIONS_PER_PRINCIPAL
COMFYUI_MCP_MAX_DYNAMIC_TOOLS
```

大上下文模型可提高动态工具预算，例如 `$env:COMFYUI_MCP_MAX_DYNAMIC_TOOLS = "64"`。预算越大，`tools/list` 的 schema 载荷和模型工具选择成本越高；该设置不会绕过 Toolset、Scope 或后端可用性过滤。

公网部署必须由反向代理终止 TLS。默认 `COMFYUI_MCP_LIMIT_MODE=process` 时只允许单 worker，`COMFYUI_MCP_WORKERS>1` 会拒绝启动。

多 worker 部署必须显式启用共享限流：

```powershell
$env:COMFYUI_MCP_LIMIT_MODE = "external"
$env:COMFYUI_MCP_WORKERS = "2"
```

`external` 模式使用 `<COMFYUI_MCP_DIR>/data/shared-limits.sqlite3` 作为跨进程后端：请求按来源 IP/主体共享固定窗口计数，`max_concurrent_requests` 作为按主体的共享并发许可上限（超额返回 503），订阅配额跨 worker 汇总；后端缺失或不可读时拒绝启动（fail-closed），不会回退到进程内计数。不要通过反向代理后的多个单 worker 副本规避 `process` 限制——它们不会共享限流与订阅配额。

## 10. Manager 与供应来源白名单

依赖供应和 Manager 操作必须显式授权来源：

```powershell
$env:COMFYUI_MCP_PROVISION_HOSTS = "github.com,raw.githubusercontent.com"
$env:COMFYUI_MCP_MANAGER_ORIGINS = "http://127.0.0.1:8188"
```

- `PROVISION_HOSTS`：允许下载的精确来源 Host。
- `MANAGER_ORIGINS`：允许调用 Manager 的 ComfyUI Origin。
- 未配置时，相关写入能力不会绕过安全边界。

依赖解析还需要 `<COMFYUI_MCP_DIR>/dependency-catalog.json`。当前没有自动生成命令；缺少该文件或条目时，服务会把依赖标记为 unresolved，并拒绝 install plan。该 catalog 属于部署方维护的受信任供应策略输入，不应从未知工作流自动推断。
示例 `dependency-catalog.json`：

```json
{
  "node:ComfyUI-Example": {
    "kind": "node",
    "source_type": "git",
    "source_url": "https://github.com/example/ComfyUI-Example.git",
    "version": "v1.2.3",
    "checksum": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "size_bytes": 1048576,
    "target_dir": "custom_nodes/ComfyUI-Example",
    "license": "MIT",
    "restart_required": true,
    "install_state": "missing"
  }
}
```

每个 key 必须是 `node:<name>` 或 `model:<name>`；version 必须是固定 Git tag/commit，checksum 必须是 SHA-256，source_url 必须是 HTTPS 且 Host 在白名单内。模型条目使用 `source_type: "model"`，并设置安全的相对 `target_dir`。

## 11. 保留策略与迁移

显式运行维护：

```powershell
$env:COMFYUI_MCP_DIR = "D:/comfyui-mcp-workspace"
$env:COMFYUI_MCP_RUN_RETENTION_DAYS = "30"
$env:COMFYUI_MCP_ASSET_RETENTION_DAYS = "30"
$env:COMFYUI_MCP_MAX_HISTORY_RECORDS = "10000"
comfyui-mcp-maintain
```

旧文件存储迁移前先演练：

```powershell
$env:COMFYUI_MCP_DIR = "D:/comfyui-mcp-workspace"
$env:COMFYUI_MCP_MIGRATION_BACKUP = "D:/backup/comfyui-mcp"
comfyui-mcp-migration-dry-run
```

`comfyui-mcp-migration-dry-run` 只生成清单、校验和备份证据，`writes_performed=false`。

生产切换到 SQLite aggregate 使用显式命令，要求精确确认短语与备份证据：

```powershell
$env:COMFYUI_MCP_DIR = "D:/comfyui-mcp-workspace"
$env:COMFYUI_MCP_MIGRATION_BACKUP = "D:/backup/comfyui-mcp"
$env:COMFYUI_MCP_MIGRATION_CONFIRM = "SWITCH FILE STORES TO SQLITE"
comfyui-mcp-migrate
```

- 命令先 dry-run 并冻结备份，然后在项目迁移锁内依次原子切换 `asset`、`job`、`workflow` 三组；任一组失败时已切换组保持有效，输出 `groups` 与 `writes_performed` 如实反映部分状态，并给出 `recovery.evidence` 用于带 `COMFYUI_MCP_MIGRATION_EVIDENCE` 重跑续传。
- 未配置精确确认短语时退出码为 3 且不写任何内容。全新目录默认保留文件仓库；不要手工写入 `store_migrations`。


Beta 升级前必须备份：

```text
config.json
data/
控制平面 SQLite 数据库
```

## 12. 验证与排错

源码验证：

```bash
uv sync --locked --extra dev
uv run ruff check src/comfyui_mcp_skills tests
uv run mypy src/comfyui_mcp_skills
uv run pytest -q
```

常见问题：

| 症状 | 原因 | 处理 |
|---|---|---|
| `Invalid tools[n].name` | 模型网关拒绝点号工具名 | 设置 `COMFYUI_MCP_PORTABLE_TOOL_NAMES=1` |
| Snow 看不到 MCP | 配置中 `enabled=false` | 改为 `enabled=true` 并重启 Host |
| 找不到 `config.json` | `COMFYUI_MCP_DIR` 指向错误目录 | 指向数据目录，不是源码或 ComfyUI 目录 |
| Operations/Admin 工具缺失 | 默认 Toolset 是 execution | 配置完整主体、Toolset、scopes 和高风险开关 |
| 本地文件被拒绝 | 文件不在授权上传根目录 | 移入 `uploads/` 或配置 `UPLOAD_ROOTS` |
| HTTP 启动失败 | 缺 Token、Origin、Public URL 或认证配置 | 按本节补齐远程部署变量 |
| 无法多 worker 启动 | `COMFYUI_MCP_LIMIT_MODE` 未设为 external | 设置 `COMFYUI_MCP_LIMIT_MODE=external` 后重试；确认 `shared-limits.sqlite3` 可读写 |
| 高级 Revision/Experiment/Routing 工具缺失 | 新目录尚未完成 SQLite aggregate cutover | 先 `comfyui-mcp-migration-dry-run` 演练，再用 `comfyui-mcp-migrate` 执行生产切换 |
| Dependency install 无可用计划 | 缺少受信任 `dependency-catalog.json` 条目 | 由部署维护者提供 catalog；不要自动猜测来源 |
