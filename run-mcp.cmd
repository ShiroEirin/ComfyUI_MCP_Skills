@echo off
rem Snow App injects an invalid PYTHONUTF8 into every stdio MCP child; CPython
rem aborts at preinit (Fatal Python error: preconfig_init_utf8_mode) and the
rem host sees "connection closed: discover response". Clear it first.
set "PYTHONUTF8="
set "PYTHONIOENCODING=utf-8"
"%~dp0.venv\Scripts\comfyui-mcp.exe" %*
