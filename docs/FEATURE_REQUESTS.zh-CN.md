# 功能请求（Feature Requests）

用户请求的能力登记。状态标注：已交付 / 进行中 / 已拍板未开始 / 已放弃。

---

## 节点能力感知（Node Capability Awareness）

> 状态：**已交付**（P0-1/P0-2/P0-3、P2 可视化、P1 知识库全部完成；节点目录快照 Resource 等后续项单独标注未交付）。迭代顺序 P0-1 → P0-3 → P0-2 → P2 → P1，每段独立可验证。

### 背景
- 底层设施已齐：`CapabilitiesClient.get_object_info()`、`DiscoveryService.nodes()/node()`、`comfyui.node.list/describe`、`comfyui.model.list` 已实现。
- 问题 1（授权错位）：`node.list` / `node.describe` / `model.list` 挂在 Toolset.OPERATIONS，而修改工作流走 ADMIN Toolset（`admin.workflow.change.plan/commit`）——需要节点知识的模型默认看不到节点目录工具。
- 问题 2（盲校验）：`change.plan` 的 add_node 只查 node_id 重复与字段形状，不查 class_type 存在性、输入字段名、枚举值，错误到 validate/执行阶段才暴露。
- 问题 3（形态单薄）：`node.list` 只返回 {class, display_name, category}；`node.describe` 一次一类且需预先知道类名；缺「目标 → 相关节点 + 输入签名 + 枚举值」的聚合紧凑投影。

### P0 节点能力感知
1. **授权对齐**（最小，改两处 frozenset + 测试）：`node.list` / `node.describe` / `model.list` 的 Toolset 从 OPERATIONS 扩展到 AUTHORING。
2. **`comfyui.node.blueprint`**（**已交付**）：目标驱动的节点模板投影——关键词匹配 class/display_name/category（加权评分），紧凑签名（≤8 字段：类型 + 枚举 ≤8 项截断 + 输出 ≤4 类型），limit ≤10；fixed_tools 成员 + OBSERVE。
3. **`change.plan` 校验增强 + 失败引导**（**已交付**）：plan 阶段对照 object_info 校验（validate_api 覆盖 class_type/required/枚举/范围）；失败错误带节点/字段定位与修复路径 hint（`comfyui.node.describe <class_type>`）；`suggested_queries` 字段标记可选未做。
4. 节点目录快照 Resource（**未交付，可选/后续范围**）：`comfyui://server/{id}/nodes` 紧凑投影 + 缓存 + 订阅失效。

### P1 采样器/模型经验知识（**已交付**）
1. `model_guidance` 内置数据（9 个模型家族 → sampler/scheduler/steps/CFG/resolution 社区共识）+ `comfyui.model.guidance` 工具（关键词匹配，未知输入不猜测）。
2. `comfyui.job.history.suggest`（已交付）：从 execution_plans.resolved_inputs_json + jobs.status 统计证据（每参数值 runs + success_rate，≤20 参数 × ≤3 值，SQLite 门控）。
3. 静态知识兜底 + 历史统计推荐双通道成立。Experiment/variant.rate 评分联动标记后续可选。
4. **已知限制（多面部署）**：suggest 按调用方 principal 隔离；OMP 4 条目分面配置下（execution=local-stdio / authoring=author-a / operations=operator-a）跑图历史与建议查询跨 principal 不可见，suggest 返回空。单面部署（一个 MCP server 全工具面）无此限制。

### P2 工作流可视化（**已交付**）
1. `comfyui.workflow.visualize`：Mermaid 渲染（≤50 节点 fail-loud、节点别名防注入、边来自输入连接、标签转义）。
2. `revision.diff` 出 Mermaid 视图（after 图 + added 节点高亮 classDef）。

### 已拍板决策
- 迭代顺序：P0-1 → P0-3 → P0-2 → P2 → P1。
- blueprint 匹配：纯关键词匹配 display_name/category，不上语义匹配。
- P1 history suggest 与 P0 分开迭代。
- WebUI 排后续（可用度不高，只读 Job 查看器兜底）。

---

## 历史登记

- 2026-08-07：登记「节点能力感知」计划（P0-P2，含拍板决策）。
- 2026-08-07：P0-1/P0-2/P0-3、P2 可视化、P1 知识库、本地化 engine.history 全部已交付。
