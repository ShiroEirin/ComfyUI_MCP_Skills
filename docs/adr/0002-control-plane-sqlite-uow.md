# ADR-0002：SQLite 控制平面 Schema 与 Unit of Work

- 状态：Accepted
- 日期：2026-07-30
- 适用阶段：G0-B 及后续持久化切换阶段

## 背景

现有 Workflow、Run 和 Asset 使用独立文件仓库，无法在一个提交中原子写入 aggregate、work item、领域事件和 Outbox。G0 需要先证明事务边界和未来对象关系可落地，但不得提前切换生产 Repository、启动 Orchestrator 或发布 Outbox。

## 决策

### 1. SQLite 与连接策略

第一阶段使用 Python 标准库 `sqlite3` 和单机绝对路径数据库。数据库路径不得经过符号链接，已有目标必须是普通文件；初始化创建文件后必须成功收紧为仅当前用户可读写。每个连接启用并回读验证：

- `foreign_keys=ON`；
- `busy_timeout=5000`；
- `synchronous=FULL`；
- `trusted_schema=OFF`。

数据库初始化时在事务外固定并验证 `journal_mode=WAL`。业务 Unit of Work 使用 `isolation_level=None` 和显式 `BEGIN IMMEDIATE`，进入事务前再次验证关键 PRAGMA；SQLite 不承担跨主机 worker 的租约或 fencing。

### 2. 版本化 Schema migration

`schema_migrations` 记录：

- `version`、`name`；
- 64 位小写 hex `checksum` 与 `schema_fingerprint`；
- `up_supported`、`down_supported`；
- `feasibility_note`、`applied_at`。

Checksum 对版本、名称、全部有序 up/down 语句、可行性声明和 bootstrap SQL 计算 canonical JSON SHA-256。代码内 migration 版本必须为正整数、唯一且严格递增，但不要求连续。每次初始化先验证数据库版本是代码 migration 的严格前缀、逐项元数据一致，且全部已提交行的唯一 fingerprint 等于迁移前实时 schema；已提交行不接受零指纹。零行历史时，实时 schema 必须精确等于 bootstrap schema。只在验证通过后应用缺失后缀，并在同一事务中把所有剩余行更新为新 schema fingerprint。任何漂移立即失败，不使用 `INSERT OR IGNORE`。

初始 down migration 只表示“尚无 aggregate 切换时，DDL 可事务回滚”。`store_migrations` 中存在任何非空 `switched_at` 后，Schema rollback 永久拒绝；`switched_at`、切换身份和 checksum 不可改写，状态只允许 `switched -> superseded`。多级 down 在执行前一次性验证当前 schema，执行后统一刷新剩余版本 fingerprint。

### 3. 按 aggregate 切换事实源

`store_migrations` 使用 `(aggregate_kind, version)` 主键，记录 `status`、`checksum` 和 `switched_at`。部分唯一索引保证每个 aggregate 最多一个 `switched` 版本。G0 不创建任何生产 aggregate 的 switched 记录；G1、G3 按对象域分别切换，禁止全局 `store_version`。

### 4. 领域关系与完整性

初始 Schema 一次创建 Workflow、Revision、Deployment、Plan、Job、ExecutionAttempt、IdempotencyRecord、Asset、Artifact 和旧 Resource alias 表，但这些表在对应阶段切换前不是生产事实源。

数据库约束包括：

- 所有 TEXT 主键、外键和唯一键成员显式约束 SQLite `typeof(...)=text`；typed ID 与 SHA-256 使用长度、前缀和小写 hex CHECK；
- Deployment 通过复合外键绑定同一 Workflow 的 Revision；
- Plan 通过复合外键固定 Deployment、Workflow、Revision 和 server；
- Job 的 Plan/Revision/Deployment 三列必须全空或全非空，非空时复合绑定同一 Plan；owner、retry 关系和执行身份列受租户约束且不可原地改写；
- 同一 Workflow/server 最多一个 `published=true` Deployment；
- Attempt 的 `submission_state` 仅允许 `submission_unknown` 或 `submitted`，prompt/job 上游 ID 必须是非空安全标识符并分别使用非空部分唯一索引；Attempt 的身份字段及删除操作不可变。仅 unknown 且无上游 ID 的记录允许一次性补写至少一个上游 ID并转为 submitted，后续映射不可覆盖；server 必须匹配绑定 Plan；
- Artifact 唯一定位与确定性 ID tuple 完全一致，文件路径字段拒绝 NUL；已提交 Artifact 禁止 UPDATE/DELETE，且 server 必须匹配绑定 Plan；
- 旧 Resource alias 使用显式对象外键、对象类型与 canonical URI 一致性 CHECK，并为四类可空外键分别建立部分索引；
- Job 三组 keyset 索引覆盖 owner、status 和 workflow 过滤；
- Outbox 只为 `status="pending"` 建部分索引。

G1 Job 的 `workflow_id` 暂不外键到 Workflow：Job 在 G1 切换，Workflow 到 G3 才迁移；但它仍使用 canonical Workflow ID 校验，拒绝完整的其他对象 typed ID。G3 完成兼容映射后再通过前向 migration 收紧引用。

### 5. Unit of Work

应用层 `ControlPlaneUnitOfWork` 端口和 SQLite 实现共享一个连接、一个显式事务。G0 仅用隔离的 `test_aggregates`、`work_items`、`domain_events` 和 `outbox` Repository 证明以下契约：

- 四类写入全部提交或全部回滚；
- 未调用 `commit()` 或上下文异常时 rollback；
- 任一 Repository 写失败后 UoW 进入 failed 状态，拒绝后续写入和 commit；
- `commit()` 成功后关闭连接，所有 Repository 和再次 commit/rollback 均拒绝；同一 UoW 实例不可重入；
- `__exit__` 不吞业务异常；commit、rollback、close 同时失败时保留主异常，并附带 cleanup 诊断；
- Event Repository 在同一事务中按 subject 原子分配单调 sequence；
- Repository 不自行 connect、commit、close 或发布外部消息。

这些测试表不伪装成生产多 aggregate WorkItem。后续阶段通过前向 migration 和真实 Repository 扩展，不能把 G0 test aggregate 当作生产模型。

## 结果

- G0 可以用真实 SQLite 事务证明原子性，而不触碰现有文件事实源。
- G1/G3/G4 所需关系、唯一性和查询索引已有可验证落点。
- migration 漂移、跨对象错误绑定和 switched 后反向切换在数据库边界失败。
- PostgreSQL 实现必须复用同一应用端口和失败注入契约，不能改变提交语义。

## 非目标

本 ADR 不实现文件 manifest、备份、导入或事实源切换；这些属于 G0-C。它也不实现 Revision→Plan→Job 生产 Repository 或动态 run 切换；这些分别属于 G0-D、G3 和 G4。
