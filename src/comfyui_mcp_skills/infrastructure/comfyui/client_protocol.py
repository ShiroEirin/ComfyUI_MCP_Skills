"""Static contract shared by responsibility-focused ComfyUI client mixins."""

from __future__ import annotations

from typing import Any, Protocol

import requests


class SharedClient(Protocol):
    server_url: str
    comfy_api_key: str
    timeout: float

    def _headers(self) -> dict[str, str]: ...

    def _get(self, path: str, **kwargs: Any) -> requests.Response: ...

    def _post(self, path: str, json_data: Any = None, **kwargs: Any) -> requests.Response: ...
