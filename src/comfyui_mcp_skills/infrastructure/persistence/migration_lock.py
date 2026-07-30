"""Trusted per-project migration lock outside mutable legacy data trees."""

from __future__ import annotations

import hashlib
import os
import stat
import threading
import time
from pathlib import Path
from types import TracebackType


class ProjectMigrationLock:
    """Cross-process lock that never follows a project-controlled lock path."""

    def __init__(self, project_key: str, timeout: float) -> None:
        self._project_key = project_key
        self._timeout = timeout
        self._local = threading.local()

    def acquire(self) -> None:
        depth = int(getattr(self._local, "depth", 0))
        if depth:
            self._local.depth = depth + 1
            return
        if os.name == "nt":
            handle = _acquire_windows_mutex(self._project_key, self._timeout)
        else:
            handle = _acquire_posix_lock(self._project_key, self._timeout)
        self._local.handle = handle
        self._local.depth = 1

    def release(self) -> None:
        depth = int(getattr(self._local, "depth", 0))
        if not depth:
            return
        if depth > 1:
            self._local.depth = depth - 1
            return
        handle = int(self._local.handle)
        del self._local.handle
        del self._local.depth
        if os.name == "nt":
            _release_windows_mutex(handle)
        else:
            _release_posix_lock(handle)

    def __enter__(self) -> ProjectMigrationLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def project_migration_lock(base_dir: str | Path, *, timeout: float = 10) -> ProjectMigrationLock:
    project = Path(base_dir).resolve(strict=False)
    try:
        info = project.stat()
        identity = f"{info.st_dev}:{info.st_ino}:{os.path.normcase(str(project))}"
    except OSError:
        identity = os.path.normcase(str(project))
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return ProjectMigrationLock(key, timeout)


def _acquire_posix_lock(project_key: str, timeout: float) -> int:
    import fcntl

    geteuid = getattr(os, "geteuid")
    effective_uid = int(geteuid())
    flock = getattr(fcntl, "flock")
    lock_ex = int(getattr(fcntl, "LOCK_EX"))
    lock_nb = int(getattr(fcntl, "LOCK_NB"))

    path = Path("/tmp") / f"comfyui-mcp-skills-{effective_uid}-{project_key}.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("migration lock must be a single regular file")
        if info.st_uid != effective_uid:
            raise ValueError("migration lock must be owned by the effective user")
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError("migration lock must be private")
        deadline = time.monotonic() + timeout
        while True:
            try:
                flock(descriptor, lock_ex | lock_nb)
                return descriptor
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out acquiring project migration lock") from None
                time.sleep(0.05)
    except BaseException:
        os.close(descriptor)
        raise


def _release_posix_lock(descriptor: int) -> None:
    import fcntl

    flock = getattr(fcntl, "flock")
    lock_un = int(getattr(fcntl, "LOCK_UN"))

    try:
        flock(descriptor, lock_un)
    finally:
        os.close(descriptor)


def _windows_current_sid() -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    )
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")
        sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID)).contents
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
        try:
            return str(sid_text.value)
        finally:
            kernel32.LocalFree(sid_text)
    finally:
        kernel32.CloseHandle(token)


def _acquire_windows_mutex(project_key: str, timeout: float) -> int:
    import ctypes
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    )
    convert.restype = wintypes.BOOL
    descriptor = wintypes.LPVOID()
    current_sid = _windows_current_sid()
    sddl = f"O:{current_sid}D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;{current_sid})"
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise OSError(ctypes.get_last_error(), "security descriptor creation failed")
    attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (
        ctypes.POINTER(SecurityAttributes),
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    create_mutex.restype = wintypes.HANDLE
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait_for_single_object.restype = wintypes.DWORD
    try:
        handle = create_mutex(
            ctypes.byref(attributes),
            False,
            f"Global\\ComfyUIMcpMigration-{current_sid}-{project_key}",
        )
    finally:
        kernel32.LocalFree(descriptor)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
    result = wait_for_single_object(handle, max(0, int(timeout * 1000)))
    if result not in {0x00000000, 0x00000080}:
        kernel32.CloseHandle(handle)
        if result == 0x00000102:
            raise TimeoutError("timed out acquiring project migration lock")
        raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")
    return int(handle)


def _release_windows_mutex(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    try:
        if not kernel32.ReleaseMutex(handle):
            raise OSError(ctypes.get_last_error(), "ReleaseMutex failed")
    finally:
        kernel32.CloseHandle(handle)
