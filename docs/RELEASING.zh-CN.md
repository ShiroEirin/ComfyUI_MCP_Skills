# 发布指南（RELEASING）

## 发布流程（1.1.0 起）

版本号单一来源为 `comfyui_mcp_skills.__version__`（`src/comfyui_mcp_skills/__init__.py`）；CI 校验 tag 与之一致。

1. **版本决策**：确认 `__version__` 目标值（semver）与 `pyproject.toml` classifier 一致（正式版 `Production/Stable`）。
2. **打 tag**：`git tag v1.1.0 && git push origin v1.1.0`（tag 必须等于 `v` + `__version__`）。
3. **CI 自动发布**（`.github/workflows/release.yml`，tag `v*` 触发）：
   - `build`：测试（含 otel extra 全量）、mypy、ruff、pip-audit、构建 sdist/wheel、verify tag==version、wheel 安装验证；产物上传 Actions artifact；
   - `publish`：PyPI Trusted Publishing（environment: `pypi`，attest-build-provenance）自动发布；
   - `github-release`：创建 GitHub Release（按已验证 tag，`--generate-notes`）并上传 `dist/*`（sdist/wheel）作为 Release assets。

## 发布渠道

- **PyPI**：`comfyui-mcp-skills`（Trusted Publishing，无需手动凭证）。
- **GitHub Releases**：每个 `v*` tag 自动创建 Release + 附件。

## 事故策略（发布后发现问题）

- **PyPI**：不得覆盖或删除已发布制品（同版本不可重传）。问题版本执行 `yank`，用户固定到上一可用版本；修复以**新 patch 版本**（如 1.1.1）发布。
- **GitHub Release**：有问题的 Release 标记为有问题或补充公告，不删除制品（保留审计轨迹）。
- **回滚发布** = 发布新 patch 版本，**不是**重跑旧 tag（旧 tag 已有制品，不可覆盖）。
- **运行时降级**：旧程序版本 + 升级前全工作区备份恢复（新 schema 被旧代码 fail-loud 拒绝，不承诺降级；见 INSTALLATION §13.0/13.1）。

## 检查清单

- [ ] `__version__` 更新并提交
- [ ] CHANGELOG.md 更新（Keep a Changelog 格式）
- [ ] classifier 与发布状态一致
- [ ] 打 tag 并推送（触发 CI）
- [ ] CI 全绿（build/publish/github-release 三 job）
- [ ] PyPI 页面与 GitHub Release 附件核验
