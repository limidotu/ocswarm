import ctypes
import os
from ctypes import wintypes
from pathlib import Path


KEY_PATH = Path.home() / ".local" / "share" / "ocswarm" / "nvidia-key.dpapi"


class DataBlob(ctypes.Structure):
	_fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data, protect):
	if os.name != "nt":
		raise OSError("Windows encryption is unavailable. Set NVIDIA_API_KEY in the environment.")
	buffer = ctypes.create_string_buffer(data)
	source = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
	result = DataBlob()
	crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
	kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
	function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
	function.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
	function.restype = wintypes.BOOL
	kernel32.LocalFree.argtypes = [ctypes.c_void_p]
	kernel32.LocalFree.restype = ctypes.c_void_p
	if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
		raise ctypes.WinError(ctypes.get_last_error())
	try:
		return ctypes.string_at(result.data, result.size)
	finally:
		kernel32.LocalFree(result.data)


def save_key(key):
	key = key.strip()
	if not key.startswith("nvapi-") or len(key) < 20:
		raise ValueError("The NVIDIA key is invalid.")
	encrypted = _crypt(key.encode("utf-8"), True)
	KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
	tmp = KEY_PATH.with_suffix(".tmp")
	tmp.write_bytes(encrypted)
	tmp.replace(KEY_PATH)


def load_key():
	key = os.environ.get("NVIDIA_API_KEY")
	if key:
		return key
	if not KEY_PATH.is_file():
		return ""
	return _crypt(KEY_PATH.read_bytes(), False).decode("utf-8")
