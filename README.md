# ComfyUI MCP Skills

面向 AI Agent 的 ComfyUI MCP 服务。基于 MCP `2026-07-28` 和 MCP Python SDK v2，同时保留原有 CLI 兼容入口。

## 能力

- 每个启用的工作流动态生成带 JSON Schema 的 MCP Tool；目录变化后发送 `notifications/tools/list_changed` 和 `notifications/resources/list_changed`。
- 固定工具覆盖资产上传、持久化作业查询/取消、服务器健康、节点搜索/详情和模型搜索。
- MCP Resources 暴露工作流、资产、作业和输出；`resources/list` 枚举工作流，模板声明所有可参数化 URI。
- 支持 stdio 和带静态 Bearer Token 的 Streamable HTTP。
- 上传、路径、下载大小、Host、Origin、请求体、并发和速率均受边界校验。
- 危险的工作流修改与删除位于独立、默认关闭的管理进程。
- 版本 `1.1.x` 的产品成熟度为 Beta；协议目标为 MCP `2026-07-28`，SDK 为 Python SDK v2。

完整架构与迁移依据见 [`MCP_MIGRATION_PLAN.zh-CN.md`](./MCP_MIGRATION_PLAN.zh-CN.md)。

## 安装

从 PyPI 安装：

```bash
python -m pip install comfyui-mcp-skills
```

开发环境使用锁文件恢复全部依赖：

```bash
uv sync --locked --extra dev
```

要求 Python 3.10+。安装后提供四个入口：`comfyui-mcp`、`comfyui-mcp-http`、`comfyui-mcp-admin` 和 `comfyui-mcp-maintain`；原 `comfyui-skill` CLI 继续保留。

项目目录沿用原 CLI 布局：

```text
config.json
data/
  <server_id>/
    <workflow_id>/
      schema.json
      workflow.json
```

## stdio MCP

MCP 客户端配置示例：

```json
{
  "mcpServers": {
    "comfyui": {
      "command": "comfyui-mcp",
      "env": {
        "COMFYUI_MCP_DIR": "D:/path/to/project"
      }
    }
  }
}
```

stdio 默认只允许 `COMFYUI_MCP_DIR/uploads` 下的本地文件通过 `comfyui.asset.upload` 上传。使用系统路径分隔符扩展授权目录：

```powershell
$env:COMFYUI_MCP_UPLOAD_ROOTS = "D:/media;E:/shared-assets"
```

固定工具按独立逻辑端点与 scope 暴露：

- 主 MCP 的 Execution / Operations / Authoring Toolset：`comfyui.capability.search` / `comfyui.capability.describe`，只搜索当前授权能力，不改变 `tools/list`。
- Execution：5 个固定工具，包括 Catalog、`asset.upload`、`job.get`、`job.cancel`；另有最多 8 个 `comfyui.run.<server>.<workflow>` 动态工具。
- Operations：7 个固定工具，包括 Catalog、Server、Node 和 Model 发现工具。
- Authoring：9 个固定工具，包括 Catalog、只读发现、`revision.list` 和 `workflow.describe`。
- 独立 Admin：4 个工作流启停/删除及审计恢复工具，不暴露 Catalog，且仅由独立管理进程提供。

单端点固定工具默认不超过 16 个，硬上限 20 个；排序保持确定以稳定 Host 缓存。

运行中作业不会调用 ComfyUI 的全局 `/interrupt`。`comfyui.server.health` 的 `cancel_running_supported=false` 明确暴露该上游限制。

动态工作流工具的 `_execution` 参数支持：

```json
{
  "idempotency_key": "agent-call-42",
  "wait": true,
  "wait_timeout_seconds": 120
}
```

超时不会丢失作业。返回的 `prompt_id` 可继续传给 `comfyui.job.get`。

Resource 模板包括：

- `comfyui://workflows/{server_id}/{workflow_id}`
- `comfyui://assets/{server_id}/{asset_id}`
- `comfyui://jobs/{server_id}/{prompt_id}`
- `comfyui://outputs/{server_id}/{prompt_id}/{index}`

同一服务器上的 output Resource URI 可直接作为后续工作流的 image、mask、audio 或 video 参数。服务会校验作业所有者、输出索引和媒体类型，并注入 ComfyUI 服务端引用，不下载后再上传。

## Streamable HTTP

远程模式拒绝匿名启动。当前只实现部署方配置的预共享静态 Bearer Token。`COMFYUI_MCP_AUTH_MODE` 只接受 `static`；OAuth 2.1、JWT/JWKS 和 Token Introspection 尚未实现，服务不会发布 OAuth Protected Resource Metadata。

```powershell
$env:COMFYUI_MCP_DIR = "D:/path/to/project"
$env:COMFYUI_MCP_AUTH_MODE = "static"
$env:COMFYUI_MCP_TOKENS = '{"replace-with-secret":{"principal_id":"agent-prod","scopes":["comfyui:execute"]}}'
$env:COMFYUI_MCP_ALLOWED_HOSTS = "mcp.example.com"
$env:COMFYUI_MCP_ALLOWED_ORIGINS = "https://agent.example.com"
$env:COMFYUI_MCP_HOST = "0.0.0.0"
$env:COMFYUI_MCP_PUBLIC_URL = "https://mcp.example.com/mcp"
$env:COMFYUI_MCP_FETCH_HOSTS = "cdn.example.com,objects.example.com"
comfyui-mcp-http
```

`principal_id` 是持久化作业、资产和幂等键的稳定所有者。轮换 Token 时必须保留同一个 `principal_id`；允许旧、新 Token 在轮换窗口内同时映射到该主体。Token、主体和 scopes 的类型或值不合法时，服务拒绝启动。

端点：

- `POST /mcp`：MCP Streamable HTTP。
- `POST /assets?server_id=<id>&filename=<name>&purpose=image`：流式上传原始媒体正文。
- `POST /assets/fetch`：仅从 `COMFYUI_MCP_FETCH_HOSTS` 精确白名单中的公开 HTTPS 地址抓取媒体。

默认限制：MCP JSON 1 MiB、抓取 JSON 64 KiB、上传 25 MiB、每分钟 120 请求、普通请求并发 32、订阅流并发 8、每主体订阅流 2。普通池饱和时快速返回 503；订阅主体配额饱和时返回 429，不会让长期 `subscriptions/listen` 阻塞工具、资源或资产请求。可分别通过 `COMFYUI_MCP_MAX_CONCURRENT_REQUESTS`、`COMFYUI_MCP_MAX_SUBSCRIPTION_STREAMS` 和 `COMFYUI_MCP_MAX_SUBSCRIPTIONS_PER_PRINCIPAL` 调整。公网部署必须由反向代理终止 TLS，并使用外部密钥管理与集中限流。

进程内限流只适用于单 worker。多 worker 部署必须同时设置：

```powershell
$env:COMFYUI_MCP_WORKERS = "4"
$env:COMFYUI_MCP_LIMIT_MODE = "external"
```

`external` 表示部署方已在网关层提供全局限流；服务不会伪装成已实现 Redis 限流。

## 独立管理面

管理工具不会出现在普通 MCP 服务中。只有显式开启后才能启动：

```powershell
$env:COMFYUI_MCP_ENABLE_ADMIN = "1"
$env:COMFYUI_MCP_DIR = "D:/path/to/project"
comfyui-mcp-admin
```

该进程提供工作流启用/停用和带精确确认短语的永久删除。删除必须携带调用方生成的稳定 `request_id`；返回 `committed` 与 `audit_status`。`comfyui.admin.audit.get` 可查询提交状态，`comfyui.admin.audit.retry` 只补写待完成的审计结果，不重复危险操作。不要将管理进程与普通执行端共享客户端配置或远程端口。

## 保留策略与可观测性

服务写入 JSON 结构化日志到 stderr。通过 `COMFYUI_MCP_LOG_LEVEL` 设置级别；日志只记录白名单上下文字段，不记录 Token 或请求正文。HTTP 进程维护请求总数、错误数、429 数和累计耗时的进程内指标快照。

元数据清理是显式维护操作，不在请求路径自动删除：

```powershell
$env:COMFYUI_MCP_RUN_RETENTION_DAYS = "30"
$env:COMFYUI_MCP_ASSET_RETENTION_DAYS = "30"
$env:COMFYUI_MCP_MAX_HISTORY_RECORDS = "10000"
comfyui-mcp-maintain
```

清理器保留运行中作业和幂等记录引用的作业；只要存在活动作业，就不会清理任何资产。清理过程与在线作业、资产元数据读写共享协调锁，删除前会重新检查活动状态和引用。

## CLI 兼容入口

原有运维和诊断命令继续可用，并与 MCP 共用同一个 ComfyUI 客户端实现：

```bash
comfyui-skill --help
comfyui-skill list --json
comfyui-skill info local/txt2img --json
```

## 验证

```bash
uv sync --locked --extra dev
uv run ruff check src/comfyui_mcp_skills
uv run mypy src/comfyui_mcp_skills
uv run python -m pytest --cov --cov-report=term-missing -q
uv run pip-audit
uv build
```

G6 工具选择基线使用 OMP NewAPI 回环端点中配置的 DeepSeek V4 Flash：

```bash
uv run comfyui-mcp-eval-deepseek evals/g6-tool-selection.json --output artifacts/g6-deepseek-v4-flash-baseline.json
```

CI 在 Ubuntu 与 Windows 上覆盖 Python 3.10–3.13，并验证构建后的 wheel 可独立导入及版本一致。
