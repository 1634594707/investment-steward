"""读取文件/目录的强制完整性级别 (Mandatory Integrity Level)。

背景：NSIS 安装包报 "Error writing temporary file. Make sure your temp folder is valid."
的一个已知根因是——EXE 所在卷/目录带非默认完整性标签，进程继承 Low IL 后
无权写入 Medium IL 的 %TEMP%。

实现说明（踩过的坑）：不要在 64 位下用无 argtypes 的 ctypes.windll 调 GetAce，
指针会被截断成 32 位导致进程直接崩溃(exit 127)。这里改为手工解析 SACL 内存布局。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

advapi32 = ctypes.windll.advapi32
kernel32 = ctypes.windll.kernel32

SE_FILE_OBJECT = 1
LABEL_SECURITY_INFORMATION = 0x00000010
SYSTEM_MANDATORY_LABEL_ACE_TYPE = 0x11

advapi32.GetNamedSecurityInfoW.argtypes = [
    wintypes.LPCWSTR,
    ctypes.c_int,
    wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
]
advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD

# 64 位下必须声明 argtypes，否则指针参数被截断成 32 位 -> 堆损坏 (0xc0000374)
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.LocalFree.restype = ctypes.c_void_p
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
advapi32.GetTokenInformation.restype = wintypes.BOOL

MANDATORY_LEVELS = {
    0x0000: "Untrusted",
    0x1000: "Low",
    0x2000: "Medium",
    0x2100: "Medium-Plus",
    0x3000: "High",
    0x4000: "System",
    0x5000: "Protected",
}


def _u8(p: int, off: int) -> int:
    return ctypes.cast(p + off, ctypes.POINTER(ctypes.c_ubyte))[0]


def _u16(p: int, off: int) -> int:
    return ctypes.cast(p + off, ctypes.POINTER(wintypes.WORD))[0]


def _u32(p: int, off: int) -> int:
    return ctypes.cast(p + off, ctypes.POINTER(wintypes.DWORD))[0]


def sid_last_subauth(sid_ptr: int) -> int:
    count = _u8(sid_ptr, 1)
    base = sid_ptr + 8  # Revision(1)+SubAuthCount(1)+IdentifierAuthority(6)
    return _u32(base, (count - 1) * 4)


def get_integrity(path: str) -> str:
    psd = ctypes.c_void_p()
    psacl = ctypes.c_void_p()
    rc = advapi32.GetNamedSecurityInfoW(
        path, SE_FILE_OBJECT, LABEL_SECURITY_INFORMATION,
        None, None, None, ctypes.byref(psacl), ctypes.byref(psd),
    )
    if rc != 0:
        return f"<读取失败 rc={rc}>"
    try:
        if not psacl.value:
            return "无 mandatory label (未设完整性标签 -> 继承父级)"
        acl = psacl.value
        ace_count = _u16(acl, 4)
        if ace_count == 0:
            return "SACL 为空"
        off = acl + 8  # 跳过 ACL 头 8 字节
        for _ in range(ace_count):
            ace_type = _u8(off, 0)
            ace_size = _u16(off, 2)
            if ace_type == SYSTEM_MANDATORY_LABEL_ACE_TYPE:
                mask = _u32(off, 4)
                rid = sid_last_subauth(off + 8)
                name = MANDATORY_LEVELS.get(rid, f"RID=0x{rid:X}")
                return f"{name}  (mask=0x{mask:X})"
            if ace_size <= 0:
                break
            off += ace_size
        return "未找到 mandatory label ACE"
    finally:
        # 注意：ppSacl 是指向 ppSecurityDescriptor 内部的指针，不是独立分配，
        # 只能释放 SD，否则 double free -> 堆损坏 0xc0000374
        if psd.value:
            kernel32.LocalFree(psd)


TOKEN_QUERY = 0x0008
TokenIntegrityLevel = 25


def process_integrity() -> str:
    h = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(h)):
        return "<OpenProcessToken 失败>"
    size = wintypes.DWORD()
    advapi32.GetTokenInformation(h, TokenIntegrityLevel, None, 0, ctypes.byref(size))
    buf = ctypes.create_string_buffer(size.value)
    if not advapi32.GetTokenInformation(h, TokenIntegrityLevel, buf, size.value, ctypes.byref(size)):
        return "<GetTokenInformation 失败>"
    sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
    rid = sid_last_subauth(sid_ptr)
    return f"{MANDATORY_LEVELS.get(rid, hex(rid))} (RID=0x{rid:X})"


TARGETS = [
    r"D:\Administrator\Desktop\quant\investment-steward\dist-desktop2\Investment Steward Setup 0.1.9.exe",
    r"D:\Administrator\Desktop\quant\investment-steward\dist-desktop2",
    r"D:\Administrator\Desktop\quant\investment-steward",
    r"D:\Administrator\Desktop",
    r"D:\Administrator",
    "D:\\",
    os.path.join(os.environ["LOCALAPPDATA"], "Temp"),
    "C:\\",
]

if __name__ == "__main__":
    print("当前进程完整性级别:", process_integrity())
    print()
    print(f"{'路径':<76} 完整性级别")
    print("-" * 108)
    for t in TARGETS:
        label = get_integrity(t) if os.path.exists(t) else "<不存在>"
        print(f"{t:<76} {label}")
