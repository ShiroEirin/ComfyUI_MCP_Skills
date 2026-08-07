# 功能请求（Feature Requests）

用户请求的能力登记。状态标注：已交付 / 进行中 / 已拍板未开始 / 已放弃。

---

## 节点能力感知（Node Capability Awareness）

> 状态：**已拍板方向，未开始实现**。迭代顺序 P0-1 → P0-3 → P0-2 → P2 → P1，每段独立可验证。

### 背景
- 底层设施已齐：`CapabilitiesClient.get_object_info()`、`DiscoveryService.nodes()/node()`、`comfyui.node.list/describe`、`comfyui.model.list` 已实现。
- 问题 1（授权错位）：`node.list` / `node.describe` / `model.list` 挂在 Toolset.OPERATIONS，而修改工作流走 ADMIN Toolset（`admin.workflow.change.plan/commit`）——需要节点知识的模型默认看不到节点目录工具。
- 问题 2（盲校验）：`change.plan` 的 add_node 只查 node_id 重复与字段形状，不查 class_type 存在性、输入字段名、枚举值，错误到 validate/执行阶段才暴露。
- 问题 3（形态单薄）：`node.list` 只返回 {class, display_name, category}；`node.describe` 一次一类且需预先知道类名；缺「目标 → 相关节点 + 输入签名 + 枚举值」的聚合紧凑投影。

### P0 节点能力感知
1. **授权对齐**（最小，改两处 frozenset + 测试）：`node.list` / `node.describe` / `model.list` 的 Toolset 从 OPERATIONS 扩展到 AUTHORING。
2. **`comfyui.node.blueprint`**（核心新增）：目标驱动的节点模板投影——输入自然语言目标 + server_id；输出按 display_name/category 匹配的相关节点紧凑签名（输入字段名 + 类型 + 枚举值截断有界 + 输出类型）；硬性体积上限（≤10 节点 × ≤8 字段）。匹配策略：**纯关键词匹配**（已拍板）。
3. **`change.plan` 校验增强 + 失败引导**（**已交付**）：plan 阶段对照 object_info 校验（validate_api 覆盖 class_type/required/枚举/范围）；失败错误带节点/字段定位与修复路径 hint（`comfyui.node.describe <class_type>`）；`suggested_queries` 字段标记可选未做。
4. 节点目录快照 Resource（可选加分）：`comfyui://server/{id}/nodes` 紧凑投影 + 缓存 + 订阅失效。

### P1 采样器/模型经验知识
1. 内置 `model_guidance` 数据文件：模型家族 → 推荐 sampler/scheduler/steps/CFG/resolution（社区共识，Resource 提供）。
2. `comfyui.job.history.suggest`（差异化）：从持久化 Job 历史统计成功组合（completed + 高评分），与 Experiment/variant.rate 评分联动；静态知识兜底 + 历史统计推荐双通道。
3. **单独迭代**（已拍板：不混入 P0；依赖 SQLite run aggregate，workspace 已 cutover）。

### P2 工作流可视化
1. `comfyui.workflow.visualize`：Mermaid 渲染（有界节点数）。
2. `revision.diff` 出 Mermaid 对比视图（差异化重点）。

### 已拍板决策
- 迭代顺序：P0-1 → P0-3 → P0-2 → P2 → P1。
- blueprint 匹配：纯关键词匹配 display_name/category，不上语义匹配。
- P1 history suggest 与 P0 分开迭代。
- WebUI 排后续（可用度不高，只读 Job 查看器兜底）。

---

## 历史登记

- 2026-08-07：登记「节点能力感知」计划（P0-P2，含拍板决策）。
- 2026-08-07：P0-1 授权对齐、P0-3 change.plan 校验引导、本地化 engine.history 已交付。
