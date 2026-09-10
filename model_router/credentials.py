"""Optional current-Windows-user DPAPI storage; never store plaintext secrets."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]


def _crypt(value: bytes, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("加密保存仅支持 Windows；其他系统请使用进程环境变量")
    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        free = ctypes.WinDLL("kernel32").LocalFree
        free.argtypes = [ctypes.c_void_p]
        free(ctypes.cast(target.data, ctypes.c_void_p))


def save_secret(path: Path, secret: str):
    encrypted = _crypt(secret.encode("utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encrypted)


def load_secret(path: Path):
    return _crypt(path.read_bytes(), decrypt=True).decode("utf-8") if path.exists() else None
