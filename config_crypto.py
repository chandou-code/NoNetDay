"""
config.json 的 DPAPI 加密 / 解密模块

DPAPI（Data Protection API）是 Windows 的加密服务：
- 密钥由 Windows 管理，派生自用户登录密码 + 机器信息
- 同一 Windows 账户能解密，换账户 / 换电脑 / 重装系统都解不开
- 密钥不暴露在代码里，攻击者读代码也拿不到明文

文件格式：
    [4字节 magic][加密的密文]
    magic = b"DPAPI" 用于识别加密文件
    如果文件不以 magic 开头，视为旧版明文 config，自动迁移到加密格式

使用：
    from config_crypto import load_config, save_config
    cfg = load_config()      # 自动解密
    save_config(cfg)         # 自动加密
"""

import os
import json
import struct
from typing import Optional

# DPAPI 通过 ctypes 调用 Windows API
import ctypes
import ctypes.wintypes as wt

# ----------
# DPAPI 调用
# ----------
class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wt.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]

# CryptProtectData / CryptUnprotectData 函数原型
_crypt32 = ctypes.windll.crypt32
_kernel32 = ctypes.windll.kernel32

_crypt32.CryptProtectData.restype = wt.BOOL
_crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(DATA_BLOB),   # DataIn
    ctypes.c_wchar_p,            # DataDescr
    ctypes.POINTER(DATA_BLOB),   # OptionalEntropy
    ctypes.c_void_p,             # Reserved
    ctypes.c_void_p,             # PromptStruct
    wt.DWORD,                    # Flags
    ctypes.POINTER(DATA_BLOB),   # DataOut
]

_crypt32.CryptUnprotectData.restype = wt.BOOL
_crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(DATA_BLOB),   # DataIn
    ctypes.POINTER(ctypes.c_wchar_p),  # DataDescrOut
    ctypes.POINTER(DATA_BLOB),   # OptionalEntropy
    ctypes.c_void_p,             # Reserved
    ctypes.c_void_p,             # PromptStruct
    wt.DWORD,                    # Flags
    ctypes.POINTER(DATA_BLOB),   # DataOut
]

_kernel32.LocalFree.restype = wt.HLOCAL
_kernel32.LocalFree.argtypes = [wt.HLOCAL]


def dpapi_encrypt(plaintext: bytes) -> bytes:
    """用 DPAPI 加密字节串，返回加密后的字节"""
    blob_in = DATA_BLOB()
    blob_in.cbData = len(plaintext)
    blob_in.pbData = (ctypes.c_byte * len(plaintext)).from_buffer_copy(plaintext)
    blob_out = DATA_BLOB()

    ok = _crypt32.CryptProtectData(
        ctypes.byref(blob_in), "config", None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        encrypted = bytes(ctypes.string_at(blob_out.pbData, blob_out.cbData))
    finally:
        _kernel32.LocalFree(blob_out.pbData)
    return encrypted


def dpapi_decrypt(ciphertext: bytes) -> bytes:
    """用 DPAPI 解密字节串，返回原始字节"""
    blob_in = DATA_BLOB()
    blob_in.cbData = len(ciphertext)
    blob_in.pbData = (ctypes.c_byte * len(ciphertext)).from_buffer_copy(ciphertext)
    blob_out = DATA_BLOB()
    descr = ctypes.c_wchar_p()

    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), ctypes.byref(descr), None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        plaintext = bytes(ctypes.string_at(blob_out.pbData, blob_out.cbData))
    finally:
        _kernel32.LocalFree(blob_out.pbData)
    return plaintext


# ----------
# 文件读写（带 magic 头）
# ----------
MAGIC = b"DPAPI\x00\x01"  # 8 字节，标识加密格式


def is_encrypted(data: bytes) -> bool:
    return data.startswith(MAGIC)


def load_config(config_path: str) -> Optional[dict]:
    """
    读取 config：
    - 文件不存在 → 返回 None
    - 加密格式 → DPAPI 解密后解析 JSON
    - 明文格式（兼容旧版）→ 解析 JSON，并自动迁移到加密格式
    """
    if not os.path.exists(config_path):
        return None

    with open(config_path, "rb") as f:
        raw = f.read()

    if is_encrypted(raw):
        ciphertext = raw[len(MAGIC):]
        plaintext = dpapi_decrypt(ciphertext)
        return json.loads(plaintext.decode("utf-8"))
    else:
        # 明文 config（旧版），解析后自动加密迁移
        cfg = json.loads(raw.decode("utf-8"))
        save_config(cfg, config_path)
        return cfg


def save_config(cfg: dict, config_path: str) -> None:
    """加密后写入 config 文件"""
    plaintext = json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8")
    ciphertext = dpapi_encrypt(plaintext)
    with open(config_path, "wb") as f:
        f.write(MAGIC + ciphertext)


if __name__ == "__main__":
    # 自测：加解密一个测试 config
    test_cfg = {"state": "disabled", "cycle_seconds": 86400}
    test_path = "_test_dpapi.bin"
    print("写入测试 config ...")
    save_config(test_cfg, test_path)
    print("读回 ...")
    loaded = load_config(test_path)
    print(f"读回结果: {loaded}")
    print(f"匹配: {loaded == test_cfg}")

    # 看看文件内容（应该是二进制）
    with open(test_path, "rb") as f:
        data = f.read()
    print(f"文件大小: {len(data)} 字节")
    print(f"前 16 字节 hex: {data[:16].hex()}")
    print(f"以 magic 开头: {is_encrypted(data)}")

    os.remove(test_path)
    print("自测完成")
