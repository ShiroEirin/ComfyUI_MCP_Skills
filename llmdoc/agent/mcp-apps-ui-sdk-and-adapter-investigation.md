# MCP Apps UI SDK and Adapter Investigation

## Evidence

- Project dependency bounds and lock facts: `pyproject.toml:25-28` requires MCP v2 major line; `uv.lock:786-820` pins `mcp==2.0.0` and `mcp-types==2.0.0`.
- Installed SDK contains the Apps extension in `.venv/Lib/site-packages/mcp/server/apps.py:1-27`. It defines `EXTENSION_ID = "io.modelcontextprotocol/ui"` and `APP_MIME_TYPE = "text/html;profile=mcp-app"` at `:43-47`.
- `Apps` is a high-level `Extension` at `.venv/Lib/site-packages/mcp/server/apps.py:77-85`. `Apps.tool` at `:91-127` requires a `ui://` `resource_uri`, stamps `_meta.ui.resourceUri` and optional visibility, and forwards tool metadata/kwargs. It rejects a non-`ui://` URI or caller-supplied `meta["ui"]`.
- `Apps.add_html_resource` at `.venv/Lib/site-packages/mcp/server/apps.py:129-173` registers a `TextResource` using the required app MIME type and optional CSP, iframe permissions, domain, and border metadata. `add_resource` at `:175-193` rejects a non-`ui://` URI or an explicit MIME mismatch; `tools()` checks that every bound resource exists at `:195-211`.
- Client negotiation is explicit in `client_supports_apps` at `.venv/Lib/site-packages/mcp/server/apps.py:217-230`: the client must advertise `io.modelcontextprotocol/ui` and include `text/html;profile=mcp-app` in its `mimeTypes` settings. The SDK documents text fallback for clients without those capabilities at `:23-26`.
- High-level extension application is available only through `MCPServer(extensions=[...])`: constructor signature at `.venv/Lib/site-packages/mcp/server/mcpserver/server.py:147-176`; `_apply_extension` registers tools/resources and advertises extension settings at `:311-339`. `Apps` is not re-exported by `mcp.server.__init__`; it is imported from `mcp.server.apps`.
- Low-level `Server` constructor used by the repository has handler arguments but no `extensions` parameter: `.venv/Lib/site-packages/mcp/server/lowlevel/server.py:128-205,213-304`. It does expose an `extensions` dictionary and capability advertisement support at `:439-443,527-549,555-625`, but does not apply `Apps` contributions automatically.
- Current adapter constructs low-level `Server` and builds the entire dynamic Tool list in custom handlers: `src/comfyui_mcp_skills/adapters/mcp/server.py:661-735,737-742,1548-1561`. `Tool` metadata currently contains project icons/risk metadata through `decorate_tool`, but no `ui.resourceUri` field is registered in `src/comfyui_mcp_skills/adapters/mcp/tooling.py:464-552`.
- Current resource adapter serves only `comfyui://` resource templates and data: `src/comfyui_mcp_skills/adapters/mcp/resources.py:140-238,397-438,441-530`; no `ui://` resource or `text/html;profile=mcp-app` content exists in repository source. Repository asset search found no project HTML/CSS/JS UI asset.
- Product status excludes App UI: `docs/FEATURES.zh-CN.md:7-14,427-437`; fallback is `resource_link` at `src/comfyui_mcp_skills/application/compatibility.py:9-24`. Existing outputs intentionally use Resource Links and bounded structured data in `src/comfyui_mcp_skills/adapters/mcp/tooling.py:464-552`.
- Runtime introspection with `.venv/Scripts/python.exe` confirmed `Apps` imports from `mcp.server.apps`, the constants above are present, and `MCPServer` has `_apply_extension` while current low-level `Server` has no extension application method.

## Findings and conclusions

1. SDK 2.0.0 has a complete server-side Apps registration surface: `Apps.tool`, `add_html_resource`, resource MIME/CSP/permission metadata, extension capability advertisement, and client-support detection. No SDK upgrade is required.
2. The current repository adapter cannot simply pass `Apps` to `create_server`: it returns low-level `Server`, while the Apps extension is consumed by high-level `MCPServer`. The low-level server can advertise an extension map and can manually attach metadata/resources, but that bypasses the SDK Apps composition checks.
3. Missing integration points are an app resource registry, app MIME/resource serving, `_meta.ui.resourceUri` on selected tools, `io.modelcontextprotocol/ui` capability advertisement, negotiated-client gating, and a meaningful text/Resource-Link fallback. There is no existing UI asset or app-specific tool metadata.
4. Smallest production vertical slice: one bounded read-only Job status view. Register a fixed `ui://comfyui/job.html` resource, bind only `comfyui.job.get` (or a dedicated read-only job-view tool), ensure the result still contains the current text/structured data and Resource Links, and expose the UI metadata only through the Apps-capable path. Scope/owner checks must remain in the existing Job Resource/tool handler.
5. The slice can be implemented on `mcp==2.0.0` either by migrating server construction to `MCPServer(extensions=[Apps()])` or by a low-level adapter that manually reproduces the extension’s metadata/resource/capability behavior. The SDK path is available; the current custom low-level dynamic Tool/resource architecture is the integration blocker.

## Report

### relations

- `Apps.tool` (`mcp/server/apps.py:91-127`) → high-level `MCPServer._apply_extension` (`mcp/server/mcpserver/server.py:311-339`): required registration path for SDK-native Apps.
- `client_supports_apps` (`mcp/server/apps.py:217-230`) → current low-level `ServerRequestContext.session.client_capabilities`: capability gate can be evaluated, but no current repository tool uses it.
- Current dynamic `Tool` list (`adapters/mcp/server.py:661-735`) → current resource handlers (`adapters/mcp/resources.py:140-238,397-438`): both are custom callback surfaces that would need UI additions.
- Existing Resource Link fallback (`adapters/mcp/tooling.py:464-552`) → compatibility policy (`application/compatibility.py:19-23`): must remain for non-App clients.

### result

MCP Apps are **not currently integrated**. They are **implementable without upgrading beyond `mcp>=2,<3`** because SDK 2.0.0 ships `mcp.server.apps`. The production blocker is that the repository uses low-level `Server` with custom dynamic lists/resources rather than high-level `MCPServer(extensions=[Apps])`, plus the absence of a bounded UI resource.

### attention

- Do not advertise `io.modelcontextprotocol/ui` without serving every referenced `ui://` resource with the exact app MIME type.
- Do not remove text/structured output or Resource Links when adding an app view; SDK guidance requires graceful degradation.
- Do not expose arbitrary workflow arguments or raw media to the iframe; start with owner-bound, bounded Job status and existing safe Resource Links.
