"""Run the G6 tool-selection baseline through OMP's DeepSeek V4 Flash endpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from comfyui_mcp_skills import __version__
from comfyui_mcp_skills.application.evaluation import EvalCase, EvalTrial, evaluate_trials

MODEL_ID = "deepseek-v4-flash"
DEFAULT_BASE_URL = "http://127.0.0.1:3000/v1"


def selection_prompt(tools: list[dict[str, str]], task: str) -> str:
    lines = [
        "Choose the single MCP tool that should be called first.",
        "Return only the exact tool name, with no explanation.",
        "Available tools:",
    ]
    lines.extend(f"- {tool['name']}: {tool['summary']}" for tool in tools)
    lines.append(f"Task: {task}")
    return "\n".join(lines)


def selected_tool(text: str, allowed: set[str]) -> str:
    candidate = text.strip()
    if not candidate or "\n" in candidate or len(candidate) > 256:
        return ""
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in "`\"'":
        candidate = candidate[1:-1].strip()
    return candidate if candidate in allowed else ""


def _validated_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ValueError("DeepSeek Eval endpoint must use the 127.0.0.1 loopback address")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("DeepSeek Eval endpoint must not contain credentials, query, or fragment")
    return value.rstrip("/")


def _response_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("DeepSeek response is missing choices")
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ValueError("DeepSeek response is missing message content")
    return str(message["content"])


def _usage_count(usage: dict[str, Any], primary: str, fallback: str) -> int:
    value = usage.get(primary, usage.get(fallback, 0))
    if type(value) is not int or value < 0:
        raise ValueError(f"DeepSeek usage.{primary} must be a non-negative integer")
    return value


def run_deepseek_baseline(
    definition_path: Path,
    output_path: Path,
    *,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    session_factory: Callable[[], requests.Session] = requests.Session,
) -> dict[str, Any]:
    if not api_key:
        raise ValueError("NEWAPI_API_KEY is required")
    endpoint = _validated_base_url(base_url) + "/chat/completions"
    definition_bytes = definition_path.read_bytes()
    definition = json.loads(definition_bytes)
    if not isinstance(definition, dict):
        raise TypeError("Eval definition must be a JSON object")
    raw_tools = definition.get("tools")
    raw_cases = definition.get("cases")
    if not isinstance(raw_tools, list) or not all(isinstance(item, dict) for item in raw_tools):
        raise TypeError("tools must be an array of objects")
    if not isinstance(raw_cases, list) or not all(isinstance(item, dict) for item in raw_cases):
        raise TypeError("cases must be an array of objects")
    tools = [{"name": str(item["name"]), "summary": str(item["summary"])} for item in raw_tools]
    cases = tuple(EvalCase(**item) for item in raw_cases)
    allowed = {tool["name"] for tool in tools}
    trials: list[EvalTrial] = []

    session = session_factory()
    session.trust_env = False
    try:
        for case in cases:
            started = time.monotonic()
            response = session.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": MODEL_ID,
                    "messages": [
                        {"role": "system", "content": "You are a deterministic MCP tool router."},
                        {"role": "user", "content": selection_prompt(tools, case.task)},
                    ],
                    "max_tokens": 512,
                    "temperature": 0,
                },
                timeout=120,
                allow_redirects=False,
            )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise TypeError("DeepSeek response must be a JSON object")
            content = _response_content(body)
            selected = selected_tool(content, allowed)
            usage = body.get("usage", {})
            if not isinstance(usage, dict):
                raise TypeError("DeepSeek response usage must be an object")
            prompt_tokens = _usage_count(usage, "prompt_tokens", "input_tokens")
            completion_tokens = _usage_count(usage, "completion_tokens", "output_tokens")
            trials.append(
                EvalTrial(
                    MODEL_ID,
                    "cost-efficient-tool-model",
                    case.case_id,
                    selected,
                    None,
                    0,
                    0,
                    None,
                    parse_status="exact" if selected else "invalid_response",
                    provider_input_tokens=prompt_tokens,
                    provider_output_tokens=completion_tokens,
                    selection_latency_ms=round((time.monotonic() - started) * 1000),
                    response_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                )
            )
    finally:
        session.close()

    report = evaluate_trials(cases, trials, active_tool_count=len(tools))
    report.update(
        {
            "eval_id": str(definition.get("eval_id", "")),
            "benchmark": "tool-selection",
            "runtime": "omp-newapi-loopback",
            "model": MODEL_ID,
            "definition_sha256": hashlib.sha256(definition_bytes).hexdigest(),
            "active_tools": sorted(allowed),
            "selection_catalog_sha256": hashlib.sha256(
                json.dumps(tools, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "package_version": __version__,
        }
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the G6 DeepSeek V4 Flash baseline")
    parser.add_argument("definition", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    arguments = parser.parse_args(argv)
    run_deepseek_baseline(
        arguments.definition,
        arguments.output,
        api_key=os.environ.get("NEWAPI_API_KEY", ""),
        base_url=arguments.base_url,
    )


if __name__ == "__main__":
    main()
