# G1 Job/Asset 确定性迁移与事实源切换协议

## Evidence

### Code Sections
- `src/comfyui_mcp_skills/infrastructure/persistence/file_migration.py:67~168`（Manifest/报告）：Manifest 固定相对路径、SHA-256、大小、`mtime_ns`，但只有 `create()/to_dict()`，没有从备份严格加载的 API。

  ```python
  class MigrationManifest:
      version: int
      captured_at_ns: int
      entries: tuple[ManifestEntry, ...]
      digest: str
  ```

- `src/comfyui_mcp_skills/infrastructure/persistence/file_migration.py:209~315`（`FileMigrationRehearsal`）：公开入口只有 fresh manifest、验证、备份、dry-run；dry-run 直接重新捕获当前文件。
- `src/comfyui_mcp_skills/infrastructure/persistence/file_migration.py:317~466`（隔离演练）：只写 `test_aggregates/test_migration_switches`，明确拒绝生产 `store_migrations`；导入、计数、末次 manifest 校验和测试切换在一个 `BEGIN IMMEDIATE` 内。

  ```python
  if connection.execute("SELECT 1 FROM store_migrations LIMIT 1").fetchone():
      raise RehearsalFailure(...)
  ...
  connection.execute("INSERT INTO test_migration_switches ...")
  connection.commit()
  ```

- `src/comfyui_mcp_skills/infrastructure/persistence/file_migration.py:468~624`（源扫描/Asset 校验）：扫描 `data/assets/*.json`、`data/runs/*/{prompts,idempotency}/*.json` 和 CLI history；Asset 旧字段没有 `source_type/expires_at`。
- `src/comfyui_mcp_skills/infrastructure/persistence/file_migration.py:663~776`（Run 校验）：活跃 300 秒 reservation 阻断；重复 `(server_id,prompt_id)` 的原始 payload 不同即冲突；任何非空 `outputs` 均因缺少确定性 Artifact 定位字段而冲突。

  ```python
  if value.get("outputs"):
      raise ValueError("legacy outputs lack deterministic Artifact mapping fields")
  ```

- `src/comfyui_mcp_skills/migration_main.py:18~52`（唯一迁移 CLI）：只执行 fresh dry-run 和可选 backup；没有 manifest 输入、数据库路径、aggregate、apply/verify/status/rollback 模式。
- `src/comfyui_mcp_skills/infrastructure/persistence/control_plane.py:101~165`（schema/store migration）：`store_migrations` 支持九种 aggregate、五种状态；已切换证据不可改写/删除，每种 aggregate 最多一个 `switched`。
- `src/comfyui_mcp_skills/infrastructure/persistence/control_plane.py:270~460`（Job/Attempt/Idempotency）：三组 Job keyset 索引已存在；Job 空 Plan 绑定只允许 `legacy_migrated=1`；表中没有旧 Job 可观察的 `error`；Idempotency 要求 `claimed_at`，但没有 `lease_token`。
- `src/comfyui_mcp_skills/infrastructure/persistence/control_plane.py:461~605`（Asset/Artifact/Alias）：Asset 强制 `source_type`；Artifact 强制 node/key/index/digest；alias 表能表达旧 Asset/Job/Output 到 canonical URI，但生产读路径未使用。
- `src/comfyui_mcp_skills/infrastructure/persistence/control_plane.py:733~905`（`SQLiteControlPlaneStore`）：`initialize()` 单独提交 schema；`rollback_schema()` 在任意 switched 后永久拒绝；没有生产 store migration API。
- `src/comfyui_mcp_skills/application/control_plane_ports.py:9~76` 与 `control_plane_uow.py:14~253`（UoW）：仅暴露 G0 `test_aggregates/work_items/events/outbox`，没有 Job/Attempt/Idempotency/Asset/Artifact/Alias/StoreMigration repository。
- `src/comfyui_mcp_skills/infrastructure/persistence/runs.py:42~192`（文件 Run）：claim/save 使用 migration lock；final idempotency 文件由 Job 序列化覆盖，丢失 `claimed_at/lease_token`。
- `src/comfyui_mcp_skills/infrastructure/persistence/assets.py:27~57`（文件 Asset）：`get()` 会 `os.utime()`，因此不能作为切换后的“只读”回滚读取器。
- `comfyui_skills_cli/history_writer.py:55~221`（CLI Job/幂等）：CLI `job_id` 同时承担幂等键，scope 目前隐含 workflow；记录没有 owner，成功状态是 `success`，输出同样是扁平列表。
- `src/comfyui_mcp_skills/application/jobs.py:235~262`（旧输出）：遍历 `outputs.values()` 丢失 node id，扁平化后只保存 filename/subfolder/type/media/global URI index。
- `src/comfyui_mcp_skills/adapters/mcp/server.py:60~82`、`adapters/http/server.py:317~326`（生产装配）：仍直接实例化 FileRun/FileAsset；没有按 store state 路由。
- `src/comfyui_mcp_skills/adapters/mcp/resources.py:54~246`（Resource）：仅解析旧 server-scoped URI，不查 alias、不读 canonical URI，也不返回 `canonical_uri`。
- `src/comfyui_mcp_skills/infrastructure/persistence/retention.py:19~161`、`maintenance_main.py:12~18`：maintenance 无条件扫描/删除旧 runs/assets；切换后会破坏只读回滚证据。
- `MCP_AGENT_NATIVE_CONTROL_PLANE.zh-CN.md:1295~1324,1424~1440`：要求按 aggregate 干净切换、同 manifest 重试、确定性 Job/Artifact、expired/unknown 幂等规则、旧 URI、真实动态生图和 G1 验收。

## Report

### conclusions
- 当前实现完成 G0 证据生成与隔离演练；生产 Job/Asset 导入、生产 UoW repository、store state reader/writer、运行时路由和 URI alias reader 均不存在。
- G1 的原子切换单元应定义为：`Asset` 单元=`asset+asset aliases`；`Job` 单元=`job+execution_attempt+idempotency_record+artifact+job/output aliases`。两个单元可独立成功/失败；单元内部只能同事务提交。
- 规范 8.8 当前列出 G1 切换 Job/ExecutionAttempt/Asset/Artifact，却同时要求 IdempotencyRecord 成为唯一事实。实现前需明确 `idempotency_record` 是 Job 单元成员，并与 Job 单元使用同 version/checksum/switched_at。

#### 最小数据迁移 API（设计）
- 在 `file_migration.py` 增加严格的 `MigrationManifest.from_dict()/load(path)`；拒绝重复 key、未知字段、路径/数值预算和 digest 不一致。apply 必须读取 backup 中既有 `migration-manifest.json`，不得 fresh dry-run。
- 增加冻结数据类型：`G1Aggregate = Literal["job", "asset"]`、`G1ImportPlan(manifest, aggregate, rows, aliases, source_counts, projection_digest, conflicts)`、`G1CutoverResult(outcome, version, checksum, counts, switched_at)`。
- 增加 `FileMigrationRehearsal.build_g1_plan(manifest, aggregate)`：在 migration lock 内按 manifest 原始字节解析，所有时间 fallback 只使用对应 `ManifestEntry.mtime_ns`。
- 增加 `FileMigrationRehearsal.cutover_g1(plan, store)`：重新校验同一 manifest/source 后，通过生产 UoW 导入、校验并写 switch；不接受调用方伪造的 `ok/conflicts/counts`。
- 在 `control_plane_ports.py/control_plane_uow.py` 增加共享连接的 `jobs/attempts/idempotency/assets/artifacts/aliases/store_migrations`；repository 只执行 SQL，不自行 connect/commit/close。
- 在 `migration_main.py` 保留无参数 dry-run；apply 使用显式 `--apply --aggregate --manifest --backup --database`，backup 必须已验证；输出稳定 outcome/count/checksum/conflict JSON。
- `store_migrations.checksum` 建议定义为 versioned aggregate projection（aggregate kind、`g1-<kind>-v1`、选中 manifest entries），并在结果中同时返回父 manifest digest；相同版本不同 checksum 不得覆盖。

#### 确定性映射
- Asset：保留全部旧字段和 `asset_id`；`source_type` 固定为契约值 `legacy_upload`，`expires_at=NULL`；不得用当前 retention 配置推算过期时间；写入旧 `comfyui://assets/{server}/{asset}` alias。
- 已提交 Job：`derive_legacy_job_id(server_id,prompt_id)`；Plan/Revision/Deployment 为空，`legacy_migrated=1`；缺少 `created_at` 时使用 manifest mtime UTC，`created_at_source=legacy_file_mtime`。
- Attempt：必须新增固定 `legacy-attempt-v1` 派生 helper；迁移 Attempt 固定 `attempt=1`，保留 server/prompt/client/submission state；随机 ID 不满足跨空库重放确定性。
- Idempotency：scope=`legacy-execute:{server_id}`；MCP key 取 `idempotency_key`；CLI job history key 取旧 `job_id`，owner 为空。相同 owner/scope/key 的 workflow/request digest 不一致必须冲突，不按最后写入覆盖。
- 活跃 reserved 阻断 Job 单元；过期 reserved 写 `expired/job_id=NULL`；unknown 无 prompt 用 `derive_legacy_unknown_job_id` 并建立 unknown Attempt，保留 client，运行时禁止自动重提。
- Artifact 只能在 manifest 已含真实 node id、output key、节点内 index、filename/subfolder/type、media type 和真实 digest 时导入；旧全局 URI index只用于 alias，不能替代节点内 index。
- 同一 prompt 的 prompt/idempotency/CLI history 先归一化再比较公共事实；只有归一投影完全兼容才能合并，结构不同本身不能作为“最后写入”依据。

#### 事务与 switch 顺序
1. 获取项目 migration lock，等待所有 FileRun/FileAsset/CLI history/retention 写操作退出；锁持有到 commit/rollback 完成。
2. 严格加载既有 manifest 和 backup，验证 backup 字节与源相关 entry 均未漂移；生成冻结 plan 与 projection digest。
3. 用同一安全 SQLite 连接执行 `BEGIN IMMEDIATE`；若 G1 需要 schema v2，则 schema_migrations 的 v2 DDL/记录也在本事务应用，不能先由 `initialize()` 单独提交。
4. 校验 switch group：无 switched 方可导入；同 version/checksum 已 switched 时只做全量 DB 投影复核并返回 `already_switched`；任何不同 checksum、部分 group 或冲突 version 均失败。
5. 按 FK 顺序写：Asset→alias；或 Job→Attempt→Idempotency→Artifact→aliases。使用 select-and-compare/普通 INSERT，禁止 `INSERT OR IGNORE/REPLACE`。
6. 在事务内比较每类 ID 集、数量、owner/status/request digest/输出定位/alias 和排序后 canonical projection digest，并执行 FK 检查；随后再次验证 manifest。
7. 对 Job group 的全部 kind 或 Asset kind 写同 version/checksum/同 `switched_at` 的 `switched`，最后 commit；commit 后释放 migration lock。
8. switch-aware repository 在 migration lock 内读取路由状态：整组全 switched→SQLite；整组无 switched且状态为 absent/pending/migrating/failed→File；部分 switched、superseded 无当前版本或 checksum 不同→fail closed。进程观察 switched 后可缓存单调 SQLite 路由，禁止回退。

#### 失败、重复与 rollback
- 导入、校验、末次 manifest 检查、switch 或 commit 任一步异常：整个事务 rollback；该单元 File repository 仍是写入事实源。`failed` 可在 rollback 后用独立、同组事务作诊断记录，但不得改变路由；诊断写失败不掩盖原异常。
- 重复相同 manifest/version/checksum：逐列复核数据库对象与 alias 后返回原对象/unknown 状态，不新增行；复核不一致按数据库篡改/冲突失败。
- 失败后源未变化可复用原 manifest；源已变化则不是同一次 retry，必须生成新的审计 manifest 与 migration version，不得静默刷新旧证据。
- switched 后禁止 `switched→failed/file`、禁止旧文件写 fallback、禁止 schema down；运维 rollback 仅允许停写后恢复已验证的 SQLite 备份，或用新版本在同事务执行 `old switched→superseded + new switched`。旧文件只作限时诊断读取。

#### 生产兼容与测试矩阵
- 生产装配改为 switch-aware Run/Asset repository；Workflow Repository、gateway `/prompt`、动态 Tool、WebSocket/polling链和 Orchestrator 保持现状。HTTP Asset、MCP、CLI history、maintenance 均须使用同一 switch 判定，不能存在文件写旁路。
- URI reader 同时支持 canonical URI 和 alias；旧 Asset/Job/Output 返回原公开字段并增加 `canonical_uri`；Output blob 与 output-as-input 仍按 alias 找到同一 Artifact/Job并调用现有 `/view`。
- 单元测试：manifest strict load/reuse/mtime fallback；所有 ID tuple；Asset补字段；reserved边界；unknown/expired；重复与冲突归一；keyset 三过滤器/同时间游标/owner隔离；alias安全解析。
- 事务测试：分别在每类 insert、投影校验、switch、commit 注入失败；断言目标表和整组 switched 全有或全无；相同 checksum重复、不同 checksum、部分组、source drift、DB预存冲突均覆盖。
- 并发测试：File writer先持锁、migration先持锁、两次apply并发；commit后下一次 submit/upload 只写 SQLite且旧文件字节/mtime不变；maintenance switched 后拒绝文件删除。
- 兼容测试：Asset单独 switched/Job仍File，Job单独失败/Asset保持SQLite，Workflow/audit store rows不变；旧与 canonical URI同对象；owner拒绝跨租户。
- smoke 边界：以现有动态 workflow Tool 调真实/Fake ComfyUI，验证 `/prompt` 恰好一次、Job/Attempt/Idempotency/Artifact落库、旧 URI读取及 output复用、生图输出可下载；断言无 Orchestrator/Outbox dispatcher。

### relations
- `migration_lock.py` → FileRun/FileAsset/CLI history/retention → `cutover_g1`：同一锁形成“旧写结束—导入—commit—新路由”的顺序。
- `MigrationManifest` → deterministic ID/created_at → DB projection/store checksum：retry 的全部派生只依赖冻结证据。
- `store_migrations` → MCP/HTTP/CLI/maintenance repository factory：切换证据必须控制每个生产读写入口，而非只由迁移 CLI 写表。
- `legacy_resource_aliases` → Resource reader/ExecutionService output解析：旧 URI 是同一对象的索引，不是第二套事实源。

### result
- 最小实施顺序：先补 schema v2/字段语义和不可迁移输出证据，再补生产 UoW repositories与switch reader，再实现 manifest-load/plan/cutover CLI，最后替换全部生产装配和 URI/maintenance 旁路；每步均以文件事实源保持不变为前置条件。

### attention
- 当前旧输出缺少 Artifact 必填证据，非空输出会阻断 G1；在线临时抓取但不写入 manifest 不具备确定性。需在冻结前持久化原始 ComfyUI history与媒体 digest，或由规范批准显式 legacy-null schema；不得造 node/key/digest。
- G1 switched 后至 G4 前仍需真实动态新 Job，但当前 jobs CHECK 只允许“有完整 Plan”或“legacy_migrated=1 的空 Plan”；必须增加 `pre_g4_unbound` 之类可区分状态，不能把新运行误标为迁移历史。
- Job `error`、final idempotency `claimed_at/lease_token` 均无法从当前表无损承载；需前向 schema/回填来源字段决策，否则迁移前后查询与 fencing 语义不一致。
- 切换后任何 `FileAssetRepository.get()` 都会改 mtime；只读回滚诊断必须读取 manifest/backup原始字节，不能调用该 loader。
