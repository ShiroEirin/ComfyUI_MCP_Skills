# Errors

Command failures and integration errors.

---

## [ERR-20260805-001] comfyui-mcp-help

**Logged**: 2026-08-05
**Priority**: medium
**Status**: resolved
**Area**: docs

### Summary
`comfyui-mcp --help` is not a valid installation check because the entrypoint ignores CLI arguments and starts the stdio server.

### Error
The command initialized the control-plane database and failed on the repository workspace's stale schema history.

### Context
Documentation verification attempted to treat a pure stdio MCP entrypoint as a conventional CLI.

### Suggested Fix
Verify package import/version instead; launch `comfyui-mcp` only through an MCP Host with an explicit clean `COMFYUI_MCP_DIR`.

### Metadata
- Reproducible: yes
- Related Files: docs/INSTALLATION.zh-CN.md, src/comfyui_mcp_skills/__main__.py

### Resolution
- **Resolved**: 2026-08-05
- **Notes**: Replaced the invalid `--help` instruction with an import/version check and documented stdio behavior.

---
