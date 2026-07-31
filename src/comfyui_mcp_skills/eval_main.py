"""Command-line entry point for deterministic Agent Eval aggregation."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from comfyui_mcp_skills.application.evaluation import report_from_payload


def run(input_path: Path, output_path: Path | None = None) -> dict[str, object]:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("Eval input must be a JSON object")
    report = report_from_payload(payload)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output_path is None:
        print(rendered, end="")
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    return report


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Aggregate ComfyUI MCP Agent Eval trials")
    parser.add_argument("input", type=Path, help="JSON Eval cases and model trials")
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    arguments = parser.parse_args(argv)
    run(arguments.input, arguments.output)


if __name__ == "__main__":
    main()
