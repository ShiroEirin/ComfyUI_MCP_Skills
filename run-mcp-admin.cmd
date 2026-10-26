@echo off
rem See run-mcp.cmd: clear the invalid PYTHONUTF8 injected by the host.
set "PYTHONUTF8="
set "PYTHONIOENCODING=utf-8"
"%~dp0.venv\Scripts\comfyui-mcp-admin.exe" %*
