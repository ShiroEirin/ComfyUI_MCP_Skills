# MCP Tasks SDK and Adapter Investigation

## Evidence

- Project documentation index was requested at `llmdoc/index.md`; the path is absent. Existing `llmdoc/agent/` contains prior investigation documents only.
- Dependency contract: `pyproject.toml:25-28` requires `mcp>=2,<3` and `mcp-types>=2,<3`. `uv.lock:786-820` pins both packages to `2.0.0`; installed `.venv` metadata also reports `mcp==2.0.0`, `mcp-types==2.0.0`.
- Task models are present in `.venv/Lib/site-packages/mcp_types/_types.py:610-742`: `ToolExecution.task_support`, `TaskMetadata`, `RelatedTaskMetadata`, `TaskStatus`, `Task`, `CreateTaskResult`, `GetTaskRequest/Result`, `CancelTaskRequest/Result`, `GetTaskPayloadRequest/Result`, `ListTasksRequest/Result`, and `TaskStatusNotification`.
- The same file states at `:610-612` that task types are “types-only”; task methods are absent from request/notification unions and “never dispatched”.
- Task capability models at `.venv/Lib/site-packages/mcp_types/_types.py:346-386,461-512` are labelled `2025-11-25 only`. Modern `ServerCapabilities.extensions` is the 2026 mechanism at `:507-512` and `.venv/Lib/site-packages/mcp_types/_v2026_07_28/__init__.py:3568-3575`.
- Dispatch evidence: `.venv/Lib/site-packages/mcp_types/methods.py:101-125` omits `tasks/*` from client requests, `:146-153` omits task status notifications, `:174-180` omits task server requests, `:326-327` has no modern server-to-client results, and `:343-406` has no `tasks/*` monolith entries. Runtime introspection with `.venv/Scripts/python.exe` confirmed `('tasks/get','2026-07-28')` is absent from `CLIENT_REQUESTS` and `tasks/get` is absent from `MONOLITH_REQUESTS`.
- Generic extension method support exists at `.venv/Lib/site-packages/mcp/server/extension.py:58-93`: `MethodBinding` accepts additive methods such as `tasks/get`, validates params models, and can gate protocol versions. `MCPServer._apply_extension` registers methods and advertises extension settings at `.venv/Lib/site-packages/mcp/server/mcpserver/server.py:311-339`.
- Current production adapter imports/returns low-level `Server`, not `MCPServer`: `src/comfyui_mcp_skills/adapters/mcp/server.py:15-34,1548-1561`; admin does the same at `src/comfyui_mcp_skills/adapters/mcp/admin.py:12-25,871-879`.
- Current workflow call handler receives `CallToolRequestParams` but returns only `CallToolResult`: `src/comfyui_mcp_skills/adapters/mcp/server.py:774-778`. It does not branch on `params.task`; dynamic tools have `output_schema` but no task execution declaration at `:677-693`.
- Durable jobs already have owner-bound IDs/status/output data: `src/comfyui_mcp_skills/application/jobs.py:81-154,179-194,332-358`; `src/comfyui_mcp_skills/domain/models.py:113-129`. No repository source reference defines `task_id`, `tasks/get`, `tasks/result`, or task status persistence.
- Product status explicitly excludes Tasks: `docs/FEATURES.zh-CN.md:7-14,427-437`; fallback behavior is `submitted_job_resource` when native Tasks are unavailable at `src/comfyui_mcp_skills/application/compatibility.py:9-24`.

## Findings and conclusions

1. SDK 2.0.0 contains legacy task wire models and capability models, but does not dispatch task methods. The package has no built-in Tasks extension or task runtime.
2. Current major line is sufficient for a custom Tasks extension: define modern extension request/response models, register `tasks/get`, `tasks/result`, `tasks/list`, `tasks/cancel` and status notification handling through `MCPServer` extension methods or low-level `Server.add_request_handler`, advertise `io.modelcontextprotocol/tasks`, and handle a custom `resultType` such as the task extension’s `task` claim.
3. This requires custom wire models because the installed `mcp-types` 2.0.0 task classes are marked 2025-11-25-only and the dispatch tables reject/omit task methods. An SDK upgrade is not the blocker; the missing production slice is the repository-to-extension mapping and custom wire surface.
4. Smallest production vertical slice: map one existing execution workflow call to an owner-bound task handle backed by the existing durable Job identity, return a task result for asynchronous execution, implement only `tasks/get`, `tasks/result`, and `tasks/cancel`, and preserve the existing Job Resource fallback for clients without the extension. `tasks/list` and task notifications can follow only if required by the negotiated extension contract.
5. Safety requirements for that slice are already represented by Job owner checks (`JobService._authorize_owner` at `src/comfyui_mcp_skills/application/jobs.py:374-377`) but task ID mapping, task retention/result projection, tool `taskSupport` metadata, capability negotiation, and method authorization are absent. They must be added before advertising native Tasks.

## Report

### relations

- `CallToolRequestParams.task` (`mcp_types/_types.py:1449-1453`) → current `call_tool` (`adapters/mcp/server.py:774-858`): field is typed by SDK but ignored by adapter.
- Durable `Job` (`domain/models.py:113-129`) → proposed task handle: existing owner/status/output facts can be projected, but no task-specific wire or retention contract exists.
- `MethodBinding` (`mcp/server/extension.py:58-93`) → `MCPServer._apply_extension` (`mcp/server/mcpserver/server.py:311-339`): available SDK hook for custom task methods.
- Compatibility fallback (`application/compatibility.py:19-23`) → feature status (`docs/FEATURES.zh-CN.md:12,433`): current behavior intentionally remains a submitted Job Resource rather than a native task.

### result

Tasks are **not currently integrated**. They are **implementable without upgrading beyond `mcp>=2,<3`**, but only as a custom 2026 extension on top of SDK 2.0.0; no turnkey SDK task runtime or dispatch exists.

### attention

- Do not advertise legacy `ServerTasksCapability` as modern Tasks support; the installed types label it 2025-11-25-only.
- Do not expose native task capability until task result retention, owner checks, cancellation semantics, and task method authorization are wired to the existing SQLite/Job evidence model.
