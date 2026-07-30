# ADR-0004：最小 Revision → Plan → Job 隔离契约 Harness

- 状态：Accepted
- 日期：2026-07-30
- 适用阶段：G0-D 及后续 Workflow/Execution 切换

## 背景

G0 已定义规范身份和 SQLite 关系，但仅有 DDL 不足以证明 Revision、Deployment、Plan 与 Job 可以形成稳定且不可漂移的最小纵向切片。真实 Workflow Repository 切换属于 G3，真实动态执行链切换属于 G4；G0 必须在不接入现有 MCP/CLI 调用方的前提下执行该契约。

## 决策

`RevisionPlanJobContractHarness` 只接受不存在、无符号链接且位于私有父目录的新 SQLite 保留路径。它用 `O_EXCL` 原子保留该名称，但不在该路径初始化或发布数据库；实际数据库始终位于同父目录随机 0700 staging 目录中，并通过 `SQLiteControlPlaneStore.initialize()` 初始化、写入本实例随机 nonce。初始化完成后只复核保留文件未被替换，不执行会覆盖竞态目标的 rename/replace。运行前同时验证 staging 数据库文件 identity 与 `g0_contract_harness` role/nonce，避免测试记录触及已有控制库。它在一个 `BEGIN IMMEDIATE` 事务中物化：

1. 项目级 Workflow；
2. 不可变 WorkflowRevision，保存 canonical graph、parameter schema、dependency contract 和内容摘要；
3. 固定 Revision 与 `server_id` 的 published WorkflowDeployment；
4. 固定 Revision、Deployment、server、resolved inputs 与摘要的不可变 ExecutionPlan；
5. 完整绑定 Plan/Revision/Deployment 的非 legacy Job；
6. 一个旧服务器作用域 Workflow URI 到 canonical Workflow URI 的只读 alias。

输入 graph 和 resolved inputs 在事务前各 canonical JSON 序列化一次；保存内容、全部摘要和派生 ID 只从该冻结快照生成。Harness 在事务内主动验证 Revision 和 Plan UPDATE 被数据库拒绝；提交后通过旧 URI parser 与兼容索引验证 alias 解析，并验证 alias UPDATE/DELETE、非 legacy Job 无 Plan 绑定均被 schema 拒绝，生产 `store_migrations` 仍为空。

Harness 支持在 commit 前注入失败。失败时 Workflow、Revision、Deployment、Plan、Job 和 alias 全部 rollback，不留下半条血缘。连接 rollback/close 保留原始异常；初始化失败仅按保留文件 identity 清理本实例拥有的目标与 sidecar。

## 结果

- G0 以可执行证据证明最小 Revision → Deployment → Plan → Job 模型可落地。
- Revision/Plan 内容不会因调用方对象或后续配置变化而漂移。
- canonical URI 与旧 URI alias 可以指向同一 Workflow 身份。
- 真实 Repository、动态 Tool 和 ComfyUI 提交链保持未切换。

## 非目标

本 Harness 不提供生产 Revision/Plan/Job Repository，不执行 ComfyUI 请求，不创建 ExecutionAttempt、Artifact 或 Outbox dispatcher，不替代 G3/G4 的真实纵向切片与兼容回填。
