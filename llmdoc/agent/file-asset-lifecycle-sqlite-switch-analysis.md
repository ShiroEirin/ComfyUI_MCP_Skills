# FileAssetRepository、Asset/Artifact 与旧 URI 调查

## Code Sections

- `src/comfyui_mcp_skills/domain/models.py:10~32` (`Asset`): 当前资产字段为 `asset_id/server_id/comfyui_ref/name/subfolder/media_type/mime_type/size_bytes/sha256/owner_id/created_at`；公开 URI 仍是服务器绑定旧 URI。

  ```python
  @property
  def resource_uri(self) -> str:
      return f"comfyui://assets/{self.server_id}/{self.asset_id}"
  ```

- `src/comfyui_mcp_skills/application/ports.py:48~50` (`AssetRepository`): 生产调用面只有 `save/get`，没有分页、删除、Artifact 或 alias 查询。

  ```python
  class AssetRepository(Protocol):
      def save(self, asset: Asset) -> None: ...
      def get(self, asset_id: str) -> Asset | None: ...
  ```

- `src/comfyui_mcp_skills/infrastructure/persistence/assets.py:19~57` (`FileAssetRepository`): `save` 以临时文件、`fsync`、`os.replace` 写 `data/assets/{asset_id}.json`；`get` 容错读取、按 dataclass 字段过滤并用 `os.utime` 更新访问时间。读操作会改源文件 mtime；ID 路径校验只要求 `asset_` 后为字母数字。

- `src/comfyui_mcp_skills/application/assets.py:55~178` (`AssetService.upload_local/get`): 授权路径→流式 staging 与 SHA-256→ComfyUI `upload_file`→构造随机 `asset_<uuid32>`→仓库保存。`original_asset_id` 只用于同服务器 image/mask 的 `original_ref`，未保存来源关系；owner 校验在 Service 中完成。

  ```python
  uploaded = gateway.upload_file(str(staged), purpose=purpose, original_ref=original_ref)
  ...
  self._repository.save(asset)
  ```

- `src/comfyui_mcp_skills/application/execution.py:174~216` (`ExecutionService._resolve_assets`): 媒体参数按 asset ID 查询，校验 server、owner、media type 后把 `comfyui_ref` 注入现有 graph；这是保持动态真实执行不变时 SQLite AssetRepository 必须兼容的读点。

- `src/comfyui_mcp_skills/application/execution.py:218~274` (`ExecutionService._resolve_output/_comfyui_output_ref`): 旧 Output URI 经 server/prompt/global index 查询 Job 输出；仅同服务器、同 owner、匹配媒体类型、`type=output` 且安全路径时注入 `"subfolder/file [output]"`，不下载。

- `src/comfyui_mcp_skills/application/jobs.py:235~262` (`JobService._outputs`): 按 history 节点字典顺序，再按 `images/gifs/audio/video` 固定顺序展开；保存 filename/subfolder/type/media/MIME 和全局旧 URI。节点 ID、输出键、键内 index、内容 digest 均未保存；未知输出键被跳过。

- `src/comfyui_mcp_skills/adapters/mcp/resources.py:54~90` (`create_resource_handlers.list_templates`): 仅声明服务器绑定 Asset 和 Output 旧模板；无 canonical Asset/Artifact 模板。

- `src/comfyui_mcp_skills/adapters/mcp/resources.py:140~246` (`_read_resource/_read_output`): Asset 旧 URI 先依赖当前 `ServerRegistry`，再按 asset ID/owner 取 JSON；Output 旧 URI按 Job 全局 index 定位并调用 `/view` 返回最多 25 MiB base64。响应不返回 `canonical_uri`，也不查询 `legacy_resource_aliases`。

- `src/comfyui_mcp_skills/adapters/mcp/server.py:60~81` (`create_server`): MCP composition root 直接实例化 FileAssetRepository，并将同一实例传给 AssetService 与 ExecutionService。

- `src/comfyui_mcp_skills/adapters/http/server.py:317~326,331~408,418~419` (`create_http_app`): HTTP upload/fetch 另建 FileAssetRepository；remote fetch 的原始 URL/来源类型未传入 Asset。嵌套 MCP `create_server` 又自行装配文件仓库，是第二个切换点。

- `src/comfyui_mcp_skills/infrastructure/persistence/retention.py:19~119` (`FileRetentionService`): Asset 生命周期终点是按 JSON 文件 mtime 删除元数据；`FileAssetRepository.get` 会延后该时间。删除前只等待没有非终态 Job，不删除 ComfyUI 端文件，也不检查 Asset 引用。

- `src/comfyui_mcp_skills/infrastructure/persistence/control_plane.py:462~509` (`assets/artifacts` schema): Asset 表比模型多必填 `source_type` 和可空 `expires_at`。Artifact 表要求 job/server/node/output key/键内 index/location/media/digest/created_at，完整 deterministic tuple 唯一且记录不可更新、不可删除。

  ```sql
  source_type TEXT NOT NULL,
  comfyui_ref TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT
  ```

- `src/comfyui_mcp_skills/infrastructure/persistence/control_plane.py:536~605` (`legacy_resource_aliases`): alias URI 主键映射 canonical URI；Asset alias 只填 asset_id，Output alias 只填 artifact_id；约束固定 canonical URI 为 `comfyui://assets/{asset_id}` 或 `comfyui://artifacts/{artifact_id}`。

- `src/comfyui_mcp_skills/domain/control_plane.py:126~168,242~303` (`derive_legacy_artifact_id/canonical_resource_uri/parse_legacy_resource_uri`): 已有确定性 Artifact ID、canonical URI 和安全旧 URI 解析；尚无生产 alias resolver 调用者。

- `src/comfyui_mcp_skills/infrastructure/persistence/file_migration.py:209~315,468~624` (`FileMigrationRehearsal`): Manifest 固定相对路径/SHA-256/大小/mtime_ns；Asset 预检要求现有 11 字段并验证 ref、owner、时间和摘要。它只 dry-run/backup，不向 `assets` 表导入。

- `src/comfyui_mcp_skills/infrastructure/persistence/file_migration.py:663~745` (`_validate_run_records`): 任一旧 run 含非空 outputs 即冲突 `legacy outputs lack deterministic Artifact mapping fields`，所以当前 G1 不能迁移有输出的 completed Job。

- `src/comfyui_mcp_skills/infrastructure/persistence/control_plane_uow.py:147~187` (`SQLiteControlPlaneUnitOfWork`): 当前只暴露 test aggregates/work items/events/outbox；无 assets/artifacts/aliases/store_migrations repository。

### Report

#### conclusions

- 当前 Asset 生命周期是“本地或 HTTP 临时文件→上传 ComfyUI input→文件元数据→Resource/执行参数读取→按最后读取 mtime 清理元数据”；没有 list、显式 delete、引用计数、Artifact promote、远端媒体删除或来源血缘。
- 当前外部 Asset URI、Output URI、Job 输出 ResourceLink 和 output 复用全部使用旧 server/prompt 形态；control-plane 规范 ID/URI/alias DDL 已存在，但运行时未接线。
- G1 的最小 Asset 应用端口仍可保持现有 `save(Asset)` 与 `get(asset_id)`；G1 不需要提前加入阶段 L 的 list/delete/collection。SQLite `save` 应为单行插入且拒绝同 ID 不同内容；迁移的“相同即复用/不同即冲突”留在同一 UoW 内的 importer，不由普通 upload 做覆盖更新。
- SQLite UoW 还需独立的 Artifact `add/get/list_for_job` 和 legacy alias `add/resolve` 能力；`list_for_job` 必须按旧全局 output index 重建兼容输出，不能按 artifact_id 哈希排序。
- Asset 读取本身不需要 UoW；Asset 创建、迁移导入及 alias/store_migrations 写入需要复用同一 SQLite 连接与事务。Artifact 与 Job 终态/旧 output alias 必须同事务落库，否则会产生可见 Job 无可读输出。

#### relations

- `mcp.server.create_server` → 同一 AssetRepository → `AssetService` 写/读及 `ExecutionService` 读；这里替换为按 `store_migrations('asset')` 选定的仓库即可保持 graph 注入与 `/prompt` 链不变。
- `http.server.create_http_app` 有独立 AssetService，且内部再调用 `create_server`；两处必须复用同一 repository composition/factory，否则一个入口切 SQLite、另一个仍写文件。
- `JobService._outputs` → `Job.outputs` → `FileRunRepository` JSON → Tool ResourceLink/Output Resource/Execution output reuse；G1 拆 Artifact 时必须维持这一旧 DTO 和顺序，同时把原始 node ID、output key、键内 index写入 Artifact。
- `legacy_resource_aliases(output)` → Artifact → Job → owner 是旧 Output URI 的授权链；不能只凭 URI 中 server/prompt 直接读 `/view`。
- `FileMigrationRehearsal` 和 FileAssetRepository 共用 project migration lock；生产 cutover 应在停止 writer 后复用该 manifest 与锁。已启动进程持有的 FileAssetRepository 不会自动感知数据库 switched 状态，因此切换边界必须包含 writer 停止与按 switched 状态重新装配。

#### result

**精确字段映射**

| 旧 Asset JSON | SQLite `assets` | 处理边界 |
|---|---|---|
| asset_id, owner_id, server_id | 同名列 | 原值；asset_id 必须为 32/64 小写 hex typed ID，缺失/冲突中止 |
| name, subfolder, comfyui_ref | 同名列 | 原值；继续验证 `comfyui_ref == subfolder/name` |
| media_type, mime_type, size_bytes, sha256 | 同名列 | 原值；SHA-256 保持 raw 64hex |
| created_at | created_at | 解析为 aware time 后规范化 UTC 文本，不从当前时间重建 |
| 无 | source_type | 必须制定迁移常量；建议明确为 `legacy_upload`，不能把 HTTP fetch/local path 等已丢失来源猜回 |
| 无 | expires_at | 只能显式置 NULL，或用“manifest mtime + 已冻结 retention policy”派生；两者生命周期语义不同，切换前需固定一项 |
| 旧 URI中的 server_id | `legacy_resource_aliases` | `comfyui://assets/{server}/{asset}` → `comfyui://assets/{asset}` |

**Artifact 缺口与映射边界**

- 规范映射必须来自原始 history：外层 key→`upstream_node_id`，集合名→`output_key`，集合内序号→`upstream_output_index`，item 的 filename/subfolder/type→位置，流式读取媒体字节→digest；旧全局 flatten index只用于 Output alias。
- 当前文件只剩扁平列表，无法证明 node ID、output key、键内 index和 digest；Artifact `created_at` 也无独立事实。不得用媒体类型、全局 index、空字符串或伪 digest补值。
- 满足现有 schema 的最小迁移路径是：切换前从对应 ComfyUI history 与 `/view` 只读取证，把原始定位和流式 digest写入版本化迁移证据并纳入同一不可变 manifest；任一 history/媒体不可用或与文件输出不一致则 Artifact aggregate 冲突且不 switched。若不允许上游取证，只能先经维护者修改 Artifact schema/迁移契约以表达 unknown，不能静默丢输出。

**最小切片与切换点**

1. 扩充 Asset 模型的 `source_type/expires_at`，将 `resource_uri` 改为 canonical；实现 SQLiteAssetRepository 的现有 `save/get` 契约，不加入阶段 L API。
2. 扩展现有 UoW，加入 assets/artifacts/legacy aliases/store migration 写端；生产 importer直接消费已捕获 manifest，不重新读取 mtime，并在一事务内校验计数、摘要、owner、引用、aliases后写对应 aggregate switched。
3. 新增唯一 composition factory，供 MCP `create_server`、HTTP upload/fetch、ExecutionService 和 maintenance 使用；Asset switched 后旧文件只读诊断，禁止 fallback 写和 mtime touch。
4. Resource handler 同时支持 canonical Asset/Artifact 与旧 alias；旧 Asset JSON返回 `canonical_uri`，旧 Output 读取返回/暴露 canonical Artifact URI。alias 解析不应先要求当前 server 配置仍存在。
5. 保持动态 Workflow catalog、graph 注入、ComfyUI gateway和 `/prompt` 路径不变；只替换 Job/Asset persistence adapter及输出 Artifact 投影，不物化 Plan/Revision/Deployment。

**测试边界**

- SQLite Asset save/get 覆盖全部字段、empty owner、NULL expiry、重复同 ID 冲突、owner Service 隔离；读取不改变 retention 时间。
- Asset manifest 重放两次对象数不变；字段/alias冲突、注入失败、manifest drift全部 rollback且 asset switched 不出现；成功后改旧 JSON不影响数据库读取且文件写被拒绝。
- canonical Asset 与旧 Asset URI读取同一 owner 数据；旧响应含 canonical_uri；错误 server/owner、未知/恶意 URI不可读；移除当前 server 配置后 alias 元数据仍可解析。
- 原始 history 中多 node、多输出键、每键多 item、`images/gifs/audio/video`分别验证 Artifact tuple、digest、全局旧 index alias和 canonical URI；未知键形成显式冲突/证据，不静默遗漏。
- canonical Artifact 与旧 Output URI读取相同字节/MIME；跨 owner、越界 index、非 output type、危险 filename/subfolder、超限 payload继续拒绝；ExecutionService 的同服务器旧 Output URI仍注入 `[output]` 且不下载。
- 使用真实配置 ComfyUI做一条动态 Tool smoke：上传 Asset→真实 `/prompt`→Job completed→旧 Output URI与 canonical Artifact均可读→旧 Output URI作为下游 output 参数复用；断言 Workflow/Plan/Revision/Deployment事实源未切换且无 Orchestrator。

#### attention

- `source_type/expires_at` 无旧事实；HTTP remote fetch 还在进入 AssetService 前丢失 URL 来源。
- Artifact schema必填 digest与确定性定位，但 manifest不含原始 history或输出字节；这是迁移 outputful Job 的当前阻断项。
- `legacy_resource_aliases` 只保存 URI字符串中的旧全局 index；Job兼容输出列表的数值排序需由 repository显式实现，不能用 alias_uri 字典序。
- 当前 canonical/legacy URI只有 domain 单元测试，MCP Resources仍只走旧路径且不返回 canonical_uri。
- maintenance 仍按文件 mtime删 Asset；直接切 SQLite 后若不更换 maintenance 路由，会继续删除只读回滚证据。
- 项目 `llmdoc/index.md` 当前不存在；本调查依据根级控制平面文档及实际源码。
