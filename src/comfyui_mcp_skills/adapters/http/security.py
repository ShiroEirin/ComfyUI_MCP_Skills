"""Remote HTTPS download policy with pinned DNS, allowlists, and size limits."""

from __future__ import annotations

import ipaddress
import socket
import ssl
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunsplit

import certifi
import urllib3

from comfyui_mcp_skills.domain.errors import PayloadTooLarge, UnsafePath, UploadFailed


class SafeHTTPSDownloader:
    def __init__(
        self,
        *,
        allowed_hosts: list[str] | None = None,
        max_bytes: int = 25 * 1024 * 1024,
        max_redirects: int = 3,
        timeout_seconds: float = 30,
    ) -> None:
        self._allowed_hosts = frozenset(host.lower() for host in (allowed_hosts or []))
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._timeout_seconds = timeout_seconds

    def download(self, url: str, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._timeout_seconds
        current = url
        for _ in range(self._max_redirects + 1):
            parsed, address = self._validated_target(current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UploadFailed("Remote HTTPS download timed out")
            pool = urllib3.HTTPSConnectionPool(
                address,
                port=443,
                timeout=urllib3.Timeout(connect=min(5, remaining), read=min(5, remaining)),
                retries=False,
                cert_reqs=ssl.CERT_REQUIRED,
                ca_certs=certifi.where(),
                assert_hostname=parsed.hostname,
                server_hostname=parsed.hostname,
            )
            path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            response = None
            try:
                response = pool.request(
                    "GET",
                    path,
                    headers={"Host": parsed.hostname or ""},
                    redirect=False,
                    preload_content=False,
                )
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "")
                    if not location:
                        raise UnsafePath("HTTPS redirect has no location")
                    current = urljoin(current, location)
                    continue
                if response.status >= 400:
                    raise UploadFailed(f"Remote HTTPS server returned status {response.status}")
                return self._write_response(response, parsed.path, directory, deadline)
            except urllib3.exceptions.HTTPError as exc:
                raise UploadFailed("Remote HTTPS request failed") from exc
            finally:
                if response is not None:
                    response.release_conn()
                pool.close()
        raise UnsafePath("HTTPS resource exceeded redirect limit")

    def _write_response(
        self,
        response: urllib3.BaseHTTPResponse,
        url_path: str,
        directory: Path,
        deadline: float,
    ) -> Path:
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise UploadFailed("Remote HTTPS content length is invalid") from exc
            if declared_size > self._max_bytes:
                raise PayloadTooLarge(f"Remote asset exceeds {self._max_bytes} bytes")
        suffix = Path(url_path).suffix.lower()[:10] or ".bin"
        destination = directory / f"remote-{uuid.uuid4().hex}{suffix}"
        size = 0
        try:
            with destination.open("xb") as handle:
                for chunk in response.stream(64 * 1024):
                    if time.monotonic() >= deadline:
                        raise UploadFailed("Remote HTTPS download timed out")
                    size += len(chunk)
                    if size > self._max_bytes:
                        raise PayloadTooLarge(f"Remote asset exceeds {self._max_bytes} bytes")
                    handle.write(chunk)
            return destination
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def _validated_target(self, url: str):
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise UnsafePath("Remote assets require an HTTPS URL without credentials")
        hostname = parsed.hostname.lower()
        if hostname not in self._allowed_hosts:
            raise UnsafePath("Remote asset host is not allowlisted")
        if parsed.port not in (None, 443):
            raise UnsafePath("Remote asset HTTPS port must be 443")
        try:
            addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafePath("Remote asset host could not be resolved") from exc
        public_addresses: list[str] = []
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise UnsafePath("Remote asset resolves to a non-public address")
            public_addresses.append(str(ip))
        if not public_addresses:
            raise UnsafePath("Remote asset host has no address")
        return parsed, public_addresses[0]
