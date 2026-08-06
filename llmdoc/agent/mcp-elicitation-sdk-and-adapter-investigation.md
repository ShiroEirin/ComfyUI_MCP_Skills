# MCP Elicitation SDK and Adapter Investigation

## Evidence

- Project dependency bounds remain `mcp>=2,<3` and `mcp-types>=2,<3` in `pyproject.toml:25-28`; `uv.lock:786-820` and installed metadata identify version `2.0.0`.
- Wire forms are defined in `.venv/Lib/site-packages/mcp_types/_types.py:310-328` (`FormElicitationCapability`, `UrlElicitationCapability`, `ElicitationCapability`) and `:1973-2029` (`ElicitRequestURLParams`, `ElicitRequest`, `ElicitResult`). Modern embedded requests/results are defined at `:2045-2089` (`InputRequest`, `InputRequests`, `InputResponse`, `InputRequiredResult`).
- Protocol dispatch supports standalone `elicitation/create` through 2025-06-18 and 2025-11-25 only: `.venv/Lib/site-packages/mcp_types/methods.py:169-180,316-327`. The same dispatch table says the 2026-07-28 server-request surface is empty at `:179-180` and removes `notifications/elicitation/complete` at `:218-226`.
- Legacy/server-session hooks exist at `.venv/Lib/site-packages/mcp/server/session.py:329-417`: `elicit_form`, deprecated `elicit`, and `elicit_url`; the session exposes `client_capabilities` and `protocol_version` at `:69-91`.
- Typed high-level hooks exist at `.venv/Lib/site-packages/mcp/server/mcpserver/context.py:185-251`: `Context.elicit` validates a Pydantic schema and `Context.elicit_url` handles out-of-band URL interaction. Validation/rendering is implemented at `.venv/Lib/site-packages/mcp/server/elicitation.py:74-142`; URL helper semantics and completion notification guidance are at `:145-173`.
- Modern 2026 input flow is implemented by the high-level resolver: `.venv/Lib/site-packages/mcp/server/mcpserver/resolve.py:9-16,93-96,429-490` states that 2026+ batches elicitation into `InputRequiredResult`, then resumes with `input_responses` and `request_state`; older revisions use standalone back-channel requests.
- `MCPServer` exposes `InputRequiredResult` from tool handlers at `.venv/Lib/site-packages/mcp/server/mcpserver/server.py:415-424`; tool registration forwards `InputRequiredResult` unchanged at `.venv/Lib/site-packages/mcp/server/mcpserver/tools/base.py:145-166`. The high-level server installs request-state middleware at `server.py:223-234`.
- Current adapters use the low-level `Server`: `src/comfyui_mcp_skills/adapters/mcp/server.py:15-34,1548-1561` and `src/comfyui_mcp_skills/adapters/mcp/admin.py:12-25,871-879`. They do not construct `MCPServer`, do not install `RequestStateBoundary`, and current `call_tool` returns `CallToolResult` only (`src/comfyui_mcp_skills/adapters/mcp/server.py:774-778`).
- Existing approval semantics are Resource/tool based: `src/comfyui_mcp_skills/application/provisioning.py:177-202` creates owner-bound approval facts and `:204-252` plans a bounded decision; admin approval tools and schemas are declared in `src/comfyui_mcp_skills/adapters/mcp/admin_control.py:42-59,462-490,771-779`.
- Product status records native Elicitation as not delivered: `docs/FEATURES.zh-CN.md:7-14,427-437`; no-native fallback is `approval_resource` at `src/comfyui_mcp_skills/application/compatibility.py:9-24`.
- Installed client SDK supplies the callback hook at `.venv/Lib/site-packages/mcp/client/session.py:181-186,371-405`; its default callback returns `INVALID_REQUEST` “Elicitation not supported” at `:226-233`. A configured callback causes `ClientCapabilities.elicitation` to advertise form and URL support at `:576-610`.

## Findings and conclusions

1. SDK 2.0.0 supports both form and URL elicitation for legacy handshake versions through `ServerSession` and supports the modern 2026 multi-round flow through `MCPServer`/`Context`/resolver APIs.
2. Current low-level production adapters cannot obtain modern elicitation by calling `ctx.session.elicit_form` alone. For a 2026 connection, standalone server-to-client requests are not in the dispatch surface; modern elicitation must be returned as `InputRequiredResult` and resumed with echoed state.
3. The missing adapter integration points are: a modern `InputRequiredResult` return path; stable input request keys; request-state sealing/validation; reading `ctx.input_responses` on retries; client-capability gating; and a bounded mapping from approval facts to elicitation schema/outcome. None is present in `create_server` or `create_admin_server`.
4. Smallest production vertical slice: add native form elicitation to one existing owner-bound administrative approval decision, preserving the current approval Resource/tool path as fallback. The elicited schema should contain only bounded decision data already represented by the approval contract; secrets should use URL mode, not form mode. Native support must be advertised only after the modern retry/state path is in place.
5. This slice is implementable on SDK 2.0.0 without an upgrade, but it requires either migrating the adapter to `MCPServer` or implementing equivalent request-state middleware and modern retry handling on the low-level server. The SDK is not the blocker; current adapter architecture and missing client-facing callback configuration are blockers.

## Report

### relations

- `ServerSession.elicit_form` (`mcp/server/session.py:351-380`) → protocol table (`mcp_types/methods.py:169-180,326-327`): works for legacy server-request versions, not modern 2026.
- `Context.elicit` (`mcp/server/mcpserver/context.py:185-216`) → resolver (`mcp/server/mcpserver/resolve.py:429-490`): the SDK’s modern path requires high-level Context and multi-round state.
- Existing Approval service (`application/provisioning.py:177-252`) → admin tool surface (`adapters/mcp/admin_control.py:462-490`): current fallback contract that a native slice must preserve.
- Client callback (`mcp/client/session.py:181-186,226-233`) → capability advertisement (`:576-610`): hosts without a callback are explicitly treated as not supporting elicitation.

### result

Elicitation is **not currently integrated**. It is **implementable without upgrading beyond `mcp>=2,<3`**, with the modern 2026 path requiring `MCPServer` resolver/request-state integration or an equivalent low-level implementation. The existing approval Resource/tool fallback remains the safe path until that integration exists.

### attention

- Do not claim native Elicitation solely because `ServerSession.elicit_form` imports successfully; protocol version and back-channel availability control whether it can send.
- Do not put credentials or other sensitive values in form elicitation; SDK documentation directs those flows to URL mode.
- Do not accept a retry response without request-state validation and owner-bound approval rechecks.
