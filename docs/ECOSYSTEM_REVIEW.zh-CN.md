# ComfyUI MCP 生态调研与设计借鉴

> **调研日期**：2026-10
> **调研范围**：GitHub 上 8 个公开 ComfyUI MCP 项目（1131★ ~ 2★）
> **调研方法**：`gh search repos "comfyui mcp" --sort stars` 取前 25 名，从中挑选 top 3 高分项目 + 5 个具差异化设计的项目，逐份阅读 README 与工具清单
> **数据来源**：本项目侧数据（工具面分布、工作流变更能力）均为**代码实测**；竞品侧数据来自各自 README

本文档的目的：对照竞品设计，识别本项目在**产品定位**上的偏差与可借鉴的设计模式，作为后续改进的输入。

---

## 1. 调研范围

| 项目 | ★ | 工具数 | 核心设计特色 |
|---|---|---|---|
| [ATH-MaaS/Pixelle-MCP](https://github.com/ATH-MaaS/Pixelle-MCP) | 1131 | 动态（工作流即工具） | 工作流零代码转 MCP 工具；**UI 内节点标题写 `$param` 即成为工具参数**；本地 ComfyUI + RunningHub 云端双模式；配置向导；uvx/pip/docker 一键部署 |
| [artokun/comfyui-mcp](https://github.com/artokun/comfyui-mcp) | 838 | 38（action 聚合） | 全链路控制面；**HuggingFace/Civitai 模型搜索下载**；42 个模型家族 Skill；生成追踪（SQLite + 内容 hash）；实时编辑 live graph |
| [joenorton/comfyui-mcp-server](https://github.com/joenorton/comfyui-mcp-server) | 411 | ~18 | **极简**（*"Everything else is optional depth"*）；**只需 `prompt`** 即可出图；`regenerate(asset_id, overrides)` 迭代闭环；显式 job 管理 + asset identity |
| [pytraveler/local-comfyui-mcp](https://github.com/pytraveler/local-comfyui-mcp) | 15 | 64 | **WebSocket bridge 驱动浏览器里正在编辑的画布**（含未保存改动）；`describe_workflow` **自动发现工作流参数**；11 种客户端配置自动生成 |
| [yutianxiao6/comfyui-mcp](https://github.com/yutianxiao6/comfyui-mcp) | 5 | — | **one-run-only overrides**（单次运行覆盖，不落 revision）；MCP server + Agent Skill 双形态；一份核心适配 5 种 agent host |
| [xingxue-ux/good-comfyui-mcp](https://github.com/xingxue-ux/good-comfyui-mcp) | 4 | 10 | **Civitai LoRA 精确检索/下载/safetensors 头验证/SHA256 by-hash 反查**；Danbooru tag 查询；本地视觉识图；图片元数据提取（复刻链路）；实战经验库 |
| [hybridindie/comfyui_mcp](https://github.com/hybridindie/comfyui_mcp) | 3 | 26+ | **执行前危险节点扫描**（`eval`/`exec`/135 个云端付费节点，递归 subgraph，enforce 模式让用户确认）；路径净化；限流；审计脱敏；MCP Resources + Prompts |
| [miller-joe/comfyui-mcp](https://github.com/miller-joe/comfyui-mcp) | 2 | 10 + 模板注册表 | **高层语义工具命名**（`refine_image`/`upscale_image`/`generate_variations`/`generate_with_controlnet`）；npx/docker 一键；HTTP 优先 |

---

## 2. 本项目现状盘点

### 2.1 工具面分布（92 个 capability，代码实测）

| 类别 | 数量 | 占比 |
|---|---|---|
| 管理 `comfyui.admin.*`（审批 / 审计 / 配置 / 依赖 / 服务器 / 发布） | 27 | 29.3% |
| 运维 `server.*` / `queue.*` / `log.*` / `runtime.*` | 14 | 15.2% |
| 资产库 `asset.*` | 11 | 12.0% |
| 执行 / 任务 `job.*` / `execution.*` / `policy` / `route` | 11 | 12.0% |
| 实验对比 `experiment.*` | 7 | 7.6% |
| 工作流编辑 `workflow.*` / `revision.*` | 7 | 7.6% |
| 节点模型感知 `node.*` / `model.*` | 6 | 6.5% |
| 其他（capability 自身、`output.image`、`defaults` 等） | 9 | 9.8% |

按 toolset 归属（一个 capability 可属多个面）：`admin` 36 / `execution` 31 / `operations` 29 / `authoring` 22。

**结论**：管理 + 运维 + 资产 + 实验 = **59 个（64%）**；创作链路（执行 + 编辑 + 感知）= 24 个（**26%**）。

### 2.2 高频诉求现状核对

| 诉求 | 现状 | 落点 |
|---|---|---|
| 调用工作流生图 | ✅ 完整 | 动态工作流工具（每个已发布 revision → 一个工具，schema 来自 `expose_parameter` 注册的参数） |
| 快速替换模型 | ✅ 已交付 | 动态 `comfyui.run.*` 的 `_overrides` 一次性覆盖（如 `{"4.ckpt_name": "other.safetensors"}`），不修改任何 Revision；结构性改动仍走 `admin.workflow.change.plan` → `commit` |
| 替换 LoRA | ✅ 已交付 | 同上，`_overrides` 直接改 `strength_model` 等 LoRA 字段；结构性新增用 `lora_model.v1` 配方 |
| 动态加辅助节点 | ✅ 能力齐全 | 10 个操作：`add_node` / `remove_node` / `replace_node` / `set_input` / **`connect`** / `disconnect` / `expose_parameter` / `insert_subgraph` / `extract_subgraph` / `apply_recipe`；5 个内置配方：`set_scalar_input.v1` / `upscale_image.v1` / `save_image.v1` / `lora_model.v1` / `controlnet_apply.v1` |
| 质量对比 | ✅ 有 | `experiment.*`（plan / commit / variant.list / rate / promote / get / cancel） |

**诊断**：能力基本齐全，但仍有三个结构性问题——

1. **发现成本偏高**：10 个改图操作、5 个配方全部藏在 `admin.workflow.change.*` 之下
2. **结构性改动仍走重流程**：换 checkpoint / 调 LoRA 权重已由 `_overrides` 覆盖，但加节点、改连线仍需 plan → digest → commit → 发布新 revision
3. **模型知识只有磁盘事实**：`model.list(include_metadata=true)` 返回 `size_bytes` / `mtime` / safetensors `__metadata__`（含触发词 `ss_tag_frequency`），但没有 hash / 预览图 / 来源与 Civitai/HuggingFace 反查

---

## 3. 设计模式对照（20 条）

| # | 模式 | 代表项目 | 本项目 |
|---|---|---|---|
| 1 | 工作流即工具（动态 schema） | Pixelle / joenorton | ✅ |
| 2 | **UI 内 `$name` 标记暴露参数** | Pixelle | ❌ 走 plan/commit |
| 3 | **自动发现工作流参数**（`describe_workflow`） | pytraveler | ✅ `workflow.describe(suggest_parameters=true)` 返回 `parameter_suggestions` |
| 4 | **单次运行覆盖**（one-run-only overrides） | yutianxiao6 / joenorton | ✅ 动态工具的 `_overrides`（只接受标量，不落 Revision） |
| 5 | **`regenerate(asset_id, overrides)`** | joenorton / artokun | ❌ |
| 6 | **默认值系统**（`get_defaults` / `set_defaults`） | joenorton / artokun / miller-joe | ✅ `comfyui.defaults`（get / set / clear，落 `defaults.json`） |
| 7 | 看图进上下文（`view_image`） | joenorton / artokun | ✅ |
| 8 | **模型生态**（Civitai/HF 搜索·下载·hash 反查·safetensors 头验证） | xingxue-ux / artokun | ❌ |
| 9 | **图片元数据提取**（从 PNG 反推 prompt/seed/LoRA，复刻） | xingxue-ux | ⚠️ 工具存在（`comfyui.asset.metadata.extract`），但**不作为 agent 主路径**：agent 直接看图即可，不从元数据反推提示词 |
| 10 | **Agent Skill 教「怎么组合工具」** | yutianxiao6 / artokun | ❌ |
| 11 | **实时画布 bridge**（WebSocket 驱动浏览器中未保存的图） | pytraveler / yutianxiao6 | ❌ |
| 12 | **执行前危险节点扫描**（`eval`/`exec`/云端付费节点） | hybridindie | ❌ 仅有事后审计 |
| 13 | MCP Resources 暴露只读状态 | hybridindie | ✅ 部分 |
| 14 | **MCP Prompts 作为模板**（`txt2img_prompt` 等） | hybridindie | ❌ |
| 15 | 实时进度（WebSocket + HTTP 轮询 fallback） | hybridindie | ⚠️ |
| 16 | **高层语义工具命名**（按用户意图而非 ComfyUI 原语） | miller-joe / artokun | ❌ |
| 17 | 一键部署（npx / pip / uvx / docker） | 多数项目 | ❌ |
| 18 | **客户端配置自动生成**（11 种 MCP 客户端） | pytraveler | ❌ |
| 19 | 默认 LoRA 组合 + 实战经验库 | xingxue-ux | ❌ |
| 20 | 跨 host plugin 适配（Claude Code / Cursor / Gemini CLI…） | yutianxiao6 | ❌ |

**命中率 7/20**（其中 #13「MCP Resources 只读状态」为部分命中）。

---

## 4. 三条核心洞察

### ① 所有竞品都在把「改工作流」从「发布流程」变成「一次调用」

| 项目 | 做法 |
|---|---|
| Pixelle | 在 ComfyUI UI 里把节点标题写成 `$param`，导出即生效 |
| pytraveler | `describe_workflow` 自动推断参数落点 |
| yutianxiao6 | `one-run-only overrides`：提交时附带覆盖，不落 revision |
| joenorton / artokun | `regenerate(asset_id, overrides)`：按资产重跑 + 参数覆盖 |

**本项目**：结构性改动仍要 `plan` → 生成 digest → `commit` 发布新 revision；高频低风险参数已由动态工具的 `_overrides` 一次性覆盖（`{"4.ckpt_name": ...}`），不落 revision。

安全性与灵活性已有分层：`_overrides` 承接换 checkpoint / 调 steps，只有改图结构才走重流程。

### ② 竞品把「知识」与「工具」分开

yutianxiao6 的 README 说得最准确：

> *"This is what makes an installed agent know **how** to combine the MCP tools instead of seeing only a flat tool list."*

artokun 用 42 个 Skill 承载模型家族知识（采样器 / CFG / 分辨率 / 推荐下载链接）；xingxue-ux 用「复刻经验」承载实战教训（负面提示词必须一致、PNG 删除后仍可从 `/history/{prompt_id}` 取 seed、`training_` 前缀含义、LoRA 同名不同内容需 by-hash 反查）。

**本项目**：`capability.search` / `capability.describe` 是**工具目录**，不是「怎么组合」的知识。

### ③ 本项目的治理是「事后审计」，竞品中有「事前拦截」的先例

hybridindie（3★）在 README 中直接批评了同类：

> *"Every existing ComfyUI MCP server is a thin passthrough to ComfyUI's API with no security guardrails. They allow arbitrary workflow execution (including malicious custom nodes that run `eval`/`exec`)..."*

它的 **Workflow Inspector** 在**执行前**解析工作流：提取节点类型、标记 `eval`/`exec`/`__import__`/`subprocess`、递归进 subgraph 防止嵌套绕过、标记 135 个云端付费 API 节点（成本 + 网络出口告警），enforce 模式下**让用户确认后才提交**。

**本项目**：审计 / 审批覆盖了「谁做了什么」，但缺少「提交前扫描工作流是否含危险节点」这一层。

---

## 5. 组合方案（目标工具面）

```
Tier 0 · 补齐地基
  comfyui.workflow.params   参数暴露三选一：UI 内 $name 标记 / 自动发现（已交付：workflow.describe suggest_parameters）/ 显式 expose
  comfyui.defaults          get / set 默认参数（已交付：comfyui.defaults）

Tier 1 · 创作闭环
  comfyui.generate          workflow_id? + prompt? + overrides? + execute
  comfyui.view              看图 / 列资产 / 资产元数据（已交付：output.image / asset.list / asset.metadata.extract）
  comfyui.regenerate        asset_id + overrides → 重跑（overrides 已交付为动态工具 `_overrides`，按 asset_id 重跑仍缺）
  comfyui.image.inspect     从图片元数据反推 prompt/seed/LoRA（已交付：asset.metadata.extract）

Tier 2 · 工作流（作者）
  comfyui.workflow.edit     现有 10 个 op + plan/commit 合并为「一次调用 + dry_run」（已部分交付：change.commit 内联模式 + dry_run）
  comfyui.workflow.list / describe / visualize

Tier 3 · 模型与知识
  comfyui.model.catalog     本地模型 + hash + 大小 + 触发词（大小/触发词已交付：model.list include_metadata）
  comfyui.model.search / download   Civitai / HuggingFace + safetensors 头验证
  Skill: comfyui-workflow   教 agent「怎么组合工具」（不进 MCP，零架构成本）

Tier 4 · 治理（保留 + 补事前拦截）
  现有 admin / audit / provisioning / experiment
  + 执行前危险节点扫描（eval / exec / 云端付费节点）
```

**不建议做**：削减 admin / audit / provisioning —— 这是本项目相对竞品的差异化护城河（竞品全都没有企业级授权分面、审计、审批、依赖管理、revision 契约），只是不应占据 64% 的工具面注意力。

---

## 6. 落地顺序建议

| 顺序 | 内容 | 理由 |
|---|---|---|
| P0 | `comfyui.regenerate`（按 `asset_id` 重跑） | `_overrides`（一次性覆盖）已交付；仍缺按资产重跑的聚合工具，底座（`asset.*` + `job.*` + `execution.*`）已具备 |
| P1 | `comfyui.defaults` | **已交付**（get / set / clear，`defaults.json` 持久化） |
| P2 | `comfyui.workflow.params`（快捷参数暴露） | 自动发现部分已由 `workflow.describe(suggest_parameters=true)` 提供；UI 内 `$name` 标记仍缺 |
| P3 | `comfyui.image.inspect` + `model.catalog` | 复刻能力已由 `asset.metadata.extract` 提供；`model.catalog`（hash / 预览 / 来源）仍缺 |
| P4 | `model.search` / `model.download` | 补模型生态，工作量最大 |
| P5 | 执行前危险节点扫描 | 补治理短板 |
| P6 | 工具聚合（92 → 更少，action 参数化） | 收益大但重构风险高，最后做 |

---

## 附录 A：竞品详细笔记

### A.1 Pixelle-MCP（1131★）

- **工作流 → 工具零代码**：把工作流 API JSON 交给它，自动转成 MCP 工具
- **参数标记**：在 ComfyUI UI 里把节点标题写成 `$image.image!`，导出后该字段自动成为工具参数
- 双执行模式：本地 ComfyUI / RunningHub 云端（无本地 GPU 也能跑）
- 统一架构：MCP server + Chainlit Web UI + 文件服务 + 工作流引擎
- 部署：`uvx pixelle@latest` / `pip install -U pixelle` / docker compose
- 首次启动配置向导（执行引擎 + LLM）

### A.2 artokun/comfyui-mcp（838★）

- 38 个 action 参数化工具（`generate_image` / `create_workflow` / `get_workflow` / `get_image` / `get_history` / `download_model` / `list_packs` / `queue` / `visualize_workflow` / `comfy_cli` 等）
- `generate_image` 一个入口覆盖 image / controlnet / ip_adapter / video / 3d / upscale / 抠图 / audio
- `download_model`：HuggingFace + Civitai 搜索 / 下载 / 状态 / 取消；`list_local_models` 按类型列出并支持删除、路径编辑
- 42 个 Skill：模型家族指南（Flux / WAN / LTX / Qwen / Z-Image / Ideogram / ERNIE / ANIMA）、模型注册表、节点编写、启动/性能参数矩阵
- Generation Tracker：每次 `enqueue_workflow` 落 SQLite，按**内容 hash（AutoV2 / SHA256）**识别模型与 LoRA（改名后仍能归组），提供 `suggest` 推荐参数
- 另附 ComfyUI 侧边栏 Agent Panel（独立仓库，129★）

### A.3 joenorton/comfyui-mcp-server（411★）

- ~18 个工具，核心理念：*"You run the server, connect a client, and issue tool calls. Everything else is optional depth."*
- 工具分组：Generation（`generate_image` / `generate_song` / `regenerate`）、Viewing（`view_image`）、Job（`get_queue_status` / `get_job` / `list_assets` / `get_asset_metadata` / `cancel_job`）、Config（`list_models` / `get_defaults` / `set_defaults`）、Workflow（`list_workflows` / `run_workflow`）、Publish（`get_publish_info` / `set_comfyui_output_root` / `publish_asset`）
- **只需 `prompt`**，其余参数走服务端默认
- 明确的设计演进自述：*"Earlier versions were a thin request/response bridge. The current version is built around **iteration** and **stateful control loops**."*
- 自定义工作流放 `workflows/` 目录即自动发现为工具

### A.4 pytraveler/local-comfyui-mcp（15★，64 tools）

- **bridge node**：把 ComfyUI 已对每个客户端保持的 WebSocket 变成请求/响应通道，从而**读写浏览器里正在编辑的画布**（litegraph，含未保存状态）
- 能力：读 live graph、改 widget 值、**重连线**、整理布局、截图画布、以「Queue 按钮」的方式排队
- 反向通道：把问题显示在画布**下方**（不遮挡图）等用户回答
- `describe_workflow`：自动找出工作流接受哪些参数以及它们的落点
- 安装脚本化：`install.bat` / `configure_comfy` / `configure_clients`（生成 11 种客户端的配置）/ `install_node`
- 工具会明确说明「缺哪一步设置」而不是含糊失败

### A.5 yutianxiao6/comfyui-mcp（5★，alpha）

- **one-run-only overrides**：提交工作流时附带一次性覆盖，不修改已保存的图
- **MCP server + Agent Skill 双形态**，一份 Python 核心 + 5 种 host 的 plugin manifest（Codex / Claude Code / Gemini CLI / Cursor / GitHub Copilot CLI）
- `skills/comfyui-workflow/SKILL.md` 教 agent 如何组合工具
- 架构上有意分离「执行后端」与「可视化画布」：API 提交不修改已打开的浏览器画布，可选扩展负责在画布上重建节点
- 图编辑：增删节点、设字面输入、连接/断开边、本地检查 + 对 live 实例校验

### A.6 xingxue-ux/good-comfyui-mcp（4★）

- 10 个工具：`lookup_character_tags`（Danbooru 规范 tag，30 天缓存）/ `lookup_character_appearance` / `generate`（双引擎，带 `reference_image` 自动生成对比页）/ `extract_image_info`（PNG/JPEG 元数据解析）/ `describe_image`（本地 Ollama 识图）/ **`search_lora`（Civitai 网页端点穷举搜索，比 API 全）** / **`download_lora`（下载 + safetensors 头验证）** / **`lookup_lora_hash`（本地 SHA256 → C 站 by-hash 反查）** / `deconfuse_image` / `list_cached_characters`
- 默认 LoRA 五件套（`DEFAULT_LORAS`，含权重与用途）
- **复刻实战经验**（文档中列出）：提示词一字不改（含负面）；PNG 删除后仍可从 `/history/{prompt_id}` 取回 seed；`training_` 前缀 = Civitai 私有训练；LoRA 同名不同内容需 by-hash 反查；触发词变体（`kan2` → `@kan2arin` → `Kanzarin`）

### A.7 hybridindie/comfyui_mcp（3★）

- 五层安全：Workflow Inspector（执行前危险节点扫描，递归 subgraph，标记 135 个云端付费节点，enforce 模式用户确认）/ Path Sanitizer（路径穿越、null 字节、百分号编码、绝对路径、扩展名白名单）/ SecurityMiddleware（限流 + 入口审计）/ Audit Logger（结构化 JSON + 敏感字段脱敏）/ Selective API Surface（`/userdata`、`/free`、`/users` 永不代理）
- MCP Resources：`comfyui://models/{folder}`、`comfyui://nodes/installed`、`comfyui://queue`、`comfyui://system`、`comfyui://settings`
- MCP Prompts：`txt2img_prompt`、`img2img_prompt`、`inpaint_prompt`、`upscale_prompt`
- 实时进度：WebSocket 跟踪（step / 当前节点 / 输出），失败自动回落 HTTP 轮询
- 结构化输出：Pydantic `Field` 约束（`steps: 1-100`、`cfg: 1.0-30.0`、`width: 64-4096`）+ 26 个工具的 `outputSchema`
- 长任务可选后台任务（Docket）

### A.8 miller-joe/comfyui-mcp（2★）

- 工具按**用户意图**命名：`generate_image` / `generate_variations` / `generate_with_workflow` / `refine_image` / `upscale_image` / `list_models` / `list_workflows` / `upload_image` / `generate_with_controlnet` / `generate_with_ip_adapter` + 工作流模板注册表
- 配置极简：`COMFYUI_URL` + `COMFYUI_DEFAULT_CKPT`
- 部署：`npx` / `npm -g` / docker
- 传输：默认 Streamable HTTP，`--stdio` 可选；`COMFYUI_PUBLIC_URL` 处理 Docker 网络下的图片 URL 可达性

---

## 附录 B：调研方法

```bash
# 1. 按 star 排序搜索
gh search repos "comfyui mcp" --sort stars --limit 25 \
  --json fullName,stargazersCount,description,updatedAt

# 2. 阅读 README（按 star 与差异化筛选）
gh repo view <owner>/<repo>
```

**选样策略**：top 3 高分项目 + 从剩余项目中挑选「设计上有明确差异化」的 5 个（浏览器画布 bridge、LoRA 生态、安全治理、双形态 Skill、语义化命名），避免只看向头部造成幸存者偏差。

**局限**：本调研仅基于 README 与工具清单，未逐份阅读源码实现；竞品功能描述以其自述为准，未经独立验证。
