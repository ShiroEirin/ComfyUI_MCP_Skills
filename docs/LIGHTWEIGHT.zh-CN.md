# 本地轻量引导（装配分层）

本文说明 ComfyUI MCP Skills 的装配分层原则、fresh 数据目录的轻量行为，以及本地 5 分钟上手配置。完整功能语义见[功能与使用模型](FEATURES.zh-CN.md)，安装配置见[安装教程](INSTALLATION.zh-CN.md)。

## 1. 装配分层原则

服务按「核心层默认、增强层按需装配」分层，避免本地单用户场景为云端增强能力付出初始化成本。

| 层 | 装配条件 | 内容 |
|---|---|---|
| **核心层**（本地默认路径） | fresh 数据目录（`data/control-plane.sqlite3` 不存在） | 动态 run 工具直连 `/prompt`、`engine.history` 看引擎历史、`local.plugins` + 节点目录写工作流、本地文件/轻量存储；不创建控制平面数据库 |
| **增强层**（按需装配） | 数据目录已存在控制平面数据库（含 all-file 未 cutover） | 幂等/恢复/retry lineage、审计闭环/owner 隔离、路由/审批/实验矩阵、cutover 迁移治理、编排 worker 与 outbox 投递 |

### 装配规则

- **轻量仅限 fresh**：`data/control-plane.sqlite3` 不存在时，stdio/HTTP 入口不创建该数据库（不建 80+ 表控制面），`store=None`，编排 worker/outbox 不启动。
- **既有数据库 fail-closed**：只要控制平面数据库已存在——包括从未执行 aggregate cutover 的 all-file 目录——启动时都执行完整初始化与 schema 升级。已初始化 all-file 数据库存在 provisioning/experiments 持久化消费者与编排 worker，走轻量会停摆持久工作，因此不做降级。
- **订阅拆分**：目录变更通知（`ToolsListChanged`/`ResourcesListChanged`）是核心契约，fresh 下同样工作；编排 worker/outbox 仅按 SQLite 可用性启动。
- 先运行过 `comfyui-mcp-admin`（管理面会创建控制平面数据库）再运行 `comfyui-mcp`，后者会走完整初始化——这是预期行为。

## 2. 轻量下可用与需 SQLite 的能力边界

「可用/需 SQLite」的完整判定请以 [FEATURES.zh-CN.md 可用性分层](FEATURES.zh-CN.md#可用性分层) 为准，本文不重复该表，只列出本地轻量最常见场景的要点（**非穷举**）：

- **轻量下可用**：工作流发现与动态执行（`comfyui.run.*`）、`asset.upload`、`job.get`/`job.cancel`、`engine.history`（引擎历史只读直连）、`local.plugins`（需 server 条目配置 `local_root`，未配置/云端会话按既有契约返回 `available:false`）、节点/模型只读目录（`node.list/describe`、`model.list`，需对应 Toolset 授权）。
- **需 SQLite aggregate cutover 后可用**：`job.list` 历史分页、Workflow Revision/Plan/Experiment/Routing/Diagnostic、Artifact/Lineage、`workflow.visualize`、`job.history.suggest` 等高级持久化能力——对应 aggregate 切换前工具不挂载或明确返回 backend unavailable，与「数据库存在但未 cutover」的行为一致。
- 全新目录默认保持文件仓库；生产切换由 `comfyui-mcp-migrate` 显式执行（见[安装教程](INSTALLATION.zh-CN.md#12-保留策略与迁移)）。

## 3. 本地 5 分钟上手

以下配置覆盖「观察 / 节点 / 插件 / 引擎历史」的只读运维面，不暴露动态工作流工具（单条目不暴露 `comfyui.run.*`；跑图需要第二条 execution 条目）。

### 3.1 数据目录

```text
my-comfyui-mcp/
├── config.json
└── data/
    └── local/
        └── txt2img/
            ├── schema.json
            └── workflow.json
```

`config.json` 示例（`local_root` 指向本机 ComfyUI 安装根目录，供 `comfyui.local.plugins` 扫描 custom_nodes，按实际环境替换）：

```json
{
  "default_server": "local",
  "servers": [
    {
      "id": "local",
      "name": "Local ComfyUI",
      "url": "http://127.0.0.1:8188",
      "local_root": "D:/ComfyUI",
      "enabled": true
    }
  ]
}
```

### 3.2 只读运维条目（operations）

```json
{
  "mcpServers": {
    "comfyui-ops": {
      "command": "comfyui-mcp",
      "env": {
        "COMFYUI_MCP_DIR": "D:/comfyui-mcp-workspace",
        "COMFYUI_MCP_PRINCIPAL_ID": "local-observer",
        "COMFYUI_MCP_TOOLSET": "operations",
        "COMFYUI_MCP_SCOPES": "comfyui:observe",
        "COMFYUI_MCP_ENABLE_HIGH_RISK": "1"
      }
    }
  }
}
```

该条目是**只读**面：`comfyui:observe` 提供 `node.list/describe`、`model.list`、`local.plugins`、`engine.history`、`server.health` 等只读目录与引擎历史工具。`COMFYUI_MCP_ENABLE_HIGH_RISK=1` 只负责让非 execution Toolset 通过启动准入，**不授予** `comfyui:operate`——队列清理、中断、显存释放等修改性运维工具不会被暴露；需要可写运维时另行配置 `comfyui:operate` 并知悉其影响。**注意：单条目不暴露动态工作流工具**——需要执行工作流时添加下面第二条目。

### 3.3 执行条目（execution，第二条目）

```json
{
  "mcpServers": {
    "comfyui-ops": {
      "command": "comfyui-mcp",
      "env": {
        "COMFYUI_MCP_DIR": "D:/comfyui-mcp-workspace",
        "COMFYUI_MCP_PRINCIPAL_ID": "local-observer",
        "COMFYUI_MCP_TOOLSET": "operations",
        "COMFYUI_MCP_SCOPES": "comfyui:observe",
        "COMFYUI_MCP_ENABLE_HIGH_RISK": "1"
      }
    },
    "comfyui-exec": {
      "command": "comfyui-mcp",
      "env": {
        "COMFYUI_MCP_DIR": "D:/comfyui-mcp-workspace"
      }
    }
  }
}
```

`comfyui-exec` 使用默认安全模型（`local-stdio` + `execution` + `comfyui:execute`），暴露动态 `comfyui.run.<server>.<workflow>` 工具、`asset.upload`、`job.get`/`job.cancel`。两个条目指向同一数据目录；首次启动均为 fresh 时都走轻量路径。

### 3.4 验证

1. 启动 MCP Host 后，通过 `comfyui.capability.search` 查看当前主体可见能力。
2. `comfyui.engine.history` 查看引擎历史；`comfyui.local.plugins` 查看插件清单（需配置 `local_root`）。
3. 确认数据目录未生成控制平面数据库（`data/control-plane.sqlite3` 不存在），即处于轻量模式。

> 提示：轻量模式下没有编排 worker 与 SQLite 持久化，跨进程恢复/审批/实验等增强能力不可用；需要这些能力时按[安装教程](INSTALLATION.zh-CN.md#12-保留策略与迁移)执行生产 cutover。
