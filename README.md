# ComfyUI MCP Skills

面向 AI Agent 的 ComfyUI MCP 服务。基于 MCP `2026-07-28` 和 MCP Python SDK v2，同时保留原有 CLI 兼容入口。

## 能力

- 每个启用的 ComfyUI 工作流动态生成一个带 JSON Schema 的 MCP Tool。
- 支持提交、限时等待、节点进度、作业恢复查询、排队作业取消和持久化幂等键。
- 支持图片、遮罩、音频等文件上传，并以稳定 `asset_id` 注入工作流。
- 通过 MCP Resources 暴露工作流、资产、作业和输出元数据。
- 提供 stdio 和带认证的 Streamable HTTP 两种传输。
- 危险的工作流修改与删除工具使用独立、默认关闭的管理进程。

完整架构与迁移依据见 [`MCP_MIGRATION_PLAN.zh-CN.md`](./MCP_MIGRATION_PLAN.zh-CN.md)。

## 安装

```bash
python -m pip install -e .
```

要求 Python 3.10+。ComfyUI 服务器和工作流目录沿用原 CLI 配置：

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

stdio 默认只允许 `COMFYUI_MCP_DIR/uploads` 下的本地文件通过
`comfyui.asset.upload` 上传。需要授权其他目录时，使用系统路径分隔符配置：

```powershell
$env:COMFYUI_MCP_UPLOAD_ROOTS = "D:/media;E:/shared-assets"
```

执行面默认暴露：

- `comfyui.run.<server>.<workflow>`：动态工作流工具。
- `comfyui.asset.upload`：上传授权目录中的本地媒体。
- `comfyui.job.get`：恢复查询持久化作业。
- `comfyui.job.cancel`：移除排队作业；对运行中作业安全拒绝，避免调用 ComfyUI 的全局中断。
- `comfyui://outputs/<server>/<prompt>/<index>`：读取受所有者校验和 25 MiB 上限保护的输出媒体 Blob。

动态工作流工具的 `_execution` 参数支持：

```json
{
  "idempotency_key": "agent-call-42",
  "wait": true,
  "wait_timeout_seconds": 120
}
```

超时不会丢失作业。返回的 `prompt_id` 可继续传给 `comfyui.job.get`。

## Streamable HTTP

远程模式拒绝匿名启动。至少配置一个 Bearer Token：

```powershell
$env:COMFYUI_MCP_DIR = "D:/path/to/project"
$env:COMFYUI_MCP_TOKENS = '{"replace-with-secret":["comfyui:execute"]}'
$env:COMFYUI_MCP_ALLOWED_HOSTS = "mcp.example.com"
$env:COMFYUI_MCP_ALLOWED_ORIGINS = "https://agent.example.com"
$env:COMFYUI_MCP_HOST = "0.0.0.0"
$env:COMFYUI_MCP_PUBLIC_URL = "https://mcp.example.com/mcp"
$env:COMFYUI_MCP_OAUTH_ISSUER_URL = "https://auth.example.com"
$env:COMFYUI_MCP_FETCH_HOSTS = "cdn.example.com,objects.example.com"
comfyui-mcp-http
```

端点：

- `POST /mcp`：MCP Streamable HTTP。
- `POST /assets?server_id=<id>&filename=<name>&purpose=image`：流式上传原始媒体正文。
- `POST /assets/fetch`：仅从 `COMFYUI_MCP_FETCH_HOSTS` 精确白名单中的公开 HTTPS 地址抓取媒体；连接固定到已校验的 DNS 地址。

远程传输启用 Host/Origin 校验、请求体限制、上传大小限制、请求 ID 和进程内限流。公网部署仍应由反向代理终止 TLS，并使用外部密钥管理和集中限流。
`/mcp` 的 JSON-RPC 请求体默认限制为 1 MiB；`/assets/fetch` 的 JSON 请求体默认限制为 64 KiB。媒体上传仍使用独立的 `COMFYUI_MCP_MAX_UPLOAD_BYTES` 上限。

## 独立管理面

管理工具不会出现在普通 MCP 服务中。只有显式开启后才能启动：

```powershell
$env:COMFYUI_MCP_ENABLE_ADMIN = "1"
$env:COMFYUI_MCP_DIR = "D:/path/to/project"
comfyui-mcp-admin
```

该进程提供工作流启用/停用和带精确确认短语的永久删除。不要将它与普通执行端共享客户端配置或远程端口。

## CLI 兼容入口

原有运维和诊断命令继续可用：

```bash
comfyui-skill --help
comfyui-skill list --json
comfyui-skill info local/txt2img --json
```

## 验证

```bash
python -m pytest --cov --cov-report=term-missing -q
```

覆盖率门禁针对新 MCP 包，最低为 80%。原 CLI 回归用例仍包含在同一测试套件中。
