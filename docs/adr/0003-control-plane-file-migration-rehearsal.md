# ADR-0003：文件事实源 Manifest 与隔离迁移演练

- 状态：Accepted
- 日期：2026-07-30
- 适用阶段：G0-C 及后续 aggregate 数据迁移

## 背景

现有 Workflow、Run/Idempotency 和 Asset 使用不同文件布局、锁和序列化规则。G0 必须证明源文件可形成稳定审计证据，备份可复核，导入可重复且切换与回滚具备事务语义；但真实 Job/Asset 回填属于 G1，Workflow 回填属于 G3，G0 不得提前改变生产事实源。

## 决策

### 1. 只读扫描边界

`FileMigrationRehearsal` 只枚举当前已发布事实文件：

- `data/<server>/<workflow>/{schema.json,workflow.json}`；
- `data/<server>/<workflow>/history/*.json`；
- `data/assets/*.json`；
- `data/runs/*/{prompts,idempotency}/*.json`。

扫描不调用现有 Repository loader，避免 Asset `get()` 更新 mtime、默认字段工厂制造新时间或宽松 loader 静默跳过损坏记录。`config.json` 可能包含认证凭据，不进入 manifest 或普通目录备份。源根、目录和文件拒绝符号链接/reparse point；文件通过单一打开句柄读取，前后校验文件身份、大小和 mtime，并拒绝硬链接。单文件、文件数和总字节均有固定上限，超限进入冲突报告。

所有扫描、备份、dry-run 和隔离演练持有按项目规范路径及目录文件身份 SHA-256 派生的全局 migration lock。Windows 使用带显式最小 DACL 的 `Global\\` 命名 Mutex，跨交互会话与服务会话协调且不接触文件系统；POSIX 使用 `/tmp` 下含有效 UID 与项目摘要的单链接 0600 常规文件，以 `O_NOFOLLOW` 打开并校验有效 UID 后 `flock`。锁不写入可变源树，避免锁叶符号链接导致只读入口修改根外文件。Workflow Admin、CLI Workflow/Bundle、CLI History、MCP Run/Asset 与 retention 删除均按相同 migration → 局部锁顺序协调；因此一次演练看到的是稳定文件事实集合。

### 2. Manifest

Manifest v1 记录固定捕获时间，并对每个源文件记录：

- POSIX 规范相对路径；
- 原始文件字节 SHA-256；
- 字节大小；
- `mtime_ns`。

条目按相对路径排序；manifest digest 是不含绝对根路径的 canonical JSON SHA-256。所有消费入口先验证版本、捕获时间、路径规范与唯一性、摘要和数值边界，并根据 entries 重算 digest。后续重试必须复用同一 manifest；新增、删除、改写或 mtime 变化均视为漂移，不自动刷新证据。

### 3. Backup

证据父目录必须位于当前进程有效身份的用户 profile 内。POSIX 校验到 profile 根的每一级 owner、链接和 group/world 写权限；Windows 校验本地卷、reparse point，并解析普通/object/callback allow ACE，只允许当前 token SID、Owner Rights、SYSTEM 与 Administrators 获得写权限。宽权限目录必须由运维先收紧，演练不会在不可信临时目录降级执行。

`COMFYUI_MCP_MIGRATION_BACKUP` 指定源根之外的本地父目录。实现创建权限收紧的随机 staging 目录，直接使用 manifest 捕获的同一原始字节，逐文件以 0600 写入并 `flush + fsync`，恢复固定 mtime，写入 `migration-manifest.json`，再次复核完整源 manifest 后发布为随机唯一备份目录。备份不复制 `config.json`；失败删除 staging，不改源文件。

### 4. Dry-run

`comfyui-mcp-migration-dry-run` 通过 `COMFYUI_MCP_DIR` 选择源根，可通过 `COMFYUI_MCP_MIGRATION_BACKUP` 生成备份。输出稳定 JSON：manifest 版本与摘要、源文件数、有效记录数、冲突明细、是否执行写入和备份证据。无冲突返回 0，数据冲突返回 2，扫描、漂移、权限或证据生成失败返回结构化 `migration_evidence_failed` 并使用 3。

JSON 使用捕获 manifest 时的同一原始字节严格 UTF-8 解析，要求根为 object，并拒绝重复键。Workflow dry-run 复用当前参数规范化、节点目标与 JSON Schema 校验；Asset 校验全部字段类型、规范 ID、路径/载荷身份、server、引用、媒体、时间和摘要；Run/History 按各自路径、hash、身份、摘要和 reservation 语义校验，活跃 reservation 阻断迁移。任何冲突都不写数据库、不创建生产 `store_migrations`、不切换 Repository。

### 5. 隔离 cutover rehearsal

隔离演练必须先用 `create_isolated_database()` 新建专用数据库并取得带随机 nonce 的 `IsolatedRehearsalDatabase`。已有数据库、符号链接路径或缺少 `g0_isolated_rehearsal` 角色标记的数据库拒绝使用。`rehearse_isolated_cutover()` 只接受无冲突 dry-run report：

1. 验证 report 的 manifest 自身完整且源文件未漂移；
2. 通过 `SQLiteControlPlaneStore.initialize()` 复核 schema migration、fingerprint、WAL 和连接安全契约；
3. `BEGIN IMMEDIATE` 并在事务内验证隔离角色与 nonce，同时要求生产 `store_migrations` 为空；
4. 以 manifest 相对路径派生隔离 `test_aggregates` ID，插入或复用完全相同 payload；
5. 校验导入计数并再次复核源 manifest；
6. 在同一事务写入 `test_migration_switches`，不触碰生产 `store_migrations`；
7. commit；任一步异常完整 rollback。

重复执行同一 report 不生成重复对象。manifest 或既有演练 checksum 不同则失败。该 API 不导入生产 Job、Asset、Workflow 表，也不改变任何生产 Repository 绑定；真实迁移阶段仍必须实现各 aggregate 的版本化解析器和生产切换审批。

## 结果

- G0 获得可执行、可重复的源证据与备份流程。
- 事务演练证明导入、校验和切换证据原子提交，失败不产生半切换状态。
- 文件事实源在 G0 始终保持生产唯一写入源。
- G1/G3 可以复用 manifest 和 backup 契约，但必须增加完整领域映射、冲突报告和生产 Repository 切换。

## 非目标

本 ADR 不执行真实 Job、IdempotencyRecord、Asset、Workflow、Revision、Deployment 或 Artifact 回填，也不发布生产 cutover。全局 migration lock 仅提供短时一致性窗口，不代表事实源已切换；真实回填与切换仍分别由 G1、G3 和对应运维审批完成。
