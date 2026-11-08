"""One-call runs must be able to hand the finished image straight back."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

import comfyui_mcp_skills.adapters.mcp.server as server_module  # noqa: F401  (import-time side effects)
from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.adapters.mcp.tooling import EXECUTION_PROPERTY
from comfyui_mcp_skills.application.execution_options import (
    EXECUTION_FIELDS,
    parse_execution_options,
)

_PNG = b"\x89PNG\r\n\x1a\nagent-result"


def _project(root: Path) -> None:
    (root / "config.json").write_text(
        json.dumps(
            {
                "default_server": "local",
                "servers": [{"id": "local", "url": "http://127.0.0.1:8188"}],
            }
        ),
        encoding="utf-8",
    )
    directory = root / "data" / "local" / "workflow-00"
    directory.mkdir(parents=True)
    (directory / "schema.json").write_text(
        json.dumps(
            {
                "description": "Returned image fixture",
                "enabled": True,
                "parameters": {
                    "prompt": {
                        "type": "string",
                        "required": True,
                        "node_id": "1",
                        "field": "text",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (directory / "workflow.json").write_text(
        json.dumps({"1": {"class_type": "Text", "inputs": {"text": ""}}}),
        encoding="utf-8",
    )


def _image_output(filename: str) -> dict[str, Any]:
    return {"images": [{"filename": filename, "subfolder": "mcp-test", "type": "output"}]}


class _Gateway:
    def __init__(self) -> None:
        self.histories: dict[str, dict[str, Any]] = {}
        self.downloads: list[tuple[str, str, str]] = []

    def queue_prompt(self, workflow: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return {"prompt_id": f"prompt-{len(self.downloads) + 1}", "client_id": "return-images"}

    def get_history(
        self, prompt_id: str, timeout_seconds: float | None = None
    ) -> dict[str, Any] | None:
        return self.histories.get(prompt_id)

    def get_queue(self, timeout_seconds: float | None = None) -> dict[str, Any]:
        return {"queue_running": [], "queue_pending": []}

    def ws_events(
        self,
        client_id: str,
        prompt_id: str,
        timeout_seconds: float,
        cancel_check: Any = None,
    ) -> tuple[dict[str, Any], ...]:
        return ()

    def download_output(
        self,
        filename: str,
        subfolder: str = "",
        output_type: str = "output",
        *,
        max_bytes: int,
    ) -> bytes:
        self.downloads.append((filename, subfolder, output_type))
        return _PNG


def _completed(history: dict[str, Any], *filenames: str) -> dict[str, Any]:
    history["status"] = {"completed": True, "status_str": "success"}
    history["outputs"] = {"9": _image_output(filenames[0])}
    for index, filename in enumerate(filenames[1:], start=10):
        history["outputs"][str(index)] = _image_output(filename)
    return history


def _image_blocks(result: Any) -> list[Any]:
    return [block for block in result.content if getattr(block, "type", "") == "image"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_tag_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tag mode writes to disk and returns no bytes; these tests cover the
    # default native-block mode, so the environment must not switch it.
    monkeypatch.delenv("COMFYUI_MCP_IMAGE_TAG_DIR", raising=False)


@pytest.mark.anyio
async def test_completed_run_attaches_the_image_block(tmp_path: Path) -> None:
    _project(tmp_path)
    gateway = _Gateway()
    gateway.histories["prompt-1"] = _completed({}, "portrait_00001_.png")
    server = create_server(tmp_path, gateway_factory=lambda _config: gateway)

    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.workflow.run",
            {
                "server_id": "local",
                "workflow_id": "workflow-00",
                "arguments": {"prompt": "one call, one picture"},
                "_execution": {
                    "wait": True,
                    "wait_timeout_seconds": 5,
                    "return_images": True,
                },
            },
        )

    blocks = _image_blocks(result)
    assert len(blocks) == 1
    assert blocks[0].mime_type == "image/png"
    assert base64.b64decode(blocks[0].data) == _PNG
    returned = result.structured_content["returned_images"]
    assert returned == [
        {
            "filename": "portrait_00001_.png",
            "subfolder": "mcp-test",
            "mime_type": "image/png",
            "size_bytes": len(_PNG),
            "attached": True,
        }
    ]
    assert gateway.downloads == [("portrait_00001_.png", "mcp-test", "output")]


@pytest.mark.anyio
async def test_max_images_bounds_the_attached_blocks(tmp_path: Path) -> None:
    _project(tmp_path)
    gateway = _Gateway()
    gateway.histories["prompt-1"] = _completed(
        {}, "first.png", "second.png", "third.png"
    )
    server = create_server(tmp_path, gateway_factory=lambda _config: gateway)

    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.workflow.run",
            {
                "server_id": "local",
                "workflow_id": "workflow-00",
                "arguments": {"prompt": "two is enough"},
                "_execution": {
                    "wait": True,
                    "wait_timeout_seconds": 5,
                    "return_images": True,
                    "max_images": 2,
                },
            },
        )

    assert len(_image_blocks(result)) == 2
    assert [name for name, _folder, _type in gateway.downloads] == [
        "first.png",
        "second.png",
    ]


@pytest.mark.anyio
async def test_timed_out_run_never_attaches_a_partial_image(tmp_path: Path) -> None:
    _project(tmp_path)
    gateway = _Gateway()  # no history: the Job is still running
    server = create_server(tmp_path, gateway_factory=lambda _config: gateway)

    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.workflow.run",
            {
                "server_id": "local",
                "workflow_id": "workflow-00",
                "arguments": {"prompt": "still running"},
                "_execution": {
                    "wait": True,
                    "wait_timeout_seconds": 0,
                    "return_images": True,
                },
            },
        )

    assert result.structured_content["timed_out"] is True
    assert result.structured_content["returned_images"] == []
    assert _image_blocks(result) == []
    assert gateway.downloads == []


@pytest.mark.anyio
async def test_failed_run_never_attaches_an_image(tmp_path: Path) -> None:
    _project(tmp_path)
    gateway = _Gateway()
    gateway.histories["prompt-1"] = {
        "status": {
            "completed": False,
            "status_str": "error",
            "messages": [["execution_error", {"node_id": "1", "exception_message": "boom"}]],
        },
        "outputs": {"9": _image_output("half_written.png")},
    }
    server = create_server(tmp_path, gateway_factory=lambda _config: gateway)

    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.workflow.run",
            {
                "server_id": "local",
                "workflow_id": "workflow-00",
                "arguments": {"prompt": "fails"},
                "_execution": {
                    "wait": True,
                    "wait_timeout_seconds": 5,
                    "return_images": True,
                },
            },
        )

    assert result.structured_content["status"] == "error"
    assert result.structured_content["returned_images"] == []
    assert _image_blocks(result) == []
    assert gateway.downloads == []


def test_execution_schema_matches_the_parser_contract() -> None:
    assert set(EXECUTION_PROPERTY["properties"]) == set(EXECUTION_FIELDS)
    properties = EXECUTION_PROPERTY["properties"]
    assert properties["return_images"]["default"] is False
    assert properties["max_images"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 4,
        "default": 1,
        "description": "Upper bound on attached image blocks when return_images is set",
    }


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"return_images": "yes"}, "return_images must be a boolean"),
        ({"max_images": 0}, "max_images must be between 1 and 4"),
        ({"max_images": 5}, "max_images must be between 1 and 4"),
        ({"max_images": 1.5}, "max_images must be an integer"),
        ({"unknown_option": True}, "Unexpected _execution fields: unknown_option"),
    ],
)
def test_parser_refuses_unusable_image_requests(raw: dict[str, Any], message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        parse_execution_options(raw)


def test_parser_defaults_keep_image_return_opt_in() -> None:
    options = parse_execution_options({})

    assert options.return_images is False
    assert options.max_images == 1
