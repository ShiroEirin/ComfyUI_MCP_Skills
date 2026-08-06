# RuntimeController Adapter and Entrypoint Analysis

## Part 1: Evidence

### RuntimeController contract and service behavior

- `src/comfyui_mcp_skills/application/runtime_control.py:17-19` declares `RuntimeController.restart(server_id) -> dict[str, Any]`.
- `src/comfyui_mcp_skills/application/runtime_control.py:24-35` accepts an optional controller in `RuntimeControlService`; the service stores it but does not call it from the currently exposed restart-plan method.
- `src/comfyui_mcp_skills/application/runtime_control.py:113-150` validates the server, enumerates owner-visible nonterminal jobs, reports `runtime_controller_available`, and returns a `runtime_plan_*` digest/resource with `status: operation_required`. There is no restart commit/execute method in this class.
- `src/comfyui_mcp_skills/adapters/mcp/server.py:413-420` constructs `RuntimeControlService(servers, run_repository, gateway_factory)` without a controller.
- `src/comfyui_mcp_skills/adapters/mcp/server.py:1398-1405` dispatches only `comfyui.runtime.restart.plan` to `restart_plan`.
- `src/comfyui_mcp_skills/adapters/mcp/tooling.py:1422-1432` exposes only a read-only `comfyui.runtime.restart.plan` schema (`server_id` required; no execute/commit input).

### Entrypoint and HTTP wiring

- `src/comfyui_mcp_skills/__main__.py:52-67` initializes SQLite and calls `create_server(...)` for stdio. It wires upload roots, authorization, dynamic-tool limits, and `SafeManagerGateway`; no runtime controller factory is present.
- `src/comfyui_mcp_skills/http_main.py:20-25,28-51,79-120` builds HTTP app options from environment and starts Uvicorn. No runtime-controller setting is read.
- `src/comfyui_mcp_skills/adapters/http/app.py:109-138` builds `ServerRegistry`, repositories, `SafeManagerGateway`, and calls `create_server(...)`; no runtime controller is passed.
- `src/comfyui_mcp_skills/admin_main.py:49-70` creates the isolated Admin server for provisioning/configuration. It does not construct the ordinary Operations runtime-control service.

### Current server configuration contract

- `src/comfyui_mcp_skills/application/servers.py:15-27` resolves `<COMFYUI_MCP_DIR>/config.json`; server records currently expose `id`, `name`, `url`, `enabled`, and `output_dir` through `Server`.
- `src/comfyui_mcp_skills/application/servers.py:50-66` returns a shallow copy of the enabled matching server record and rejects missing/disabled/invalid config; unknown extra fields are preserved in the returned dictionary.
- `docs/INSTALLATION.zh-CN.md:46-81` documents `config.json`, server IDs, ComfyUI HTTP URL, and enabled/default-server behavior. The example has no runtime-manager section.
- `src/comfyui_mcp_skills/application/config_bundles.py:179-232` normalizes exported/imported server config to bounded `server_id`, `endpoint_url`, enabled/default fields. A runtime-manager field is not part of the current Config Bundle schema.

### Security and authorization evidence

- `src/comfyui_mcp_skills/adapters/mcp/tooling.py:1422-1432` marks restart planning read-only; `src/comfyui_mcp_skills/application/runtime_control.py:137-140` currently sets `approval_required` to `False` while reporting that global impact and management approval are required.
- `src/comfyui_mcp_skills/application/runtime_control.py:63-111` requires explicit execution for global queue clear/interruption and requires management permission for cross-owner effects.
- `README.md:189-196` states that without a RuntimeController restart reports host operation requirements and does not execute host Shell commands.
- `src/comfyui_mcp_skills/application/authorization.py:195-218` requires explicit principal, toolset, scopes, and high-risk enablement for non-execution stdio toolsets. Operations uses `comfyui:observe,comfyui:operate` as documented in `docs/INSTALLATION.zh-CN.md:180-215`.

### Packaging and installed SDK facts

- `pyproject.toml:25-37` directly depends on MCP, requests, urllib3, websocket-client, uvicorn, and related libraries; it has no Docker SDK, pywin32, systemd/dbus binding, or Unix-socket requests dependency.
- `comfyui_mcp_skills.egg-info/requires.txt:1-11` matches the direct runtime dependency list and contains no host service-manager dependency.
- `uv.lock:785-804` records MCP `2.0.0`; its dependency list includes `pywin32` only with `sys_platform == 'win32'`.
- `uv.lock:1321-1325` records `pywin32` `312`. The current Python 3.13 environment imports `win32service` and `win32serviceutil`; `win32serviceutil.RestartService`, `OpenSCManager`, `ControlService`, `StartService`, and `QueryServiceStatus` are available. This is a transitive MCP dependency, not a direct project contract.
- The current environment imports `requests` and `mcp`; `docker`, `systemd`, and `requests_unixsocket` modules are not installed.
- `comfyui_mcp_skills.egg-info/SOURCES.txt:36-156` lists all packaged source modules and has no runtime adapter module.

### Existing roadmap and tests

- `docs/FEATURES.zh-CN.md:295-315,427-437` lists restart planning as delivered and Docker/systemd/Windows Service adapters as not built.
- `MCP_AGENT_NATIVE_CONTROL_PLANE.zh-CN.md:1642-1660` states the Phase P acceptance boundary: optional host adapters, global-impact enumeration, management approval, and Job reconciliation after restart; without a controller, no shell execution.
- `tests/test_phase_p_runtime_control.py:101-106` constructs the service without a controller; `:123-136` asserts restart planning reports `runtime_controller_available is False`; `:144-179` verifies MCP exposure and the same unavailable-controller result.
- `tests/test_entrypoints.py:1-29` currently covers only stdio upload-root environment parsing; no runtime-adapter configuration or entrypoint wiring test exists.
- `README.md:244-255` and `comfyui_mcp_skills.egg-info/PKG-INFO:289-291` both list host RuntimeController adapters as not delivered.

## Part 2: Factual findings and conclusions

### Recommended first vertical slice: systemd-only adapter

The smallest adapter that fits the current dependency set is a Linux systemd adapter backed by a fixed-argument `systemctl` invocation. It requires no new Python SDK: `subprocess.run` is in the Python standard library. The adapter must be opt-in, server-scoped, and unavailable by default.

Proposed existing `config.json` extension (under each server record):

```json
{
  "id": "local",
  "url": "http://127.0.0.1:8188",
  "enabled": true,
  "runtime": {
    "adapter": "systemd",
    "unit": "comfyui-local.service"
  }
}
```

Exact contract:

- `runtime` must be an object when present.
- `runtime.adapter` is required and has the single supported value `systemd`.
- `runtime.unit` is required, 1–128 ASCII characters, starts with an ASCII letter or digit, and allows only `[A-Za-z0-9_.:@-]`; reject whitespace, `/`, `\\`, NUL, and a leading `-`. A `.service` suffix should be required for this first slice to avoid targeting other unit kinds.
- Any absent `runtime`, unsupported adapter, malformed object, or malformed unit leaves the controller unavailable or fails closed; it must not fall back to a shell command.
- The adapter executes exactly `systemctl restart <validated-unit>` with `shell=False`, an argv list, a bounded timeout, captured output suppressed/redacted, and a checked return code. It must never accept a command, arguments array, executable path, environment fragment, or shell text from `config.json`.
- `server_id` selects the server record; the unit is read from that record. No user-supplied unit is accepted by the MCP tool, and no arbitrary server record is selected outside existing server validation/authorization.
- `restart()` should return bounded facts such as `server_id`, `adapter`, `unit` (if policy allows), `accepted`/`completed`, and a status/error code; raw stdout/stderr and credentials must not be returned.

This slice is not complete until restart execution is intentionally exposed through a plan/approval/commit path. The current API exposes only a read-only plan, and `RuntimeControlService` has no execute method. The smallest production cutover should therefore either (a) add a controller-backed restart commit that accepts the existing plan ID/digest and management authorization, or (b) defer actual controller invocation while wiring only availability. Option (a) is required to claim a functioning adapter; option (b) is only wiring and does not satisfy restart execution.

### Files to change for that slice (recommendation only; no files changed here)

1. Add `src/comfyui_mcp_skills/infrastructure/runtime/systemd.py` implementing `RuntimeController` with fixed argv, timeout, no shell, bounded errors, and platform/`systemctl` availability checks.
2. Update `src/comfyui_mcp_skills/application/runtime_control.py` to add the plan-bound, management-authorized restart execution method and preserve fail-closed behavior when no controller exists. Keep the existing plan digest as the binding input; do not invent a second command contract.
3. Update `src/comfyui_mcp_skills/adapters/mcp/server.py` so `create_server` accepts an optional controller and passes it to `RuntimeControlService`; add the restart commit dispatch only if the application method is added.
4. Update `src/comfyui_mcp_skills/__main__.py` to construct the optional controller from the validated server config for stdio.
5. Update `src/comfyui_mcp_skills/adapters/http/app.py` (and, if preferred, `http_main.py` option construction) to construct the same optional controller for HTTP. The adapter must not be enabled by HTTP network exposure alone.
6. Update `src/comfyui_mcp_skills/application/servers.py` or a dedicated configuration validator to validate the nested runtime contract. Update `src/comfyui_mcp_skills/application/config_bundles.py` only if runtime settings are intended to be exported/imported; otherwise explicitly leave runtime host binding outside portable Config Bundles.
7. Update `docs/INSTALLATION.zh-CN.md`, `docs/FEATURES.zh-CN.md`, and `README.md` to document Linux/systemd prerequisites, privilege boundaries, config, and the no-controller fallback.
8. Update `pyproject.toml` only if a direct platform dependency is chosen. The systemd first slice needs no dependency change.

### Required tests

- Extend `tests/test_phase_p_runtime_control.py` with a fake controller test proving: restart commit requires the plan digest, rejects another owner/plan, does not call the controller on preview or failed authorization, calls it once on a valid commit, and reports unavailable/failure without shell fallback.
- Add focused adapter tests (new `tests/test_runtime_systemd.py` or the existing Phase P file) mocking `subprocess.run`: assert exact argv `['systemctl', 'restart', unit]`, `shell=False`, timeout, `check=True`/equivalent, and bounded result mapping; assert malformed units, unsupported adapter, nonzero exit, timeout, and missing executable fail closed.
- Extend `tests/test_entrypoints.py` or add a wiring test proving a configured `runtime` record creates the controller and no runtime block leaves it `None`; cover both stdio factory options and HTTP app options if both transports support restart.
- Add config tests proving command-like fields (`command`, `args`, `executable`, `shell`) are rejected/ignored and cannot affect argv. If Config Bundles include runtime settings, add round-trip and bounded-schema tests; otherwise assert host runtime settings are not serialized.
- Existing `tests/test_phase_p_runtime_control.py:123-136,175-176` assertions for no controller must remain valid when runtime config is absent.

### Cross-platform blockers

- systemd exists only on Linux hosts running systemd; it does not control a remote ComfyUI URL, Windows Service, Docker Desktop container, WSL process, or non-systemd init system.
- `systemctl restart` requires service-manager permissions and can return before ComfyUI is HTTP-ready; a production commit needs post-restart health/reconciliation semantics rather than treating process command acceptance as Job completion.
- Docker has no installed Python SDK (`docker` module absent), no Unix-socket adapter dependency (`requests_unixsocket` absent), and Docker CLI/API context, socket/pipe permissions, container identity, and remote contexts differ by platform. A Docker adapter would need a direct SDK dependency or a separately fixed CLI/API boundary.
- Windows Service control is available in the current environment through `pywin32` `312`, but only transitively through MCP's Windows marker in `uv.lock`; it is not a direct project dependency or cross-platform install guarantee. A Windows adapter must add a direct Windows-marked dependency, use SCM APIs (not `sc.exe` shell text), validate a service name, and handle stop/start wait states and service-account permissions.
- A single adapter factory must not import systemd or pywin32 modules at module import time on other platforms; use platform-gated imports and return controller-unavailable/fail-closed results.
- Existing per-owner routing can resolve owner-specific server connections in `create_server`, but runtime manager identity is global host configuration. Runtime restart must not infer a host unit/container from an owner-provided routing record without an explicit trusted binding.
- Existing SQLite evidence and authorization boundaries do not record restart command execution. A production adapter needs an audit/evidence event or durable operation record tied to the plan digest before claiming restart completion; otherwise preserve `operation_required`/unavailable status.

## Report

### conclusions

- The protocol exists, but no adapter is wired and no restart execution endpoint exists.
- The first adapter should be systemd, with a server-local fixed unit binding and fixed `systemctl restart` argv.
- Docker and Windows Service require additional platform/dependency contracts; Windows pywin32 is currently transitive only.
- Actual production support requires plan-bound authorized execution plus post-restart reconciliation/evidence, not merely constructing a controller.

### relations

- `create_server` → `RuntimeControlService` (`server.py:255-270`, `:413-420`): current wiring drops the optional controller.
- `RuntimeControlService.restart_plan` → `comfyui.runtime.restart.plan` (`runtime_control.py:113-150`, `server.py:1398-1405`, `tooling.py:1422-1432`): read-only plan path with no execution.
- `ServerRegistry.connection` → `config.json` (`servers.py:15-17`, `:50-66`): extra server fields survive as adapter configuration.
- stdio/HTTP entrypoints → `create_server` (`__main__.py:52-67`, `http/app.py:127-138`): both need the same optional controller factory.
- packaging → platform SDKs (`pyproject.toml:25-37`, `uv.lock:785-804`, `:1321-1325`): no direct host-manager SDK exists.

### result

Recommend a server-scoped, opt-in systemd adapter as the first boring vertical slice. Use only a validated unit name from `config.json`; invoke `systemctl` via fixed argv with `shell=False`; add the controller to both stdio and HTTP factory wiring; add plan-bound authorized execution and reconciliation/evidence before exposing a successful restart commit.

### attention

- Do not add a generic `command`/`args` configuration escape hatch.
- Do not execute restart directly from `runtime.restart.plan`.
- Do not expose raw manager output or treat a successful process-control return as ComfyUI readiness.
- Do not claim Docker/Windows portability from the systemd adapter.
