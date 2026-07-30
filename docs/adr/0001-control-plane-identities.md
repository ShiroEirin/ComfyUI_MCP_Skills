# ADR-0001：控制平面规范身份与 Resource URI

- 状态：Accepted
- 日期：2026-07-30
- 适用阶段：G0-A 及后续全部控制平面阶段

## 背景

现有执行内核使用 `server_id + workflow_id`、`server_id + prompt_id` 和输出索引定位对象。这些上游标识无法稳定表达跨服务器 Deployment、提交结果未知、重试 Attempt、归档 Artifact 和历史兼容读取。控制平面需要独立于 ComfyUI 上游 ID 的项目级身份。

## 决策

### 1. 规范对象与 ID

控制平面定义以下规范对象：

| 对象 | ID | 说明 |
|---|---|---|
| Workflow | `workflow_id` | 项目级逻辑身份；允许现有安全 slug |
| WorkflowRevision | `revision_id` | 不可变内容版本 |
| WorkflowDeployment | `deployment_id` | Revision 在一台服务器上的部署记录 |
| ExecutionPlan | `plan_id` | 不可变执行快照 |
| Job | `job_id` | 项目级作业身份 |
| ExecutionAttempt | `attempt_id` | 一次上游提交尝试 |
| Asset | `asset_id` | 可复用输入资产 |
| Artifact | `artifact_id` | Job 生成的输出产物 |

新建对象使用 `<kind>_<uuid4 hex>`。迁移对象使用 `<kind>_<sha256 hex>`。除 Workflow 可保留现有安全 slug 外，ID 必须携带对象类型前缀，不能把一个类型的完整 typed ID 当作另一类型使用；与其他前缀相同但不构成完整 typed ID 的旧 Workflow slug（例如 `job_daily`）保持合法。

### 2. 确定性派生

迁移 ID 使用以下 canonical payload：

```text
canonical_json([kind, namespace, ...components])
```

编码规则：

- UTF-8；
- JSON 数组；
- 字符串、整数、布尔值和 null 保持类型；
- 分隔符为 `,` 和 `:`；
- 不写入无意义空白；
- namespace 必须以无前导零的正整数 `-vN` 结尾，例如 `legacy-job-v1`；`-v0`、`-v01` 和无版本 namespace 非法。
- tuple 最多 16 个 component；单个字符串最多 4096 字符，整数限定为 SQLite 可表达的有符号 64 位范围，canonical UTF-8 payload 最多 16384 字节；超限记录必须进入迁移冲突报告，不计算 ID。
- 对象专用派生入口在哈希前验证字段语义：旧 server/prompt/workflow/node/output key 使用安全标识符；Artifact 必须引用合法 `job_id`、使用非负 32 位输出索引、非空无 NUL 文件名和 `storage_type="output"`；Revision 必须引用合法 `workflow_id`；内容与请求摘要接受 64 位小写 SHA-256 hex 或 `sha256:` 前缀，但哈希前必须移除前缀，canonical tuple 永远只写 raw 64hex。

对象类型同时进入摘要，避免不同类型共享相同摘要。命名空间升级必须产生不同 ID，已经发布的命名空间不得修改语义。

确定性 ID 不是授权凭证，也不是敏感字段的脱敏机制。tuple 只能包含迁移定位所需的非秘密字段；文件名、子目录和幂等键若含个人信息、凭据或其他低熵秘密，迁移必须阻断而不是依赖 SHA-256 隐藏。日志和遥测不得跨主体传播完整私有 Resource URI。

### 3. Workflow、Revision 与 Deployment

Workflow 不属于服务器。Revision 是不可变内容。Deployment 固定绑定：

```text
workflow_id + revision_id + server_id
```

Deployment 使用 `published` 布尔状态。数据库必须保证同一 `workflow_id + server_id` 最多一个 `published=true` Deployment。publish 在一个事务内撤销旧 Deployment 并发布新 Deployment。

### 4. Job、Attempt 与上游标识

Job 使用规范 `job_id`。ComfyUI 标识只保存在 ExecutionAttempt：

```text
attempt_id
job_id
attempt
server_id
upstream_prompt_id?
upstream_job_id?
client_id
submission_state
```

G4 切换后的新 Job 必须绑定非空 Plan、Revision 和 Deployment。无法证明历史 Revision 的迁移 Job 保留可空绑定并标记 `legacy_migrated=true`，不得把当前 Workflow 伪装成历史执行快照。

### 5. 幂等身份

IdempotencyRecord 使用以下唯一键：

```text
owner_id + scope + key
```

它保存 request digest、状态、可空 `job_id`、`client_id` 和租约时间。提交结果未知时，IdempotencyRecord 可以先于上游 ID 存在。不同 request digest 使用同一唯一键时必须返回冲突。

旧文件记录的 scope 固定为 `legacy-execute:{server_id}`，保持现有 server/owner/key 唯一性。

### 6. Canonical Resource URI

规范 URI 为：

```text
comfyui://workflows/{workflow_id}
comfyui://workflows/{workflow_id}/revisions/{revision_id}
comfyui://deployments/{deployment_id}
comfyui://plans/{plan_id}
comfyui://jobs/{job_id}
comfyui://assets/{asset_id}
comfyui://artifacts/{artifact_id}
```

规范 URI 不包含 Token、宿主机路径、`server_id`、上游 prompt ID 或输出枚举位置。Workflow slug 是会出现在 Tool 与 Resource URI 中的公开标识，禁止承载 Token、凭据或个人敏感信息。

### 7. 旧 URI 兼容

以下已发布 URI 至少跨一个主版本保留只读解析：

```text
comfyui://workflows/{server_id}/{workflow_id}
comfyui://assets/{server_id}/{asset_id}
comfyui://jobs/{server_id}/{prompt_id}
comfyui://outputs/{server_id}/{prompt_id}/{index}
```

解析旧 URI 后必须通过兼容索引返回同一领域对象及 `canonical_uri`。别名不得创建第二个 Job、复制媒体或生成第二份血缘。旧标识符只允许当前系统实际发布的 ASCII 安全字符，解析器拒绝所有 percent-encoding、查询参数、fragment、路径穿越、ASCII 控制字符或空白、畸形 authority、未知 URI 形态和超过 2048 字符的输入。输出索引限定为非负 32 位有符号整数，除单独的 `0` 外不得有前导零。

## 兼容和迁移规则

- 现有 Asset 保留原 `asset_id`。
- 旧 Job 和 Artifact 使用版本化 canonical tuple 确定性派生。
- 同名且内容摘要相同的服务器工作流合并为一个 Workflow 和多个 Deployment。
- 同名但内容不同的工作流不得合并，使用服务器和旧 Workflow ID 确定性派生项目 ID。
- 若旧 Workflow slug 本身完整匹配 `workflow_<32 或 64 位小写 hex>`，不得把该源字符串直接提升为规范 ID；迁移时按冲突 Workflow 公式派生项目 ID，并只把原服务器作用域 URI 记录为旧别名，避免与新建或已迁移 Workflow 碰撞。
- 迁移 manifest 固定源文件路径、SHA-256、大小和 `mtime_ns`。
- 迁移重试必须复用相同 manifest 和 ID 算法。

## 结果

- 领域身份不再依赖 ComfyUI 上游 ID。
- Retry 和提交结果未知不会覆盖历史 Attempt。
- Workflow Revision 可以部署到多台服务器。
- 旧客户端 URI 保持可读。
- G0-B 可以围绕这些 ID 和唯一约束建立 SQLite schema。

## 非目标

本 ADR 不实现数据库、文件迁移、Revision Repository、Execution Plan 或生产执行链切换。这些内容分别属于 G0-B、G0-C、G0-D、G3 和 G4。
