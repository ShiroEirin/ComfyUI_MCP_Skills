# Job、幂等与 ExecutionAttempt 迁移及 SQLite 切换调查

## Part 1：Evidence

### Code Sections

- `src/comfyui_mcp_skills/domain/models.py:54~66`（`Job`）：当前模型以 ComfyUI `prompt_id` 为主身份，输出、幂等信息内嵌；没有规范 `job_id`、`created_at`、Plan 绑定或 Attempt。

  ```python
  class Job:
      prompt_id: str
      server_id: str
      workflow_id: str
      status: str
      outputs: tuple[dict[str, Any], ...] = ()
      error: str = ""
      idempotency_key: str = ""
  ```

- `src/comfyui_mcp_skills/infrastructure/persistence/runs.py:42~83`（`FileRunRepository.claim`）：唯一域为 server 路径加 `sha256(owner_id + NUL + key)`；reservation 固定 300 秒，返回独立 `lease_token`。

  ```python
  active = existing.get("status") != "reserved" or time.time() - claimed_at <= 300
  if active:
      return None
  lease_token = uuid.uuid4().hex
  record = {"status": "reserved", "claimed_at": time.time(),
            "client_id": client_id, "lease_token": lease_token}
  ```

- `src/comfyui_mcp_skills/infrastructure/persistence/runs.py:91~130`（claim 状态变更）：release 以 digest+token 比较后删除无 prompt 记录；mark-unknown 以 token 比较后写 `submission_unknown`。release 未限制原状态必须为 `reserved`。

- `src/comfyui_mcp_skills/infrastructure/persistence/runs.py:142~192`（`save/get`）：有幂等键时依次写 prompt 文件与 idempotency 文件，不是单事务；状态按 `_STATUS_PRIORITY` 防止部分回退。

  ```python
  self._atomic_write(prompt_path, self._serialize(job))
  self._atomic_write(idempotency_path, self._serialize(job))
  ...
  def get(self, server_id, prompt_id): ...
  def get_by_idempotency(self, server_id, key, owner_id=""): ...
  ```

- `src/comfyui_mcp_skills/infrastructure/persistence/runs.py:204~224`（序列化）：最终记录只保留 10 个 Job 字段；`claimed_at`、`lease_token` 和文件时间不进入最终 JSON；损坏/OSError 被读取为不存在。

- `src/comfyui_mcp_skills/application/ports.py:16~45`（`RunRepository`）：当前应用端口包含 claim/get-claim/release/mark-unknown、digest、save、按 prompt 和幂等键查询；没有 list/cursor/UoW。

- `src/comfyui_mcp_skills/application/execution.py:50~143`（`ExecutionService.submit`）：先 claim，再 `/prompt`；相同 key 比较 workflow+digest，已落盘则返回，unknown 用稳定 `client_id` 查 queue/history；ServerOffline 保留 unknown，其他异常释放 claim，成功后保存 Job。

- `src/comfyui_mcp_skills/application/jobs.py:35~99`（`JobService.get`）：以 `(server_id,prompt_id)` 读快照，再以 history/queue 权威状态覆盖并保存；history 终态为 error/interrupted/cancelled/completed。

- `src/comfyui_mcp_skills/application/jobs.py:101~193`（wait/cancel）：wait 超时返回已保存 handle；运行中禁止定向 cancel；排队删除确认后写 cancelled；终态集合为 completed/error/interrupted/cancelled。

- `src/comfyui_mcp_skills/application/jobs.py:235~262`（输出序列化）：遍历 history 的 node value 和 images/gifs/audio/video，但仅保存 filename/subfolder/type/media/mime/旧 URI；丢失 node id、output key、node 内 output index。

- `src/comfyui_mcp_skills/adapters/mcp/server.py:60~81`（生产装配）：唯一生产构造点固定实例化 `FileRunRepository`，同时注入 `ExecutionService` 和 `JobService`。

- `src/comfyui_mcp_skills/adapters/mcp/server.py:154~245`（Tool 调用方）：动态 run 调 `submit`，可选调 `wait`；固定 `job.get/cancel` 仍接受 server_id+prompt_id；结果经 `job_dict`。

- `src/comfyui_mcp_skills/adapters/mcp/resources.py:140~245`（Resource 调用方）：旧 Job URI 和 Output URI 都通过 `JobService.get(server,prompt,owner)`；输出按扁平 tuple index 下载。

- `src/comfyui_mcp_skills/application/execution.py:218~249`（内部调用方）：Output URI 作为下一工作流输入时，通过内部 `JobService.get` 做 owner/server/media/type 校验。

- `src/comfyui_mcp_skills/adapters/mcp/tooling.py:16~53,102~107`（公开序列化）：`JOB_SCHEMA` 不含 `job_id/canonical_uri/created_at` 且 `additionalProperties=false`；`job_dict` 隐藏 owner/digest，但公开旧 prompt/server/idempotency/client/error/output。

- `src/comfyui_mcp_skills/infrastructure/persistence/control_plane.py:270~351`（SQLite `jobs`）：核心字段、三组 list 索引已存在；Plan 三列全空只允许 `legacy_migrated=1`；执行身份不可变，但 status 只检查非空且无状态迁移约束。

  ```sql
  CREATE INDEX ix_jobs_owner_created
  ON jobs(owner_id, created_at DESC, job_id);
  CREATE INDEX ix_jobs_owner_status_created
  ON jobs(owner_id, status, created_at DESC, job_id);
  ```

- `src/comfyui_mcp_skills/infrastructure/persistence/control_plane.py:353~460`（Attempt/幂等）：Attempt `(job_id,attempt)` 唯一且上游 ID 按 server 唯一；unknown 只允许一次补写为 submitted。Idempotency 主键为 `(owner_id,scope,key)`，状态为 reserved/unknown/resolved/expired，但表无 `lease_token`。

- `src/comfyui_mcp_skills/infrastructure/persistence/control_plane.py:481~509`（Artifact）：要求 node id、output key、upstream index、filename/subfolder/type、media 和必填 64hex digest；旧扁平 output 不能完整映射。

- `src/comfyui_mcp_skills/infrastructure/persistence/control_plane_uow.py:147~253`（UoW）：`BEGIN IMMEDIATE`、显式 commit/rollback、失败封闭已实现；目前只暴露 test_aggregates/work_items/events/outbox，没有生产 jobs/attempts/idempotency/artifacts/aliases Repository。

- `src/comfyui_mcp_skills/infrastructure/persistence/file_migration.py:663~776`（现有 manifest 校验）：严格校验路径 hash、owner、digest、300 秒 reservation 和重复 prompt 来源；任何非空 outputs 当前直接报迁移冲突。

- `src/comfyui_mcp_skills/infrastructure/persistence/retention.py:19~161` 与 `src/comfyui_mcp_skills/maintenance_main.py:12~18`：maintenance 绕过 Repository，直接扫描并删除 `data/runs`；切换后仍会修改应只读的旧事实文件。

- `MCP_AGENT_NATIVE_CONTROL_PLANE.zh-CN.md:1311~1324,1424~1440`：规定旧 Job/Artifact 确定性 ID、mtime created_at、legacy scope、unknown/expired reservation、manifest 重用、keyset list、旧 URI 和 G1 验收。

## Part 2：Report

#### conclusions

- 生产直接依赖图完整列表：stdio `__main__` 与 HTTP server 都进入 `create_server`；该装配创建一个 FileRunRepository；ExecutionService 是写入/幂等调用方，JobService 是状态读写调用方；MCP Tool、Job/Output Resource 和 ExecutionService 的输出复用是下游调用方。CLI `history_writer.py` 是另一套文件事实源，不调用 FileRunRepository，但 manifest 同时扫描它。
- 旧 prompt Job 映射：`job_id=derive_legacy_job_id(server_id,prompt_id)`；workflow/owner/status 直接映射（CLI `success` 必须归一为 `completed`）；Plan/Revision/Deployment 为空；retry_of 为空；`legacy_migrated=1`；created_at 取同一 manifest entry 的 `mtime_ns` 并固定为 UTC，source=`legacy_file_mtime`。
- 已提交 Attempt 映射：attempt=1，server/prompt/client 取旧记录，upstream_job_id=NULL，submission_state=submitted；但规范与代码都没有 legacy Attempt 的版本化确定性 namespace/helper。
- finalized idempotency 映射：owner 取源、scope=`legacy-execute:{server_id}`、key/digest/client 取源、state=resolved、job_id 指向同一 deterministic Job。最终 JSON 已丢 claimed_at，目标列却 NOT NULL，当前没有规范回填来源。
- unknown 映射：无 prompt 时用 `derive_legacy_unknown_job_id(owner,server,key,digest)`，建立 submission_unknown Job+Attempt，Idempotency 指向该 Job并保留 client；同摘要重试只能对账，不能再提交。源 lease_token 无目标列，进程重启后的旧 token CAS 无法等价实现。
- reservation 映射：manifest 时 age≤300 秒（含未来时间）阻断；过期记录变 expired、job_id=NULL，后续 claim 原子替换。该 300 秒 reservation token 不是 G5 WorkLease/fencing；当前应用无 renew，超长 `/prompt` 调用存在租约过期窗口。
- 当前 SQLite JobRepository 尚不存在；三组索引存在但只有 DDL 形状测试，没有 list 查询、cursor 编解码或生产 UoW 属性。
- list 最小契约：`list(owner_id, limit, after=(created_at,job_id)?, status?, workflow_id?) -> (items,next_cursor)`；固定 `ORDER BY created_at DESC, job_id ASC`，after 条件为 `created_at < ? OR (created_at = ? AND job_id > ?)`，limit+1 生成 opaque cursor。owner 必填；status/workflow 分别命中现有复合索引；时间必须统一固定宽度 UTC，否则 TEXT keyset 顺序不可靠。
- G1 最小端口：UoW 增加 jobs/attempts/idempotency/artifacts/aliases；JobRepository 至少提供 `add`、`get(job_id,owner)`、`get_by_upstream_prompt(server,prompt,owner)`、`transition(expected_statuses,new_status,error)`、`list(...)`。IdempotencyRepository 提供原子 claim/lookup/release/mark_unknown/resolve；Attempt 提供 add/reconcile-once；Artifact 提供 add/list_by_job。当前 RunRepository 可由 SQLite 兼容 facade 实现，保持 ExecutionService/JobService 和真实 gateway 不变。
- 原子边界：claim/过期替换为一个 UoW；成功提交必须在一个 UoW 写 Job+Attempt+Idempotency resolved+Job alias；状态完成必须在一个 UoW 写 status+Artifacts+Output aliases；任何冲突不能用捕获 IntegrityError 后继续，因为现有 `_Repository._execute` 会把 UoW 标为 failed。
- 切换点是 `adapters/mcp/server.py:create_server` 的 Repository factory，不是 WorkflowCatalog 或 gateway。迁移锁内导入、校验并写对应 store_migrations；启动时按 aggregate 选择 SQLite；switched 后 DB 不可用必须失败关闭，不能回退文件。长驻旧实例和 FileRetentionService 也必须拒绝文件写，否则 release migration lock 后仍可产生分叉。

#### relations

- `ExecutionService.submit` → `RunRepository.claim/get_claim/get_by_idempotency/save` → 当前两个 JSON；SQLite facade 应把同一语义拆到 Idempotency+Job+Attempt 且在 UoW 中提交。
- `JobService.get/wait/cancel` → `RunRepository.get/save` → history/queue；SQLite 查询需由 Attempt 的 `(server,upstream_prompt)` 反查 Job，写状态不能覆盖 Attempt 身份。
- `JobService._outputs` → Job.outputs → FileRun JSON → 旧 Output URI；目标为 Artifact+legacy alias。源缺 node/output-key/index/digest，因此现有 completed output 无无损离线映射。
- `job_dict/JOB_SCHEMA/resources.py` → 旧 DTO；规范 Job 分表后需 hydrate prompt/server/client/idempotency/output，并给旧 URI 响应增加 `canonical_uri`，同时保持 owner/digest 不公开。
- `store_migrations` → repository factory/maintenance：二者必须读取同一 aggregate cutover 事实；只切 Job/Asset，不改变 Workflow Repository、动态 Tool 列表或 ComfyUI 请求链。

#### result

- 应先写的行为测试：①同 manifest 导入 prompt+idempotency 去重且重跑 ID/计数不变；注入失败时对象与 switch 全回滚。② active reservation 阻断、expired 可被一个并发 claimant 替换、wrong/stale token 不能 release/resolve。③ unknown 同摘要重试不二次 `/prompt`，不同摘要冲突，client 对账只允许一次补写 Attempt。④并发独立连接 claim 只有一个胜者；Job+Attempt+Idempotency+alias 原子提交。⑤终态不回退且 error 文本持久化；owner 隔离覆盖 get/idempotency/list。⑥相同 created_at 多条记录跨页无重复/遗漏，status/workflow filter 与 cursor 篡改/过滤器错配被拒绝。⑦旧 Job/Output URI 返回同一对象和 canonical_uri；服务重启后仍可读。⑧switched 后动态 MCP run 仍调用 fake/contract ComfyUI `/prompt`、重复 key 只提交一次、job.get 完成落库且不再生成 run JSON。⑨stale FileRunRepository 与 maintenance 在 switched 后失败关闭。⑩输出缺确定性字段时整个 Job aggregate 迁移冲突而非猜测 Artifact。
- 当前已有测试只覆盖文件版生命周期/unknown 对账/部分终态优先级、MCP 旧 URI、schema 索引形状和 Attempt 约束；没有上述 SQLite Repository、迁移导入、keyset、生产切换或切换后真实执行契约。

#### attention

- schema `jobs` 无旧公开 `error` 列，Artifact 无 `mime_type`，Idempotency 无 lease_token；迁移前后查询无法字段等价。
- `idempotency_records(job_id)` 只有普通索引，未实现 ER 图中的至多一个 IdempotencyRecord/Job；从 Job hydrate 单个 idempotency_key 可能多义。
- G1 后至 G4 前的新动态 Job 没有 Plan，但 jobs CHECK 只允许 Plan 全空且 `legacy_migrated=1`；这会把新运行误标历史迁移，需前向 schema/字段语义决策。
- FileRun prompt+idempotency 双文件可能因崩溃只写一份或状态不同；导入必须按 `(server,prompt)` 合并并在 payload 冲突时停止。
- CLI history 的 idempotency 原作用域含 workflow 且 digest 只 hash args；FileRun 作用域为 server+owner+key 且 digest 含 workflow。统一 legacy scope 时可能出现跨 workflow key 折叠或相同 digest 假去重。
- 当前 Resource parser 仅识别 `comfyui://jobs/{server}/{prompt}`，`JOB_SCHEMA` 禁止新字段；只换 Repository 不会自动提供 canonical Job Resource。
