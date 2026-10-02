"""Windows DPAPI protection for the provider API key."""

from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes


class _DATA_BLOB(ctypes.Structure):
    # DATA_BLOB is DWORD + BYTE*. Both native functions take pointers to it.
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]

    def __init__(self, raw: bytes | None = None):
        super().__init__()
        self._buffer = None
        if raw is not None:
            self._buffer = ctypes.create_string_buffer(raw)
            self.cbData = len(raw)
            self.pbData = ctypes.cast(self._buffer, ctypes.c_void_p)

    def bytes(self) -> bytes:
        return ctypes.string_at(self.pbData, self.cbData)


def protect(value: str) -> str:
    if not value:
        return ""
    raw = value.encode("utf-8")
    if sys.platform == "win32":
        in_blob = _DATA_BLOB(raw)
        out_blob = _DATA_BLOB()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True).CryptProtectData
        crypt.argtypes = [ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(_DATA_BLOB), wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB)]
        crypt.restype = wintypes.BOOL
        if not crypt(ctypes.byref(in_blob), "PaperTranslator API key", None, None, None, 1, ctypes.byref(out_blob)):
            raise OSError(ctypes.get_last_error(), "Windows DPAPI CryptProtectData failed; API Key will not be persisted")
        try:
            encrypted = out_blob.bytes()
        finally:
            _local_free(out_blob.pbData)
        return "dpapi:" + base64.b64encode(encrypted).decode("ascii")
    raise OSError("非 Windows 环境不支持持久化 API Key；请仅在 Windows 本机运行。")


def unprotect(value: str) -> str:
    if not value:
        return ""
    if value.startswith("dpapi:") and sys.platform == "win32":
        encrypted = base64.b64decode(value[6:])
        in_blob = _DATA_BLOB(encrypted)
        out_blob = _DATA_BLOB()
        description = wintypes.LPWSTR()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True).CryptUnprotectData
        crypt.argtypes = [ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DATA_BLOB), wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB)]
        crypt.restype = wintypes.BOOL
        if not crypt(ctypes.byref(in_blob), ctypes.byref(description), None, None, None, 1, ctypes.byref(out_blob)):
            raise OSError(ctypes.get_last_error(), "Windows DPAPI CryptUnprotectData failed")
        try:
            raw = out_blob.bytes()
        finally:
            _local_free(out_blob.pbData)
            if description:
                _local_free(ctypes.cast(description, ctypes.c_void_p))
        return raw.decode("utf-8")
    # Never accept a legacy plaintext value from disk.
    return ""


def _local_free(pointer: object) -> None:
    free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    free(pointer)


def mask(value: str) -> str:
    if not value:
        return "未设置"
    if len(value) <= 8:
        return "•" * len(value)
    return value[:3] + "•" * max(4, len(value) - 7) + value[-4:]
