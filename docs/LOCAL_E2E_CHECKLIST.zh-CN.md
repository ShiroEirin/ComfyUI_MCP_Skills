# 本地 ComfyUI 直通 · 端到端验证清单

> 目的：在一台真实的本地 ComfyUI 上，按顺序验证「MCP ↔ ComfyUI 直通」与「agent 自由操作工作流」是否真的可用。
> 每条都给出**工具调用**与**预期结果**；失败时看文末「故障排查」。
> 适用版本：>= `2fe5190`。

---

## 0. 前置条件

| 项 | 要求 | 说明 |
|---|---|---|
| ComfyUI | 本地运行，默认 `http://127.0.0.1:8188` | 先确认浏览器能打开 ComfyUI |
| server 条目 | 配置 `url`；**可选** `local_root` | `local_root` 缺失只影响 `model.list(include_metadata)` 与 `local.plugins`，其余功能不受影响 |
| Toolset/Scope | `COMFYUI_MCP_TOOLSET=authoring` + `COMFYUI_MCP_SCOPES=comfyui:observe,comfyui:author` | 只有 authoring 面能做工作流变更（`change.plan/commit`、`output.image`） |
| 数据目录 | `COMFYUI_MCP_DIR` | 默认值文件 `defaults.json` 落在这里（也可用 `COMFYUI_MCP_DEFAULTS_PATH` 指定） |
| 工作流 | 至少 1 个已发布 Revision + Deployment | 动态执行工具由发布状态生成 |

> 提示：`comfyui.capability.search` / `capability.describe` 可在任何面使用，用来确认某个工具在你当前权限下是否可见。
>
> **跨面说明（重要）**：Toolset 是**互斥的授权面**，一个 MCP 条目只暴露一个面的工具。authoring 面（`comfyui:observe,comfyui:author`）不暴露动态 `comfyui.run.*`，execution 面（默认 `local-stdio` + `comfyui:execute`）不暴露 `change.plan/commit`、`workflow.describe/visualize`、`output.image`。所以本清单要按顺序跑完，需要**两条指向同一数据目录的 MCP 条目**（一条 authoring、一条 execution），在对应步骤切换到相应条目；不要在单个条目里找齐全部工具，那是设计如此而不是配置失败。

---

## 1. 连接与服务器

| 步骤 | 调用 | 预期 |
|---|---|---|
| 1.1 | `comfyui.server.list` | 返回已启用的 server 条目（不含凭据/私有 URL） |
| 1.2 | `comfyui.server.health(server_id="local")` | 在线，附带运行时设备信息 |
| 1.3 | `comfyui.server.capabilities(server_id="local")` | 各可选能力的探测状态 |

**判定**：1.2 在线即说明 HTTP 直通成立。

---

## 2. 节点与模型发现

| 步骤 | 调用 | 预期 |
|---|---|---|
| 2.1 | `comfyui.node.list(server_id="local", query="KSampler")` | 命中节点类（分页游标） |
| 2.2 | `comfyui.node.describe(server_id="local", node_class="KSampler")` | 完整输入签名（字段/枚举/必填） |
| 2.3 | `comfyui.model.list(server_id="local", kind="checkpoints")` | checkpoint 文件名列表 |
| 2.4 | `comfyui.model.list(server_id="local", kind="loras", include_metadata=true)` | 每个文件 `size_bytes` / `mtime` / safetensors `__metadata__`（含 `ss_tag_frequency`） |
| 2.5 | （无 `local_root` 时）2.4 | `{"available": false, "reason": "no_local_root"}` —— **这是降级，不是故障** |

**判定**：2.2 能拿到 KSampler 的 `steps`/`cfg`/`seed` 等字段，2.4 能读到 LoRA 元数据（若配了 `local_root`）。

---

## 3. 工作流与参数建议

| 步骤 | 调用 | 预期 |
|---|---|---|
| 3.1 | `comfyui.workflow.list(server_id="local")` | 已发布工作流列表 |
| 3.2 | `comfyui.workflow.describe(workflow_id="<id>", server_id="local")` | Revision + Deployment 摘要 |
| 3.3 | `comfyui.workflow.describe(workflow_id="<id>", server_id="local", suggest_parameters=true)` | 额外返回 `parameter_suggestions`：每个建议含 `node_id` / `class_type` / `field` / `value` / `suggested_name` |
| 3.4 | `comfyui.workflow.visualize(workflow_id="<id>", server_id="local")` | Mermaid 流程图 + `node_count` |

**判定**：3.3 应该能列出 checkpoint、LoRA 权重、sampler 参数、latent 尺寸这类字段；**已连线**的输入与**已暴露**的字段不会出现。

---

## 4. 参数暴露（结构变更 · 需 author 权限）

| 步骤 | 调用 | 预期 |
|---|---|---|
| 4.1 | `comfyui.admin.workflow.change.commit(server_id="local", workflow_id="<id>", operations=[{"op":"expose_parameter","node_id":"4","field":"ckpt_name","name":"checkpoint","required":false}], dry_run=true)` | 返回 `plan_id` / `plan_digest` / diff，**不产生** `deployment_id` |
| 4.2 | 去掉 `dry_run` 重跑 | 返回 `deployment_id`（未发布 Revision） |
| 4.3 | 用 `comfyui.admin.workflow.publish(deployment_id="<上一步>")` | `published: true`，动态执行工具的参数里出现 `checkpoint` |

**判定**：4.1 返回计划且**未提交**；4.3 之后工作流的工具 schema 多出 `checkpoint` 参数。

> 互斥校验：`operations` 不能与 `plan_id`/`plan_digest` 混用；`dry_run` 只能配 `operations`。两者都会**明确报错**，不会静默忽略。

---

## 5. 执行与一次性覆盖（核心）

> 本节只在 **execution** 面可见（动态 `comfyui.run.*` 由已发布 Deployment 生成）；在 authoring 条目里看不到这些工具是正常的，切到 execution 条目再调用。

| 步骤 | 调用 | 预期 |
|---|---|---|
| 5.1 | 动态工具（名称形如 `comfyui.run.<server>.<workflow>`，如 `comfyui.run.local.txt2img`）`{"prompt": "a cat"}` | 返回 Job（`prompt_id`） |
| 5.2 | 动态工具 `{"prompt": "a cat", "_overrides": {"4.ckpt_name": "other.safetensors"}}` | 同上，但**本次**用另一个 checkpoint，**不修改任何 Revision** |
| 5.3 | 动态工具 `{"prompt": "a cat", "_overrides": {"10.strength_model": 0.5}}` | 调 LoRA 权重，同样不落 Revision |
| 5.4 | `_overrides` 指向不存在的节点/字段 | **报错**（不静默忽略） |
| 5.5 | `_overrides` 传数组/对象（想改连线） | **报错**（只接受标量；改连线请走 `change.commit` 的 `connect`） |

**判定**：5.2 / 5.3 是「换模型 / 换 LoRA」的一步式通道；5.4 / 5.5 必须报错。

---

## 6. 生成默认值

| 步骤 | 调用 | 预期 |
|---|---|---|
| 6.1 | `comfyui.defaults(action="get")` | `{"values": {...}, "path": "<文件路径>"}` |
| 6.2 | `comfyui.defaults(action="set", values={"steps": 30, "cfg": 7})` | 原始原子写入，返回规范化后的 `values` |
| 6.3 | 动态工具只传 `{"prompt": "a cat"}` | `steps`/`cfg` 自动补齐（仅对**该工作流声明过**的参数生效） |
| 6.4 | 手工把 `defaults.json` 改成非法 JSON，再执行 6.3 | **生图仍然成功**（默认值被忽略）；`comfyui.defaults(action="get")` **报错**指出文件有问题 |
| 6.5 | `comfyui.defaults(action="clear")` | `values` 清空，文件删除 |

**判定**：6.4 是关键 —— 一个坏配置文件不能拖垮整个 MCP。

---

## 7. 产出与图片

| 步骤 | 调用 | 预期 |
|---|---|---|
| 7.1 | `comfyui.job.get(server_id="local", prompt_id="<id>")` | Job 状态 + 输出资源链接（execution 面） |
| 7.2 | `comfyui.output.image(server_id="local", filename="<输出文件名>")` | 默认返回**标准 MCP 图片块**（宿主渲染/落盘/去重）；若设了 `COMFYUI_MCP_IMAGE_TAG_DIR`，改返回 `@@image:upload/<日期>/<名>@@` 路径标签（authoring 面）。MIME 由图片内容嗅探得出，扩展名写错也会返回真实类型；非图片内容会被拒绝 |
| 7.3 | `comfyui.asset.list(limit=5)` | 最近资产（用于串联复刻/元数据） |
| 7.4 | `comfyui.asset.metadata.extract(asset_id="<id>")` | `recovered_parameters` + 可选匹配到的 `revision`（用于复刻） |

---

## 8. 复刻闭环（可选）

| 步骤 | 调用 | 预期 |
|---|---|---|
| 8.1 | 7.4 拿到 `recovered_parameters` 与 `revision` | 能反推 prompt/seed/采样参数 |
| 8.2 | 用 `revision` 对应的动态工具 + `_overrides` 重跑 | 复刻并只改动指定字段 |

---

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 工具列表里没有 `change.plan/commit`、`output.image` | 当前不在 **authoring** 面或缺 `comfyui:author` | 设 `COMFYUI_MCP_TOOLSET=authoring` + `COMFYUI_MCP_SCOPES=comfyui:observe,comfyui:author` |
| 工具列表里没有 `defaults` | 不在 **execution** 面 | `defaults` 的 scope 是 `comfyui:execute` |
| `model.list(include_metadata=true)` 返回 `no_local_root` | server 条目未配 `local_root` | 补上 ComfyUI 安装根目录 |
| `change.commit` 报 `INVALID_ARGUMENTS` 且提示混用 | 同时传了 `operations` 与 `plan_id` | 二选一 |
| `_overrides` 报「missing input」 | 字段名写错或该节点无此输入 | 用 `workflow.visualize` + `node.describe` 核对 |
| 生图成功但参数没生效 | 该参数未被工作流声明（未 expose） | 先用 `suggest_parameters` 找，再 `expose_parameter` + publish |
| `output.image` 返回 `Unsafe output media locator` | filename/subfolder 含路径穿越字符 | 只传服务端相对文件名 |

---

## 附：本清单对应的能力提交

| commit | 能力 |
|---|---|
| `fabd59e` | `_overrides` 一次性节点覆盖 |
| `6e53114` | `comfyui.defaults` 生成默认值 |
| `d827f11` | `change.commit` 内联模式（一次调用 plan+commit） |
| `cf5e207` | `model.list` 磁盘元数据 |
| `9826191` | review 修复（互斥校验 / 默认值降级 / 符号链接） |
| `2fe5190` | `workflow.describe(suggest_parameters=true)` 参数建议 |
