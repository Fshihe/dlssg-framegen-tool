"""显卡与系统环境检测 —— 决定用哪条计算路由。

规则来自上游 README / docs/NATIVE_INI.md：
  - SM86 路由 → RTX 30 系列（Ampere，GA10x）
  - SM75 路由 → RTX 20 系列（Turing，TU10x）
  - RTX 40/50 系列自带 DLSS 帧生成，不需要也不应该用本 Mod
  - GTX 16 系列是 SM75 但没有 Tensor Core，无法做帧生成
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# 判定结果
VERDICT_OK = "ok"            # 可用，给出 Router
VERDICT_NOT_NEEDED = "not_needed"  # 显卡原生支持，无需本 Mod
VERDICT_UNSUPPORTED = "unsupported"  # 架构不支持
VERDICT_NO_NVIDIA = "no_nvidia"      # 没找到 N 卡


@dataclass
class GpuInfo:
    name: str
    driver: str = ""
    vram_mib: int = 0
    sm: str = ""          # SM75 / SM86 / SM89 ...
    router: str = ""      # 传给 INI 的 Router 值
    family: str = ""      # Turing / Ampere / Ada / ...
    verdict: str = VERDICT_UNSUPPORTED
    reason: str = ""
    is_laptop: bool = False


@dataclass
class SystemInfo:
    os_caption: str = ""
    os_build: int = 0
    arch: str = ""
    is_admin: bool = False


@dataclass
class EnvReport:
    gpus: list[GpuInfo] = field(default_factory=list)
    system: SystemInfo = field(default_factory=SystemInfo)
    nvidia_smi: bool = False
    notes: list[str] = field(default_factory=list)
    # 硬件加速 GPU 计划（HAGS）—— DLSS 帧生成的硬性系统前置条件
    hags: bool | None = None
    hags_raw: int | None = None

    @property
    def primary(self) -> GpuInfo | None:
        """挑一张最相关的卡：优先 verdict==ok，其次任意 N 卡。"""
        nv = [g for g in self.gpus if g.name]
        if not nv:
            return None
        ok = [g for g in nv if g.verdict == VERDICT_OK]
        if ok:
            # 显存大的优先（独显 > 核显）
            return max(ok, key=lambda g: g.vram_mib)
        return max(nv, key=lambda g: g.vram_mib)

    @property
    def router(self) -> str:
        g = self.primary
        return g.router if g and g.router else "SM86"


# --------------------------------------------------------------------------
# 显卡型号 → 架构
# --------------------------------------------------------------------------

# RTX 30 系列（Ampere 消费级，全部 SM86）
_SM86_30 = re.compile(r"\bRTX\s*30(50|60|70|80|90)\b", re.I)
# RTX 20 系列（Turing，SM75）
_SM75_20 = re.compile(r"\bRTX\s*20(60|70|80)\b", re.I)
# RTX 40 系列（Ada，SM89 —— 原生支持帧生成）
_SM89_40 = re.compile(r"\bRTX\s*40(60|70|80|90)\b", re.I)
# RTX 50 系列（Blackwell，SM120 —— 原生支持帧生成）
_SM120_50 = re.compile(r"\bRTX\s*50(60|70|80|90)\b", re.I)
# GTX 16 系列（Turing 无 Tensor Core，SM75）
_GTX16 = re.compile(r"\bGTX\s*16(50|60|60\s*SUPER|SUPER)\b", re.I)
# 专业卡
_QUADRO_RTX = re.compile(r"\bQuadro\s+RTX\b|\bRTX\s+A\d{4}\b", re.I)  # Turing/Ampere 专业卡
_TITAN_RTX = re.compile(r"\bTITAN\s+RTX\b", re.I)


def classify(name: str) -> tuple[str, str, str, str]:
    """把显卡名映射成 (sm, router, family, verdict_reason)。

    router 为空表示不给路由（不支持 / 不需要）。
    """
    n = name or ""

    if _SM120_50.search(n):
        return "SM120", "", "Blackwell (RTX 50)", "RTX 50 系列原生支持 DLSS 帧生成，无需本 Mod"
    if _SM89_40.search(n):
        return "SM89", "", "Ada Lovelace (RTX 40)", "RTX 40 系列原生支持 DLSS 帧生成，无需本 Mod"

    if _SM86_30.search(n):
        return "SM86", "SM86", "Ampere (RTX 30)", ""
    if _SM75_20.search(n):
        return "SM75", "SM75", "Turing (RTX 20)", ""

    if _GTX16.search(n):
        return "SM75", "", "Turing (GTX 16)", "GTX 16 系列没有 Tensor Core，无法运行 DLSS 帧生成"

    if _TITAN_RTX.search(n):
        return "SM75", "SM75", "Turing (TITAN RTX)", ""
    if _QUADRO_RTX.search(n):
        # RTX Axxxx = Ampere SM86；Quadro RTX xxxx = Turing SM75
        if re.search(r"\bRTX\s+A\d{4}\b", n, re.I):
            return "SM86", "SM86", "Ampere (RTX A 系列)", ""
        return "SM75", "SM75", "Turing (Quadro RTX)", ""

    if re.search(r"\bRTX\s*PRO\b", n, re.I):
        return "SM120", "", "Blackwell (RTX PRO)", "RTX PRO 原生支持 DLSS 帧生成，无需本 Mod"
    if re.search(r"\bTITAN\s+V\b", n, re.I):
        return "SM70", "", "Volta", "Volta 架构不支持 DLSS 帧生成"
    if re.search(r"\bGTX\s*(9|10)\d0\b", n, re.I):
        return "SM6x", "", "Pascal/Maxwell", "该架构不支持 DLSS 帧生成"

    return "", "", "", "无法识别的显卡型号，本工具无法确定计算路由"


# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 12) -> tuple[int, str]:
    """静默执行命令，不弹黑框。"""
    try:
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=flags,
            errors="ignore",
        )
        return p.returncode, (p.stdout or "")
    except Exception:
        return -1, ""


def _nvidia_smi_paths() -> list[str]:
    cands = ["nvidia-smi"]
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    cands.append(str(Path(sysroot) / "System32" / "nvidia-smi.exe"))
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    cands.append(str(Path(pf) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"))
    return cands


def query_nvidia_smi() -> tuple[list[dict], bool]:
    """优先用 nvidia-smi：能拿到 NVIDIA 口径的驱动版本和显存。"""
    for exe in _nvidia_smi_paths():
        code, out = _run(
            [
                exe,
                "--query-gpu=name,driver_version,memory.total",
                "--format=csv,noheader,nounits",
            ]
        )
        if code == 0 and out.strip():
            rows = []
            for line in out.strip().splitlines():
                parts = [x.strip() for x in line.split(",")]
                if len(parts) >= 3:
                    try:
                        vram = int(float(parts[2]))
                    except ValueError:
                        vram = 0
                    rows.append({"name": parts[0], "driver": parts[1], "vram": vram})
            if rows:
                return rows, True
    return [], False


def _wmi_driver_to_nvidia(wmi_ver: str) -> str:
    """WMI 的 32.0.16.1088 → NVIDIA 口径 610.88。拿不到就原样返回。"""
    m = re.match(r"^\d+\.\d+\.(\d+)\.(\d+)$", (wmi_ver or "").strip())
    if not m:
        return wmi_ver or ""
    third, fourth = int(m.group(1)), int(m.group(2))
    if third < 10:
        return wmi_ver
    return f"{(third - 10) * 10000 + fourth:.0f}".rjust(5, "0")[:3] + "." + f"{(third - 10) * 10000 + fourth:.0f}"[3:]


def query_wmi_gpus() -> list[dict]:
    """nvidia-smi 不可用时的兜底：PowerShell + CIM。"""
    ps = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,DriverVersion,AdapterRAM | ConvertTo-Json -Compress"
    )
    code, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=30)
    if code != 0 or not out.strip():
        code, out = _run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + ps],
            timeout=30,
        )
    if not out.strip():
        return []
    import json

    try:
        data = json.loads(out.strip())
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    rows = []
    for d in data:
        nm = str(d.get("Name") or "").strip()
        if not nm:
            continue
        vram = d.get("AdapterRAM") or 0
        try:
            vram_mib = int(vram) // (1024 * 1024)
        except Exception:
            vram_mib = 0
        rows.append(
            {
                "name": nm,
                "driver": _wmi_driver_to_nvidia(str(d.get("DriverVersion") or "")),
                "vram": vram_mib,
            }
        )
    return rows


def query_system() -> SystemInfo:
    si = SystemInfo(arch=os.environ.get("PROCESSOR_ARCHITECTURE", ""))
    try:
        si.is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        si.is_admin = False
    try:
        import platform

        si.os_caption = f"{platform.system()} {platform.release()} ({platform.version()})"
        build = platform.version().split(".")[-1]
        si.os_build = int(build) if build.isdigit() else 0
    except Exception:
        pass
    return si


def detect(include_generic: bool = True) -> EnvReport:
    """完整环境检测。"""
    rep = EnvReport(system=query_system())

    rows, used_smi = query_nvidia_smi()
    rep.nvidia_smi = used_smi
    if not rows:
        rows = query_wmi_gpus()
        if rows:
            rep.notes.append("nvidia-smi 不可用，驱动版本由 WMI 换算，可能与 NVIDIA 口径略有差异")

    if include_generic and not rows:
        rep.notes.append("未检测到任何显示适配器信息")

    for r in rows:
        name = r["name"]
        sm, router, family, why = classify(name)
        g = GpuInfo(
            name=name,
            driver=r.get("driver", ""),
            vram_mib=int(r.get("vram", 0) or 0),
            sm=sm,
            router=router,
            family=family,
            is_laptop=bool(re.search(r"laptop|notebook|mobile", name, re.I)),
        )
        if router:
            g.verdict = VERDICT_OK
            g.reason = f"{family}，使用 Router={router}, KernelImage=PTX"
        elif sm in ("SM89", "SM120"):
            g.verdict = VERDICT_NOT_NEEDED
            g.reason = why
        elif not re.search(r"nvidia|geforce|rtx|gtx|quadro|titan", name, re.I):
            g.verdict = VERDICT_NO_NVIDIA
            g.reason = "非 NVIDIA 显卡，不支持 DLSS 帧生成"
        else:
            g.verdict = VERDICT_UNSUPPORTED
            g.reason = why or "该显卡不在本 Mod 支持范围内"
        rep.gpus.append(g)

    nv = [g for g in rep.gpus if re.search(r"nvidia|geforce|rtx|gtx|quadro|titan", g.name, re.I)]
    if not nv:
        rep.notes.append("未检测到 NVIDIA 显卡：本 Mod 依赖 NVIDIA 驱动提供的 NGX/NVAPI/CUDA 接口")
    if rep.system.os_build and rep.system.os_build < 22000:
        rep.notes.append("Windows 版本较旧（非 Win11 内核），上游要求 Windows 10/11 x64")

    # 硬件加速 GPU 计划：帧生成的硬性前置条件，缺了它游戏会直接说"显卡不支持"
    from . import winenv

    rep.hags, rep.hags_raw = winenv.read_hags()
    if rep.hags is False:
        rep.notes.append(
            "硬件加速 GPU 计划（HAGS）已关闭 —— DLSS 帧生成硬性依赖它，"
            "不开启的话游戏会提示「您的显卡不支持 DLSS 帧生成技术」"
        )
    return rep


__all__ = [
    "GpuInfo",
    "SystemInfo",
    "EnvReport",
    "detect",
    "classify",
    "VERDICT_OK",
    "VERDICT_NOT_NEEDED",
    "VERDICT_UNSUPPORTED",
    "VERDICT_NO_NVIDIA",
]
