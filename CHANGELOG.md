# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与[语义化版本](https://semver.org/lang/zh-CN/)。版本号单一来源为 `comfyui_mcp_skills.__version__`；发布 tag 由 CI 校验与之一致。

## [1.1.0] - 2026-08-10

正式发布（移除 Beta 预发布标记；包元数据 classifier 同步为 Production/Stable）。本版本从 1.0.0 基线（d9c2467）起累计 114 个提交。

### 新增（Added）

**控制面与执行**
- 控制面身份体系：Workflow/Revision/Deployment/Plan/Job/ExecutionAttempt/IdempotencyRecord/Asset/Artifact 的 schema、规范 ID 与 canonical URI。
- job/asset SQLite cutover 与生产迁移（dry-run 演练、一致性校验、原子切换、失败回滚续传、store fencing）。
- 不可变 Revision/Deployment 发布、rollback 与载荷完整性保护。
- 幂等执行（idempotency_key 双层校验）、execution plan/commit、崩溃恢复与 Job reconciliation。
- 审批式重启执行闭环：`runtime.restart` plan → approve（单次审批，1 小时 TTL）→ commit（drain/fence 原子协调）→ get；systemd/Docker/Windows Service RuntimeController 三平台适配器。
- 多服务器路由（execution.plan/commit、route.explain、policy.evaluate）、Server Revision pin 与双层幂等。

**工作流与节点**
- 语义图描述、参数目标校验、损失感知 API/Editor 导入、`workflow.list`/`workflow.validate`。
- 图级变更 plan/commit：节点生命周期、subgraph 提取/按名复用、高层分支 recipe（`set_scalar_input`/`upscale_image`/`save_image`/`lora_model`/`controlnet_apply.v1`，经 `apply_recipe`）。
- 校验失败定位与修复引导（node/field + `node.describe` hint）。
- `workflow.visualize` 有界 Mermaid 渲染与 `revision.diff` mermaid 视图（added 节点高亮）。

**节点感知与建议**
- 节点/模型目录工具在 AUTHORING/ADMIN 面可见（授权对齐）；`node.blueprint` 目标驱动投影。
- `model.guidance`（9 个模型家族社区共识起点）、`job.history.suggest`（运行历史证据建议）。
- `local.plugins` 第三方整合包兼容（aki 双布局、有界扫描、reparse 拒绝、云端降级）。
- `engine.history` 只读引擎历史（有界 + 扁平投影）。

**可观测性与运维**
- 审计闭环（append-only 事件 + `audit.get/retry/export` 有界导出）。
- OpenTelemetry traces/metrics/logs（OTLP/HTTP 按信号端点；logs 经 `_ProjectingLogHandler` 白名单投影与防导出循环）。
- 同主机多 worker SQLite 共享限流、保留策略、确定性诊断与安全重试 lineage。
- 装配分层：fresh 数据目录轻量装配（不建控制面库），既有数据库完整初始化（fail-closed）。
- admin portable 工具名（`COMFYUI_MCP_PORTABLE_TOOL_NAMES`，碰撞拒绝 + canonical 分发）。

### 变更（Changed）

- 持久化 schema 自 1.1.0 起**版本化冻结**：已发布迁移（v1–v13）不可改写，跨版本升级由迁移回归套件验证；升级自动且单向，新 schema 被旧代码打开显式拒绝（fail-loud，不承诺降级）。
- 本地默认装配轻量化：全新数据目录不创建控制面数据库；编排 worker/outbox 按 SQLite 可用性门控；目录变更通知保持为核心契约。
- 5 份文档与代码全量对齐（能力状态、预算常量、工具面、测试基线）。

### 修复（Fixed）

- MCP 2026 协议契约兼容修复。
- 本地读取安全加固：有界响应解码（8 MiB）、TOCTOU stat→open→fstat→read→stat 复核、Windows reparse/junction 拒绝、路径/凭据清洗。
- 幂等与审计语义修正（request_id 幂等、intent-first 审计、terminal 失败显式报错）。
- 引擎历史状态归一化（success/error/running 白名单）与数值时间排序。

### 移除（Removed）

- Beta 预发布标记（1.1.0 正式发布）。
- workflow aggregate cutover 后 file-backed `set_enabled`/`delete` 工具（审计工具保留）。

[1.1.0]: https://github.com/ShiroEirin/ComfyUI_MCP_Skills/releases/tag/v1.1.0
