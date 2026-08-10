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

## 装配分层（架构原则，GA 前置）

> 状态：**已交付**（2026-08-08，`23c0c52`）。任务 1 引导文档 `docs/LIGHTWEIGHT.zh-CN.md`（分层说明 + 本地 5 分钟上手 + 双条目配置）+ INSTALLATION:30 旧说明修正 + README 链接；任务 2 入口懒初始化（`__main__.py`/`http_main.py` 删除无条件 initialize，fresh 不建 control-plane.sqlite3、既有 DB 由 repository bundle 完整初始化；`http_main` 保留 `_ensure_data_dir` 于 `_with_shared_store` 之前保障 external 限流；编排 worker 门控零改动）。回归 10 测试（stdio/HTTP fresh 不建库含 process/external、all-file fail-closed 编排接入 + fresh 负向 + job cutover、真实 provisioning 三单步推进含事件增量 +0/+1/+1、g5 outbox owner 解析投递）。诊断记录：本地 stdio 入口在 create_server 前无条件初始化 SQLite 控制面（80+ 表）+ 编排仓库 + 订阅总线，核心层（本地单用户）被云端增强层包裹；分层顺序应为核心层默认、增强层按需装配。

### 原则
- **核心层（本地默认路径）**：run 动态工具直连 `/prompt`、`engine.history` 看历史、`local.plugins` + node 目录写工作流、本地文件/轻量存储。
- **增强层（云端可选装配）**：幂等/恢复/retry lineage、审计闭环/owner 隔离、路由/审批/多租户/实验矩阵、cutover 迁移治理。
- **装配规则（方案审查确认）**：轻量仅限 fresh（control-plane.sqlite3 不存在，store=None）；任何已存在数据库（含 all-file 未 cutover）保持完整初始化 + fail-closed（已初始化 all-file DB 有 provisioning/experiments 消费者与编排 worker，走轻量会停摆持久工作）。订阅总线拆分：目录变更通知（ToolsListChanged/ResourcesListChanged）是核心契约保留；OrchestrationRuntime worker/outbox 按 SQLite 可用性门控。

### 任务
1. **本地轻量引导文档**（零代码）：分层说明 + 本地 5 分钟上手（operations 单条目：OBSERVE principal/scopes/toolset + `COMFYUI_MCP_ENABLE_HIGH_RISK=1`，覆盖观察/节点/插件/历史；跑图需 execution 第二条目——单条目不暴露动态工作流）；同步修正 INSTALLATION "立即初始化控制平面"旧说明。
2. **入口懒初始化**（代码）：`__main__.py`/`http_main.py` 按 DB 是否存在决定初始化；fresh 不建 control-plane.sqlite3、既有 DB 完整升级；编排 worker 门控。回归：fresh 无库、all-file pending provisioning/outbox 推进、混合/全 SQLite 升级、1029 通过/8 跳过基线。

---

## 1.0 GA 路径（Beta → 正式）

> 状态：**承诺门槛已全部交付，进入 1.0 GA 发布评估**。装配分层（前置）、G1（schema 冻结）、G2（重启执行闭环）、G3（Windows Service RuntimeController）均已交付；发布评估项（schema 冻结承诺已兑现、重启闭环已交付、适配器三平台齐备）不再有功能性 blocker。当前 1.1.0 Beta，正式 GA 需发布流程决策（版本号/发布渠道/迁移文档）。

### G1. 持久化 schema 冻结承诺（最高优先级）
> 状态：**已交付**（2026-08-08）。`RELEASED_SCHEMA_MIGRATIONS` 冻结清单（v1–v13 的 version/name/checksum）+ initialize 前缀一致 fail-fast 校验（改名/改 SQL/重排/删头部拒绝；追加新迁移允许）；参数化跨版本升级回归套件（`tests/test_schema_freeze.py`：按 `RELEASED_SCHEMA_MIGRATIONS` 动态覆盖每个冻结版本（当前 v1..v13）→ 当前，schema 完整 + 基础数据保留 + 幂等；篡改拒绝/追加允许/未来库被旧代码 fail-loud 拒绝）；README 移除「不保证兼容」声明并写明冻结与单向升级契约（向后=新代码升级任一冻结历史前缀，向前=新 schema 被旧代码显式拒绝、不承诺降级、备份恢复）；INSTALLATION 同步措辞。尾部删除（缩减发布版本）由冻结清单测试拦截（`len(_MIGRATIONS) >= len(RELEASED)` 断言）。
- **问题**：README 明示"Beta 阶段不保证持久化 schema 永久兼容；升级前应备份"——数据层无冻结承诺，升级需备份重迁，生产不敢用。
- **现状**：已有 `store_migrations` + cutover + fencing 基础设施（迁移治理能力在），缺的是**冻结承诺 + 长期升级验证**。
- **交付**：schema 版本化冻结；向前/向后兼容保证；从旧版本一键升级路径验证（迁移回归套件覆盖历史版本 → 当前版本）。
- **验收**：文档移除"不保证兼容"声明；CI 有跨版本升级测试。

### G2. 重启执行闭环（approve/commit + drain/fence）
> 状态：**已交付**（2026-08-08）。`runtime.restart.plan`（server-wide 跨 owner 非终态快照 + 明细表 + 规范化 digest + 单次审批 approval_id）→ `runtime.restart.approve` → `runtime.restart.commit`（receipt-first 幂等/失败重放重抛、controller binding digest 漂移拒绝、原子 drain 开 fence + execution intent 预持久化、有界 admission 结算等待、per-server active 唯一索引串行、失败解除 fence）→ `runtime.restart.get`。fence 原子门下沉 SQLite 幂等 claim 事务（含无幂等键路径的 admission gate + finally 释放）；崩溃恢复 Recoverer（启动清理 admission + 遗留 draining/restarting 置 failed 不自动重试，单实例前提）；v13 迁移三表 + 冻结清单 append；门控：approve/commit/get 仅 SQLite run store，plan 在文件后端/fresh 返回只读预览。24 测试（状态机/串行/崩溃恢复/admission 生命周期/门控/MCP 集成/分页无遗漏/错误重放同构/时钟一致性/边界 10000）。
- **原问题（已解决）**：`runtime.restart.plan` 只读预览、执行闭环缺失（无影响快照/单次审批/drain-fence 协调）。
- **已交付**：服务端持久化影响快照 + 单次审批 + 所有提交路径入队前检查 + 原子启用/解除（drain/fence）；`runtime.restart.commit` 在审批后执行固定重启命令。
- **验收**：并发/恢复场景测试（重启中提交被 fence、审批后原子执行）；诚实不可用原则保持（未完成前不暴露执行工具）。

### G3. Windows Service RuntimeController
> 状态：**已交付**（2026-08-08）。`runtime: {"adapter": "windows_service", "service": "<name>"}` 绑定 + `WindowsServiceController`（固定 `sc.exe stop` → 有界轮询 `sc.exe query` 至 `STATE : 1` STOPPED（1062 未运行直通——returncode 优先 + 非零结果 stdout/stderr 文本兼容，行首锚定数值解析防服务名含 "STATE : 1"/"STOPPED"/"1062" 字样误判；轮询受总预算约束——query timeout 与 sleep 均按剩余时间截断、query 后重算 remaining）→ `sc.exe start`；无 shell、超时、fail-closed）；`controller_from_config` 接线（延迟 import 无循环）+ `_controller_binding` 规范化（service 字段入 digest，G2 plan/commit pin 完整）；19 测试（全 mock subprocess 跨平台：校验/1062 双通道（FAILED 1062 精确模式 + 服务名含 1062 fail-closed 回归）/轮询/剩余预算/慢 query 预算/query 1062 fail-closed/超时/接线/digest/G2 全链路）。
- systemd/Docker 适配器已实现并接线（固定命令 + 超时 + fail-closed）；Windows 服务控制器已完成服务管理 CLI 集成（`sc.exe`），执行动作复用 G2 闭环。
- **交付**：接入 Windows Service 适配器 + 测试。

### 建议迭代顺序
装配分层（前置原则 + 懒初始化 + 引导文档）→ G1（schema 冻结）→ G2（重启闭环）→ G3（Windows 控制器）。每项独立可验证、不阻塞。当前进度：装配分层 ✅、G1 ✅、G2 ✅、G3 ✅——GA 路径三项承诺门槛全部交付，进入 1.0 GA 发布评估。

---

## 历史登记

- 2026-08-07：登记「节点能力感知」计划（P0-P2，含拍板决策）。
- 2026-08-07：P0-1/P0-2/P0-3、P2 可视化、P1 知识库、本地化 engine.history、第三方整合包兼容（local.plugins）全部已交付。
- 2026-08-07：登记 1.0 GA 路径（G1 schema 冻结 / G2 重启闭环 / G3 Windows 控制器）。
- 2026-08-07：登记装配分层原则（诊断经 3 轮方案审查 PASS；轻量仅限 fresh，既有 DB fail-closed 保持）。
- 2026-08-09：高层分支 recipe 交付（upscale_image/save_image/lora_model/controlnet_apply.v1，经 `apply_recipe` 变更操作）。
- 2026-08-10：OpenTelemetry logs 交付（OTLP /v1/logs、CONTEXT_FIELDS 白名单、防导出循环）；G2 重启闭环与 G3 Windows 控制器交付。
