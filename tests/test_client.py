"""Tests for the shared ComfyUIClient implementation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from comfyui_mcp_skills.infrastructure.comfyui.client import ComfyUIClient


def _streaming_response(
    payload: bytes,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    response = MagicMock(status_code=status_code)
    response.headers = {"Content-Length": str(len(payload))} if headers is None else headers
    response.iter_content.side_effect = lambda chunk_size: [
        payload[offset : offset + chunk_size] for offset in range(0, len(payload), chunk_size)
    ]
    return response


# -- queue_prompt --


class QueuePromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ComfyUIClient("http://localhost:8188")

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_with_client_id(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"prompt_id": "p-123"})
        mock_post.return_value.raise_for_status = MagicMock()
        result = self.client.queue_prompt({"1": {}}, client_id="my-client-id")
        self.assertEqual(result["client_id"], "my-client-id")
        self.assertEqual(result["prompt_id"], "p-123")
        self.assertEqual(mock_post.call_args.kwargs["json"]["client_id"], "my-client-id")

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_generates_client_id(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"prompt_id": "p-456"})
        mock_post.return_value.raise_for_status = MagicMock()
        result = self.client.queue_prompt({"1": {}})
        self.assertIn("client_id", result)
        self.assertTrue(len(result["client_id"]) > 0)

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_with_targets(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"prompt_id": "p-100"})
        mock_post.return_value.raise_for_status = MagicMock()
        self.client.queue_prompt({"1": {}}, targets=["5", "8"])
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["partial_execution_targets"], ["5", "8"])

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_without_targets(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"prompt_id": "p-101"})
        mock_post.return_value.raise_for_status = MagicMock()
        self.client.queue_prompt({"1": {}}, targets=None)
        payload = mock_post.call_args.kwargs["json"]
        self.assertNotIn("partial_execution_targets", payload)

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_with_empty_targets(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"prompt_id": "p-102"})
        mock_post.return_value.raise_for_status = MagicMock()
        self.client.queue_prompt({"1": {}}, targets=[])
        payload = mock_post.call_args.kwargs["json"]
        self.assertNotIn("partial_execution_targets", payload)


# -- interrupt / queue management / free --


class InterruptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ComfyUIClient("http://localhost:8188")

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_with_prompt_id(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=200)
        result = self.client.interrupt("abc-123")
        self.assertTrue(result["success"])
        self.assertEqual(mock_post.call_args.kwargs["json"], {"prompt_id": "abc-123"})

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_without_prompt_id(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=200)
        result = self.client.interrupt()
        self.assertTrue(result["success"])
        self.assertEqual(mock_post.call_args.kwargs["json"], {})


class QueueManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ComfyUIClient("http://localhost:8188")

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_queue_clear(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=200)
        result = self.client.queue_clear()
        self.assertTrue(result["success"])
        self.assertEqual(mock_post.call_args.kwargs["json"], {"clear": True})

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_queue_delete(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=200)
        result = self.client.queue_delete(["id-1", "id-2"])
        self.assertTrue(result["success"])
        self.assertEqual(mock_post.call_args.kwargs["json"], {"delete": ["id-1", "id-2"]})


class FreeMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ComfyUIClient("http://localhost:8188")

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_free_both(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=200)
        result = self.client.free_memory(unload_models=True, free_memory=True)
        self.assertTrue(result["success"])
        self.assertEqual(
            mock_post.call_args.kwargs["json"], {"unload_models": True, "free_memory": True}
        )

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_free_models_only(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=200)
        self.client.free_memory(unload_models=True, free_memory=False)
        self.assertEqual(mock_post.call_args.kwargs["json"], {"unload_models": True})

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_free_memory_only(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=200)
        self.client.free_memory(unload_models=False, free_memory=True)
        self.assertEqual(mock_post.call_args.kwargs["json"], {"free_memory": True})

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_free_no_flags_sends_empty(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=200)
        self.client.free_memory(unload_models=False, free_memory=False)
        self.assertEqual(mock_post.call_args.kwargs["json"], {})


# -- upload --


class UploadFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ComfyUIClient("http://localhost:8188")

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_upload_file_calls_upload_image_endpoint(self, mock_post: MagicMock) -> None:
        payload = json.dumps({"name": "test.png", "subfolder": "", "type": "input"}).encode("utf-8")
        mock_post.return_value = _streaming_response(payload)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"fake png data")
            tmp_path = f.name

        try:
            result = self.client.upload_file(tmp_path)
            self.assertEqual(result["name"], "test.png")
            self.assertIn("/upload/image", mock_post.call_args.args[0])
            self.assertTrue(mock_post.call_args.kwargs["stream"])
            self.assertFalse(mock_post.call_args.kwargs["allow_redirects"])
            self.assertEqual(mock_post.call_args.kwargs["headers"]["Accept-Encoding"], "identity")
            mock_post.return_value.close.assert_called_once()
        finally:
            os.unlink(tmp_path)

    def test_upload_image_delegates_to_upload_file(self) -> None:
        with patch.object(self.client, "upload_file", return_value={"name": "x.png"}) as mock:
            result = self.client.upload_image("/fake/path.png")
            mock.assert_called_once_with("/fake/path.png")
            self.assertEqual(result["name"], "x.png")

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.Path")
    def test_upload_streams_chunks_and_sanitizes_crlf_filename(
        self, path_class: MagicMock, mock_post: MagicMock
    ) -> None:
        mock_post.return_value = _streaming_response(b'{"name":"safe.png"}')
        path = MagicMock()
        path.name = "cat.png\r\nX-Injected: yes"
        path.stat.return_value.st_size = 7
        handle = MagicMock()
        handle.read.side_effect = [b"payload", b""]
        path.open.return_value.__enter__.return_value = handle
        path_class.return_value = path

        self.client.upload_file("ignored")

        stream = mock_post.call_args.kwargs["data"]
        assert not isinstance(stream, (bytes, bytearray))
        body = b"".join(stream)
        assert b"X-Injected: yes" not in body
        assert b"payload" in body
        assert b"\r\n--" in body
        handle.read.assert_called_with(64 * 1024)

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_upload_mask_has_well_formed_multipart_boundaries(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _streaming_response(b'{"name":"mask.png"}')

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as file:
            file.write(b"mask")
            path = file.name
        try:
            self.client.upload_mask(path, original_ref="source.png")
            body = b"".join(mock_post.call_args.kwargs["data"])
            content_type = mock_post.call_args.kwargs["headers"]["Content-Type"]
            boundary = content_type.split("boundary=", 1)[1].encode("ascii")
            assert body.endswith(b"--" + boundary + b"--\r\n")
            assert b"\r\n--" + boundary + b"\r\n" in body
            assert b'name="original_ref"\r\n\r\nsource.png\r\n' in body
        finally:
            os.unlink(path)

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_upload_redirect_is_rejected_without_reading_body(self, mock_post: MagicMock) -> None:
        response = _streaming_response(
            b"redirect body",
            status_code=302,
            headers={"Location": "http://internal.example/private"},
        )
        mock_post.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(b"payload")
            with self.assertRaises(requests.HTTPError):
                self.client.upload_file(str(path))

        self.assertFalse(mock_post.call_args.kwargs["allow_redirects"])
        response.iter_content.assert_not_called()
        response.close.assert_called_once()

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_upload_mask_redirect_is_rejected(self, mock_post: MagicMock) -> None:
        response = _streaming_response(
            b"redirect body",
            status_code=303,
            headers={"Location": "http://internal.example/private"},
        )
        mock_post.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mask.png"
            path.write_bytes(b"payload")
            with self.assertRaises(requests.HTTPError):
                self.client.upload_mask(str(path), original_ref="source.png")

        self.assertIn("/upload/mask", mock_post.call_args.args[0])
        self.assertFalse(mock_post.call_args.kwargs["allow_redirects"])
        response.iter_content.assert_not_called()
        response.close.assert_called_once()

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_upload_receipt_rejects_declared_and_chunked_oversize(
        self, mock_post: MagicMock
    ) -> None:
        declared = _streaming_response(b"{}", headers={"Content-Length": str(64 * 1024 + 1)})
        chunked = _streaming_response(b"x" * (64 * 1024 + 1), headers={})
        mock_post.side_effect = [declared, chunked]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(b"payload")
            for response in (declared, chunked):
                with self.subTest(response=response):
                    with self.assertRaisesRegex(ValueError, "too large"):
                        self.client.upload_file(str(path))
                    response.close.assert_called_once()

        declared.iter_content.assert_not_called()
        chunked.iter_content.assert_called_once_with(chunk_size=64 * 1024)

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_upload_receipt_rejects_gzip_without_reading_body(self, mock_post: MagicMock) -> None:
        response = _streaming_response(
            b"compressed",
            headers={"Content-Encoding": "gzip", "Content-Length": "10"},
        )
        mock_post.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(b"payload")
            with self.assertRaisesRegex(ValueError, "Content-Encoding"):
                self.client.upload_file(str(path))

        response.iter_content.assert_not_called()
        response.close.assert_called_once()

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_upload_receipt_bounds_content_length_and_json_numbers(
        self, mock_post: MagicMock
    ) -> None:
        invalid_length = _streaming_response(b"{}", headers={"Content-Length": "9" * 129})
        huge_number = _streaming_response(b'{"name":"image.png","number":' + b"9" * 129 + b"}")
        mock_post.side_effect = [invalid_length, huge_number]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(b"payload")
            with self.assertRaisesRegex(ValueError, "Content-Length"):
                self.client.upload_file(str(path))
            with self.assertRaisesRegex(ValueError, "invalid"):
                self.client.upload_file(str(path))

        invalid_length.iter_content.assert_not_called()
        invalid_length.close.assert_called_once()
        huge_number.close.assert_called_once()

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_upload_receipt_must_be_json_object(self, mock_post: MagicMock) -> None:
        response = _streaming_response(b"[]")
        mock_post.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(b"payload")
            with self.assertRaisesRegex(ValueError, "invalid"):
                self.client.upload_file(str(path))

        response.close.assert_called_once()

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.post")
    def test_upload_receipt_stream_failure_closes_response(self, mock_post: MagicMock) -> None:
        response = _streaming_response(b"{}", headers={})
        response.iter_content.side_effect = requests.ConnectionError(
            "https://secret.internal/receipt failed"
        )
        mock_post.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(b"payload")
            with self.assertRaises(requests.ConnectionError):
                self.client.upload_file(str(path))

        response.close.assert_called_once()


# -- object_info --


class ObjectInfoNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ComfyUIClient("http://localhost:8188")

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_found(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"KSampler": {"display_name": "KSampler", "category": "sampling"}},
        )
        result = self.client.get_object_info_node("KSampler")
        self.assertIsNotNone(result)
        self.assertEqual(result["display_name"], "KSampler")

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_not_found(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(status_code=404)
        result = self.client.get_object_info_node("NonExistentNode")
        self.assertIsNone(result)

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_percent_encodes_node_class_path_segment(self, mock_get: MagicMock) -> None:
        node_class = "Custom/Node?view=full"
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {node_class: {"display_name": "Custom Node"}},
        )

        result = self.client.get_object_info_node(node_class)

        self.assertEqual(result, {"display_name": "Custom Node"})
        self.assertIn("/object_info/Custom%2FNode%3Fview%3Dfull", mock_get.call_args.args[0])

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_percent_encodes_model_folder_path_segment(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(status_code=200, json=lambda: [])

        self.client.get_models("checkpoints/../queue?raw=1")

        self.assertIn(
            "/models/checkpoints%2F..%2Fqueue%3Fraw%3D1",
            mock_get.call_args.args[0],
        )


# -- system_stats --

SAMPLE_STATS = {
    "system": {
        "os": "posix",
        "ram_total": 68719476736,
        "ram_free": 32000000000,
        "comfyui_version": "v0.3.10",
        "python_version": "3.11.5",
        "pytorch_version": "2.1.0",
        "embedded_python": False,
    },
    "devices": [
        {
            "name": "cuda:0",
            "type": "cuda",
            "index": 0,
            "vram_total": 25769803776,
            "vram_free": 20000000000,
            "torch_vram_total": 25769803776,
            "torch_vram_free": 22000000000,
        }
    ],
}


class SystemStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ComfyUIClient("http://localhost:8188")

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_success(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(status_code=200, json=lambda: SAMPLE_STATS)
        result = self.client.get_system_stats()
        self.assertEqual(result, SAMPLE_STATS)

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_raises_on_error(self, mock_get: MagicMock) -> None:
        mock_resp = MagicMock(status_code=500)
        mock_resp.raise_for_status.side_effect = Exception("Server error")
        mock_get.return_value = mock_resp
        with self.assertRaises(Exception):
            self.client.get_system_stats()

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_multi_server(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(status_code=200, json=lambda: SAMPLE_STATS)
        clients = [ComfyUIClient("http://s1:8188"), ComfyUIClient("http://s2:8188")]
        results = [c.get_system_stats() for c in clients]
        self.assertEqual(len(results), 2)
        self.assertEqual(mock_get.call_count, 2)


# -- ws_events --


class WsEventsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ComfyUIClient("http://localhost:8188")

    @patch("websocket.create_connection")
    def test_yields_matching_events(self, mock_ws_create: MagicMock) -> None:
        import websocket

        mock_ws = MagicMock()
        mock_ws_create.return_value = mock_ws
        events = [
            (
                websocket.ABNF.OPCODE_TEXT,
                json.dumps({"type": "execution_start", "data": {"prompt_id": "p-1"}}).encode(),
            ),
            (
                websocket.ABNF.OPCODE_TEXT,
                json.dumps(
                    {"type": "executing", "data": {"node": "5", "prompt_id": "p-1"}}
                ).encode(),
            ),
            (websocket.ABNF.OPCODE_BINARY, b"\x01\x00\x00\x00fake-preview"),
            (
                websocket.ABNF.OPCODE_TEXT,
                json.dumps(
                    {"type": "executing", "data": {"node": None, "prompt_id": "p-1"}}
                ).encode(),
            ),
        ]
        mock_ws.recv_data = MagicMock(side_effect=events)
        collected = list(self.client.ws_events("cid-1", "p-1"))
        self.assertEqual(len(collected), 3)
        self.assertEqual(collected[0]["type"], "execution_start")
        self.assertEqual(collected[1]["data"]["node"], "5")
        self.assertIsNone(collected[2]["data"]["node"])
        mock_ws.close.assert_called_once()

    @patch("websocket.create_connection")
    def test_forwards_bearer_auth_header(self, mock_ws_create: MagicMock) -> None:
        import websocket

        authenticated = ComfyUIClient("http://localhost:8188", auth="secret-token")
        mock_ws = MagicMock()
        mock_ws.recv_data.return_value = (
            websocket.ABNF.OPCODE_TEXT,
            json.dumps(
                {
                    "type": "executing",
                    "data": {"node": None, "prompt_id": "p-1"},
                }
            ).encode(),
        )
        mock_ws_create.return_value = mock_ws

        list(authenticated.ws_events("cid-1", "p-1"))

        assert mock_ws_create.call_args.kwargs["header"] == {"Authorization": "Bearer secret-token"}

    @patch("websocket.create_connection")
    def test_filters_other_prompts(self, mock_ws_create: MagicMock) -> None:
        import websocket

        mock_ws = MagicMock()
        mock_ws_create.return_value = mock_ws
        events = [
            (
                websocket.ABNF.OPCODE_TEXT,
                json.dumps(
                    {"type": "executing", "data": {"node": "1", "prompt_id": "other"}}
                ).encode(),
            ),
            (
                websocket.ABNF.OPCODE_TEXT,
                json.dumps(
                    {"type": "executing", "data": {"node": None, "prompt_id": "p-1"}}
                ).encode(),
            ),
        ]
        mock_ws.recv_data = MagicMock(side_effect=events)
        collected = list(self.client.ws_events("cid-1", "p-1"))
        self.assertEqual(len(collected), 1)

    @patch("websocket.create_connection")
    def test_stops_on_error(self, mock_ws_create: MagicMock) -> None:
        import websocket

        mock_ws = MagicMock()
        mock_ws_create.return_value = mock_ws
        events = [
            (
                websocket.ABNF.OPCODE_TEXT,
                json.dumps(
                    {
                        "type": "execution_error",
                        "data": {"prompt_id": "p-1", "exception_message": "boom"},
                    }
                ).encode(),
            ),
        ]
        mock_ws.recv_data = MagicMock(side_effect=events)
        collected = list(self.client.ws_events("cid-1", "p-1"))
        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0]["type"], "execution_error")

    @patch("websocket.create_connection")
    def test_silent_socket_stops_at_deadline(self, mock_ws_create: MagicMock) -> None:
        import websocket

        mock_ws = MagicMock()
        mock_ws.recv_data.side_effect = websocket.WebSocketTimeoutException()
        mock_ws_create.return_value = mock_ws

        collected = list(self.client.ws_events("cid-1", "p-1", timeout_seconds=0.01))

        self.assertEqual(collected, [])
        mock_ws.close.assert_called_once()

    @patch("websocket.create_connection")
    def test_silent_socket_honors_cancellation(self, mock_ws_create: MagicMock) -> None:
        import websocket

        class Cancelled(BaseException):
            pass

        checks = 0

        def cancel_check() -> None:
            nonlocal checks
            checks += 1
            if checks > 1:
                raise Cancelled()

        mock_ws = MagicMock()
        mock_ws.recv_data.side_effect = websocket.WebSocketTimeoutException()
        mock_ws_create.return_value = mock_ws

        with self.assertRaises(Cancelled):
            list(
                self.client.ws_events("cid-1", "p-1", timeout_seconds=10, cancel_check=cancel_check)
            )
        mock_ws.close.assert_called_once()


# -- userdata workflows --


class ListUserdataWorkflowsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ComfyUIClient("http://localhost:8188")

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_v2_userdata_returns_list_of_dicts(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"name": "a.json", "path": "workflows/a.json", "type": "file"},
                {"name": "b.json", "path": "workflows/b.json", "type": "file"},
            ],
        )
        paths = self.client.list_userdata_workflows()
        self.assertEqual(paths, ["workflows/a.json", "workflows/b.json"])
        self.assertEqual(mock_get.call_args.kwargs["params"], {"path": "workflows"})

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_falls_back_to_userdata_bare_strings(self, mock_get: MagicMock) -> None:
        # /v2/userdata returns empty, /userdata returns bare filenames
        mock_get.side_effect = [
            MagicMock(status_code=200, json=lambda: []),
            MagicMock(status_code=200, json=lambda: ["a.json", "b.json"]),
        ]
        paths = self.client.list_userdata_workflows()
        self.assertEqual(paths, ["workflows/a.json", "workflows/b.json"])

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_both_endpoints_empty(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(status_code=200, json=lambda: [])
        self.assertEqual(self.client.list_userdata_workflows(), [])

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_filters_non_json_entries(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"path": "workflows/a.json"},
                {"path": "comfy.settings.json"},
                {"path": "workflows/readme.txt"},
            ],
        )
        paths = self.client.list_userdata_workflows()
        self.assertEqual(paths, ["workflows/a.json", "comfy.settings.json"])


class ReadUserdataWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ComfyUIClient("http://localhost:8188")

    @staticmethod
    def _ok_response(payload: bytes) -> MagicMock:
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.headers = {"Content-Length": str(len(payload))}
        response.iter_content = lambda chunk_size: [
            payload[index : index + chunk_size]
            for index in range(0, len(payload), chunk_size)
        ]
        return response

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_percent_encodes_full_path(self, mock_get: MagicMock) -> None:
        mock_get.return_value = self._ok_response(b'{"nodes": []}')
        self.client.read_userdata_workflow("workflows/MultiCharacter.json")
        called_url = mock_get.call_args.args[0]
        self.assertIn("/userdata/workflows%2FMultiCharacter.json", called_url)
        self.assertNotIn("/userdata/workflows/MultiCharacter.json", called_url)

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_returns_none_on_404(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(status_code=404)
        mock_get.return_value.raise_for_status.side_effect = requests.HTTPError("404")
        self.assertIsNone(self.client.read_userdata_workflow("workflows/missing.json"))

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_rejects_oversized_declared_body(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(status_code=200)
        mock_get.return_value.raise_for_status = MagicMock()
        mock_get.return_value.headers = {"Content-Length": str(2 * 1024 * 1024 + 1)}
        with self.assertRaises(ValueError):
            self.client.read_userdata_workflow("workflows/huge.json")

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_rejects_oversized_chunked_body(self, mock_get: MagicMock) -> None:
        response = self._ok_response(b"x" * (2 * 1024 * 1024 + 1))
        del response.headers["Content-Length"]  # no declared length; streaming cap
        mock_get.return_value = response
        with self.assertRaises(ValueError):
            self.client.read_userdata_workflow("workflows/huge.json")


class DownloadOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ComfyUIClient("http://localhost:8188")

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_streams_output_to_atomic_destination(self, mock_get: MagicMock) -> None:
        response = MagicMock()
        response.headers = {"content-length": "6"}
        response.iter_content.return_value = [b"abc", b"def"]
        mock_get.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "output.bin"
            receipt = self.client.download_output_to("output.bin", destination, max_bytes=6)
            self.assertEqual(
                receipt,
                {"size_bytes": 6, "sha256": hashlib.sha256(b"abcdef").hexdigest()},
            )
            self.assertEqual(destination.read_bytes(), b"abcdef")
        response.close.assert_called_once()
        self.assertFalse(mock_get.call_args.kwargs["allow_redirects"])
        self.assertTrue(mock_get.call_args.kwargs["stream"])
        self.assertEqual(mock_get.call_args.kwargs["headers"]["Accept-Encoding"], "identity")

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_stream_failure_cleans_partial_file_and_preserves_destination(
        self, mock_get: MagicMock
    ) -> None:
        response = MagicMock()
        response.headers = {}
        response.iter_content.return_value = [b"abcd", b"efgh"]
        mock_get.return_value = response

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "output.bin"
            destination.write_bytes(b"existing")

            with self.assertRaisesRegex(ValueError, "byte limit"):
                self.client.download_output_to("output.bin", destination, max_bytes=6)

            self.assertEqual(destination.read_bytes(), b"existing")
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["output.bin"])
        response.iter_content.assert_called_once_with(chunk_size=64 * 1024)
        response.close.assert_called_once()

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_rejects_stream_exceeding_declared_limit(self, mock_get: MagicMock) -> None:
        response = MagicMock()
        response.headers = {}
        response.iter_content.return_value = [b"abcd", b"efgh"]
        mock_get.return_value = response

        with self.assertRaisesRegex(ValueError, "byte limit"):
            self.client.download_output("large.bin", max_bytes=4)
        response.close.assert_called_once()

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_download_redirect_is_rejected_and_destination_is_unchanged(
        self, mock_get: MagicMock
    ) -> None:
        response = MagicMock(status_code=307)
        response.headers = {"Location": "http://internal.example/private"}
        mock_get.return_value = response

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "output.bin"
            destination.write_bytes(b"existing")
            with self.assertRaises(requests.HTTPError):
                self.client.download_output_to("output.bin", destination, max_bytes=1024)
            self.assertEqual(destination.read_bytes(), b"existing")
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["output.bin"])

        self.assertFalse(mock_get.call_args.kwargs["allow_redirects"])
        response.iter_content.assert_not_called()
        response.close.assert_called_once()

    @patch("comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get")
    def test_download_rejects_encoded_response_and_cleans_partial_state(
        self, mock_get: MagicMock
    ) -> None:
        response = MagicMock(status_code=200)
        response.headers = {"Content-Encoding": "gzip", "Content-Length": "10"}
        mock_get.return_value = response

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "output.bin"
            with self.assertRaisesRegex(ValueError, "Content-Encoding"):
                self.client.download_output_to("output.bin", destination, max_bytes=1024)
            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

        response.iter_content.assert_not_called()
        response.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
