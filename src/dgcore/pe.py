"""极简 PE 解析器（纯标准库，只读不执行）。

用途：判断游戏主程序是否 64 位、是否导入 d3d12.dll、是否带 DLSS 组件。
只读取文件头与导入表，不加载、不执行目标文件，对系统无副作用。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

MACHINE_NAMES = {
    0x014C: "x86",
    0x8664: "x64",
    0xAA64: "ARM64",
    0x01C4: "ARMNT",
    0x0200: "IA64",
}

DIR_IMPORT = 1
DIR_DELAY_IMPORT = 13

# 只在导入表缺失时使用的兜底字符串扫描（动态 LoadLibrary 的场景）
_FALLBACK_NEEDLES = (
    b"d3d12.dll",
    b"D3D12CreateDevice",
    b"d3d12core.dll",
)
_MAX_SCAN_BYTES = 96 * 1024 * 1024
_CHUNK = 4 * 1024 * 1024


@dataclass
class Section:
    name: str
    virtual_size: int
    virtual_address: int
    raw_size: int
    raw_pointer: int


@dataclass
class PEInfo:
    path: str = ""
    ok: bool = False
    error: str = ""
    machine: int = 0
    arch: str = "unknown"
    is_pe: bool = False
    imports: tuple[str, ...] = ()
    char_hits: tuple[str, ...] = ()
    size: int = 0

    # 便捷判定
    @property
    def is_x64(self) -> bool:
        return self.machine == 0x8664

    @property
    def uses_d3d12(self) -> bool:
        low = {i.lower() for i in self.imports}
        if "d3d12.dll" in low or "d3d12core.dll" in low:
            return True
        return bool(self.char_hits)

    @property
    def uses_d3d11(self) -> bool:
        low = {i.lower() for i in self.imports}
        return "d3d11.dll" in low

    @property
    def uses_vulkan(self) -> bool:
        low = {i.lower() for i in self.imports}
        return "vulkan-1.dll" in low

    @property
    def imports_nvngx(self) -> bool:
        low = {i.lower() for i in self.imports}
        return any(i.startswith("nvngx") for i in low)


def _read(fp, offset: int, size: int) -> bytes:
    fp.seek(offset)
    return fp.read(size)


def _rva_to_offset(sections: list[Section], rva: int):
    for s in sections:
        span = max(s.virtual_size, s.raw_size)
        if s.virtual_address <= rva < s.virtual_address + span:
            delta = rva - s.virtual_address
            if delta < s.raw_size:
                return s.raw_pointer + delta
            return None
    return None


def _read_cstr(fp, offset: int, limit: int = 260) -> str:
    fp.seek(offset)
    data = fp.read(limit)
    end = data.find(b"\x00")
    if end >= 0:
        data = data[:end]
    try:
        return data.decode("ascii", "ignore").strip()
    except Exception:
        return ""


_DLL_SUFFIX = (".dll", ".drv", ".ocx", ".exe", ".cpl", ".sys")


def _plausible_module_name(nm: str) -> bool:
    """导入表里的模块名必须是可打印 ASCII，且以常见扩展名结尾。"""
    if not (4 <= len(nm) <= 260):
        return False
    if not all(32 <= ord(c) < 127 for c in nm):
        return False
    return nm.lower().endswith(_DLL_SUFFIX)


def _parse_import_table(
    fp, sections, rva: int, size: int, into: set, stride: int = 20, name_off: int = 12
) -> None:
    """解析导入描述符数组。

    标准导入描述符 = 20 字节（DLL 名 RVA 在偏移 12）；
    延迟导入描述符 = 32 字节（Attributes + DllNameRVA 在偏移 4）。
    两者步长不同，混用会读出垃圾项。
    """
    if not rva:
        return
    off = _rva_to_offset(sections, rva)
    if off is None:
        return
    fp.seek(off)
    raw = fp.read(stride * 64)  # 最多 64 个描述符，足够
    for i in range(0, len(raw) - stride + 1, stride):
        chunk = raw[i : i + stride]
        if len(chunk) < stride:
            break
        if not any(chunk):
            break  # 全零 = 数组结束
        name_rva = struct.unpack_from("<I", chunk, name_off)[0]
        if name_rva == 0:
            continue
        noff = _rva_to_offset(sections, name_rva)
        if noff is None:
            continue
        nm = _read_cstr(fp, noff)
        if _plausible_module_name(nm):
            into.add(nm)


def _byte_scan(fp, size: int, needles) -> tuple[str, ...]:
    """整文件分块扫描 ASCII / UTF-16LE 关键字，用于识别动态加载 d3d12 的游戏。"""
    hits: set[str] = set()
    wide = [n.decode("ascii").lower().encode("utf-16-le") for n in needles]
    limit = min(size, _MAX_SCAN_BYTES)
    overlap = 64
    fp.seek(0)
    pos = 0
    tail = b""
    while pos < limit and len(hits) < len(needles):
        chunk = fp.read(min(_CHUNK, limit - pos))
        if not chunk:
            break
        pos += len(chunk)
        buf = tail + chunk
        low = buf.lower()
        for n in needles:
            if n in low:
                hits.add("d3d12.dll")
        for n, w in zip(needles, wide):
            if w in low:
                hits.add("d3d12.dll")
        tail = buf[-overlap:]
    if hits:
        return ("动态引用 d3d12",)
    return ()


def inspect(path: str | Path) -> PEInfo:
    """解析一个 PE 文件，返回关键信息。任何异常都会被吞掉并记在 error 里。"""
    p = Path(path)
    info = PEInfo(path=str(p))
    try:
        info.size = p.stat().st_size
        with open(p, "rb") as fp:
            if _read(fp, 0, 2) != b"MZ":
                info.error = "不是 PE 文件（缺少 MZ 头）"
                return info
            e_lfanew = struct.unpack("<I", _read(fp, 0x3C, 4))[0]
            if e_lfanew <= 0 or e_lfanew > info.size:
                info.error = "PE 头偏移非法"
                return info
            sig = _read(fp, e_lfanew, 4)
            if sig != b"PE\x00\x00":
                info.error = "缺少 PE 签名"
                return info
            coff = _read(fp, e_lfanew + 4, 20)
            if len(coff) < 20:
                info.error = "COFF 头不完整"
                return info
            machine, nsec = struct.unpack("<HH", coff[:4])
            size_opt = struct.unpack("<H", coff[16:18])[0]
            info.machine = machine
            info.arch = MACHINE_NAMES.get(machine, f"0x{machine:04X}")
            info.is_pe = True

            opt_off = e_lfanew + 24
            magic = struct.unpack("<H", _read(fp, opt_off, 2))[0]
            if magic == 0x20B:
                dd_off = opt_off + 112
                nrv_off = opt_off + 108
            elif magic == 0x10B:
                dd_off = opt_off + 96
                nrv_off = opt_off + 92
            else:
                info.error = f"未知可选头 Magic 0x{magic:04X}"
                return info

            nrv = struct.unpack("<I", _read(fp, nrv_off, 4))[0]
            dirs = []
            if nrv:
                raw = _read(fp, dd_off, 8 * min(nrv, 16))
                for i in range(0, len(raw) - 7, 8):
                    dirs.append(struct.unpack("<II", raw[i : i + 8]))
            while len(dirs) <= DIR_DELAY_IMPORT:
                dirs.append((0, 0))

            sec_off = opt_off + size_opt
            sections: list[Section] = []
            raw_secs = _read(fp, sec_off, 40 * nsec)
            for i in range(nsec):
                s = raw_secs[i * 40 : i * 40 + 40]
                if len(s) < 40:
                    break
                nm = s[:8].rstrip(b"\x00").decode("ascii", "ignore")
                vs, va, rs, rp = struct.unpack("<IIII", s[8:24])
                sections.append(Section(nm, vs, va, rs, rp))

            found: set[str] = set()
            # 标准导入表：20 字节步长，DLL 名 RVA 在 +12
            _parse_import_table(fp, sections, dirs[DIR_IMPORT][0], dirs[DIR_IMPORT][1], found)
            # 延迟导入表：32 字节步长，DLL 名 RVA 在 +4
            # Attributes 位 0 (dlattrRva) 未置位时字段是 VA 而非 RVA（极老的链接器产物），跳过。
            d_rva, d_size = dirs[DIR_DELAY_IMPORT]
            if d_rva:
                attrs_off = _rva_to_offset(sections, d_rva)
                attrs = 0
                if attrs_off is not None:
                    attrs = struct.unpack("<I", _read(fp, attrs_off, 4))[0]
                if attrs & 0x1:
                    _parse_import_table(
                        fp, sections, d_rva, d_size, found, stride=32, name_off=4
                    )
            info.imports = tuple(sorted(found))

            low = {i.lower() for i in found}
            if "d3d12.dll" not in low and "d3d12core.dll" not in low:
                info.char_hits = _byte_scan(fp, info.size, _FALLBACK_NEEDLES)

            info.ok = True
            return info
    except Exception as exc:  # 读不了就当解析失败，绝不抛出
        info.error = f"{type(exc).__name__}: {exc}"
        return info


def file_version_string(path: str | Path) -> dict[str, str]:
    """用 Win32 版本资源读取 ProductName / FileDescription（尽力而为）。"""
    out: dict[str, str] = {}
    try:
        import ctypes
        from ctypes import wintypes

        version = ctypes.WinDLL("version", use_last_error=True)
        GetFileVersionInfoSizeW = version.GetFileVersionInfoSizeW
        GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        GetFileVersionInfoSizeW.restype = wintypes.DWORD

        handle = wintypes.DWORD()
        size = GetFileVersionInfoSizeW(str(path), ctypes.byref(handle))
        if not size:
            return out
        buf = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buf):
            return out

        lptr = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(lptr), ctypes.byref(length)):
            return out
        if length.value < 4:
            return out
        lang, cp = struct.unpack("<HH", ctypes.string_at(lptr.value, 4))

        for key in ("ProductName", "FileDescription", "CompanyName", "FileVersion"):
            sub = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\{key}"
            ptr = ctypes.c_void_p()
            ln = wintypes.UINT()
            if version.VerQueryValueW(buf, sub, ctypes.byref(ptr), ctypes.byref(ln)) and ptr.value:
                out[key] = ctypes.wstring_at(ptr.value).strip("\x00").strip()
    except Exception:
        pass
    return out


__all__ = ["PEInfo", "inspect", "file_version_string", "MACHINE_NAMES"]
