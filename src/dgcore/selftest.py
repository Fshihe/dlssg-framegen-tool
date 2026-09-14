"""内置自检 —— 在临时目录里完整跑一遍安装/校验/卸载/回滚。

给用户一个可以自己按的按钮：证明这个工具只会改游戏目录里那两个文件，
并且一定会备份、一定会还原、失败一定会回滚。

同时也是本项目的自动化回归测试。
运行： python -m dgcore.selftest   （或 exe --selftest）
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import INI_NAME
from . import anticheat as ac
from . import capability
from . import games, gpu, installer, pe, proc
from . import gfxapi
from . import profiles
from .installer import MANIFEST

# 外来 Mod 的占位内容（用来验证"备份-还原"链路）
FOREIGN_DLL = b"FOREIGN-MOD-CONTENT-" + bytes(range(256)) * 64


class Runner:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.rows: list[tuple[str, str, str]] = []

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        if cond:
            self.passed += 1
            self.rows.append(("PASS", name, detail))
        else:
            self.failed += 1
            self.rows.append(("FAIL", name, detail))
        return bool(cond)

    def eq(self, name: str, got, want, detail: str = "") -> bool:
        return self.check(name, got == want, detail or f"got={got!r} want={want!r}")


def _write_tmp(base: Path, name: str, text: str) -> Path:
    p = base / name
    p.write_text(text, encoding="utf-8")
    return p


def _make_fake_game(root: Path, exe_name: str = "FakeGame-Win64-Shipping.exe", foreign: bool = True):
    """造一个假的游戏目录。"""
    exe_dir = root / "Game" / "Binaries" / "Win64"
    exe_dir.mkdir(parents=True, exist_ok=True)
    (exe_dir / exe_name).write_bytes(synth_pe())
    if foreign:
        (exe_dir / "version.dll").write_bytes(FOREIGN_DLL)
    return exe_dir


# --------------------------------------------------------------------------
# 合成 PE —— 自检**不能**依赖任何系统文件
#
# 早期版本会去复制 System32\notepad.exe 当测试素材，结果在 Windows 11 上
# 直接报「系统找不到指定文件」：Win11 已经把 notepad 移出 System32 改成
# Store 应用了。一个用来建立信任的自检功能自己报错，比没有还糟。
#
# 所以改成自己拼一个语法合法的 64 位 PE：只含一个 .text 段和一个导入表，
# 内容完全可控、任何 Windows 上行为一致。
# --------------------------------------------------------------------------

def synth_pe(imports: tuple[str, ...] = ("KERNEL32.dll", "d3d12.dll", "dxgi.dll")) -> bytes:
    """生成一个最小但结构合法的 x64 PE 文件。

    只做到 PE 解析器需要的程度：DOS 头、COFF 头、可选头、一个 .text 段、
    以及一个真实的导入表（这样导入表解析路径能被真正测到）。
    """
    import struct

    SECT_RVA = 0x1000
    SECT_RAW = 0x400
    OPT_SIZE = 240  # PE32+ 可选头大小

    # ---- 构造 .text 段内容 ----
    # 布局： [导入描述符 20*N + 全零] [DLL 名字符串] [INT/IAT 表] [函数名]
    n = len(imports)
    desc_size = 20 * (n + 1)

    name_off = desc_size
    name_offsets: dict[str, int] = {}
    blob = bytearray(b"\x00" * name_off)
    for nm in imports:
        name_offsets[nm] = len(blob)
        blob += nm.encode("ascii") + b"\x00"
    # 对齐到偶数
    if len(blob) % 2:
        blob += b"\x00"

    # 每个 DLL 一组 INT(8 字节/项, 2 项含结尾 0) + IAT
    tables_off = len(blob)
    int_offsets: dict[str, int] = {}
    fname_off = tables_off + 16 * n
    func_names: list[tuple[str, int]] = []
    cur = fname_off
    for nm in imports:
        int_offsets[nm] = len(blob)
        blob += b"\x00" * 16  # INT (2 项)
        blob += b"\x00" * 16  # IAT (2 项)
        # 一个假的函数名
        fn = ("Fake_" + nm.split(".")[0])[:24]
        func_names.append((nm, cur))
        blob += fn.encode("ascii") + b"\x00"
        cur = len(blob) + tables_off - tables_off  # 保持相对
        cur = len(blob)
        if cur % 2:
            blob += b"\x00"
            cur = len(blob)

    # 填导入描述符 + INT
    for i, nm in enumerate(imports):
        d_off = i * 20
        struct.pack_into(
            "<IIIII",
            blob, d_off,
            SECT_RVA + int_offsets[nm],   # OriginalFirstThunk (INT RVA)
            0, 0,
            SECT_RVA + name_offsets[nm],  # Name RVA
            SECT_RVA + int_offsets[nm] + 16,  # FirstThunk (IAT RVA)
        )
        # INT[0] 指向函数名
        fn_rva = SECT_RVA + func_names[i][1]
        struct.pack_into("<Q", blob, int_offsets[nm], fn_rva)
        struct.pack_into("<Q", blob, int_offsets[nm] + 16, fn_rva)

    # 补齐到段大小
    while len(blob) < 0x200:
        blob += b"\x00"

    # ---- DOS 头 ----
    dos = bytearray(b"\x00" * 0x40)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x40)  # e_lfanew

    # ---- PE 签名 + COFF ----
    pe_sig = b"PE\x00\x00"
    coff = struct.pack(
        "<HHIIIHH",
        0x8664,          # Machine = AMD64
        1,               # NumberOfSections
        0, 0, 0,
        OPT_SIZE,        # SizeOfOptionalHeader
        0x0022,          # Characteristics: EXECUTABLE | LARGE_ADDRESS_AWARE
    )

    # ---- 可选头（PE32+）----
    opt = bytearray(OPT_SIZE)
    struct.pack_into("<H", opt, 0, 0x20B)      # Magic = PE32+
    struct.pack_into("<I", opt, 16, SECT_RAW)  # AddressOfEntryPoint
    struct.pack_into("<I", opt, 20, SECT_RVA)  # BaseOfCode
    struct.pack_into("<Q", opt, 24, 0x140000000)  # ImageBase
    struct.pack_into("<I", opt, 32, SECT_RAW)  # SectionAlignment
    struct.pack_into("<I", opt, 36, 0x200)     # FileAlignment
    struct.pack_into("<H", opt, 40, 6)         # MajorOSVersion
    struct.pack_into("<I", opt, 56, len(blob) + SECT_RAW)  # SizeOfImage
    struct.pack_into("<I", opt, 60, SECT_RAW)  # SizeOfHeaders
    struct.pack_into("<H", opt, 68, 3)         # Subsystem = CONSOLE
    struct.pack_into("<I", opt, 108, 16)       # NumberOfRvaAndSizes
    # DataDirectory[1] = Import Table（偏移 112 + 1*8）
    struct.pack_into("<II", opt, 112 + 8, SECT_RVA, desc_size)

    # ---- 段表 ----
    name = b".text\x00\x00\x00"
    sec = name + struct.pack(
        "<IIII", len(blob), SECT_RVA, len(blob) + 0x200, SECT_RAW
    ) + struct.pack("<I", 0x60000020)  # CODE | EXECUTE | READ

    body = bytes(blob)
    pad = (0x200 - (len(body) % 0x200)) % 0x200

    out = bytearray()
    out += dos
    out += pe_sig + coff + bytes(opt) + sec
    # 补齐到 SECT_RAW
    if len(out) < SECT_RAW:
        out += b"\x00" * (SECT_RAW - len(out))
    out += body + b"\x00" * pad
    return bytes(out)


def run(verbose: bool = True) -> Runner:
    r = Runner()
    tmp = Path(tempfile.mkdtemp(prefix="dlssg-selftest-"))
    saved_state = None
    state_p = installer.state_file()
    try:
        # 备份用户真实状态，测试完恢复 —— 自检绝不能污染真实记录
        if state_p.is_file():
            saved_state = state_p.read_bytes()

        # ------------------------------------------------------------------
        # 1. payload 完整性
        # ------------------------------------------------------------------
        avail = installer.available_payloads()
        _n_default = len(profiles.PROFILES[profiles.DEFAULT_VERSION].proxy_names)
        r.check(
            f"payload：默认 profile（{profiles.DEFAULT_VERSION}）的 {_n_default} 个代理 DLL 全部就绪",
            all(avail.values()) and len(avail) == _n_default,
            ", ".join(f"{k}={'OK' if v else 'BAD'}" for k, v in avail.items()),
        )
        r.eq("payload：默认 profile 清单条目数", len(MANIFEST), _n_default)
        v = installer.resolve_payload("version.dll")
        r.check(
            f"payload：{profiles.DEFAULT_VERSION} 的 version.dll 与基线一致",
            v.sha256 == profiles.PROFILES[profiles.DEFAULT_VERSION].manifest["version.dll"][0],
            f"sha256={v.sha256[:24]}…",
        )
        # 0.2.4 的 version.dll 是上游 README 公布过哈希的那个版本，单独核对
        v24 = installer.resolve_payload("version.dll", version="0.2.4")
        r.check(
            "payload：0.2.4 的 version.dll 与上游公布哈希一致（c844646d…）",
            v24.sha256.startswith("c844646d"),
            f"sha256={v24.sha256[:24]}…",
        )
        bad = installer.PayloadFile("x.dll", Path("nope"), "", 0, False, "t")
        r.check("payload：缺失文件被判为不可用", not bad.ok)

        # ------------------------------------------------------------------
        # 2. INI 生成
        # ------------------------------------------------------------------
        # 注意：SM86 现在走 0.3.0（无 Router 键），SM75 走 0.2.4（有 Router 键）
        i86 = installer.build_ini("SM86", 0, 3, 1)
        r.check("INI：SM86 走 0.3.0 结构（含 [Runtime]、无 Router）",
                "[Runtime]" in i86 and "Router=" not in i86, i86[:200])
        i86_24 = installer.build_ini("SM86", 0, 3, 1, version="0.2.4")
        r.check("INI：SM86 强制用 0.2.4 时含 Router=SM86",
                "Router=SM86" in i86_24 and "KernelImage=PTX" in i86_24)
        i75 = installer.build_ini("SM75", 1, 3, 1)
        r.check("INI：SM75 走 0.2.4 且强制关闭近似采样",
                "Router=SM75" in i75 and "HardwareBilinear=0" in i75)
        perf = installer.build_ini("SM86", 1, 3, 1, version="0.2.4")
        r.check("INI：0.2.4 性能档写入 HardwareBilinear=1", "HardwareBilinear=1" in perf)
        clamp = installer.build_ini("SM86", 0, 99, 99)
        r.check("INI：倍率与日志级别被夹到合法区间",
                f"MaxGeneratedFrames={profiles.PROFILE_030.max_frames}" in clamp and "Level=3" in clamp,
                clamp[:200])
        r.check("INI：非法路由回落到 SM86",
                "Router=SM86" in installer.build_ini("XX99", version="0.2.4"))

        # ------------------------------------------------------------------
        # 2b. SM75（RTX 20 系列）路径 —— 与 SM86 同等对待
        # ------------------------------------------------------------------
        r.eq("SM75：RTX 2070 判为 SM75", gpu.classify("NVIDIA GeForce RTX 2070")[1], "SM75")
        r.eq("SM75：2070 笔记本版判为 SM75", gpu.classify("NVIDIA GeForce RTX 2070 Laptop GPU")[1], "SM75")
        r.eq("SM75：2060 SUPER 判为 SM75", gpu.classify("NVIDIA GeForce RTX 2060 SUPER")[1], "SM75")
        r.eq("SM75：TITAN RTX 判为 SM75", gpu.classify("NVIDIA TITAN RTX")[1], "SM75")
        r.eq("SM75：Quadro RTX 5000 判为 SM75", gpu.classify("Quadro RTX 5000")[1], "SM75")
        i75b = installer.build_ini("SM75", 1, 3, 1)
        r.check("SM75：KernelImage 必须是 PTX（不能用 Cubin）", "KernelImage=PTX" in i75b)
        r.check("SM75：HardwareBilinear 被强制归零", "HardwareBilinear=1" not in i75b)
        r.check("SM75：注释写明对应 RTX 20 系列", "RTX 20 系列" in i75b)

        # ------------------------------------------------------------------
        # 2c. 倍率上限（评论区明确要求的功能）
        # ------------------------------------------------------------------
        r.eq("倍率：4X 选项映射到 MaxGeneratedFrames=3",
             installer.frame_option_to_value("4X（上限，推荐）"), 3)
        r.eq("倍率：3X 选项映射到 2", installer.frame_option_to_value("3X"), 2)
        r.eq("倍率：2X 选项映射到 1", installer.frame_option_to_value("2X"), 1)
        r.eq("倍率：未知文本回落到 3", installer.frame_option_to_value("乱写的"), 3)
        ini2x = installer.build_ini("SM86", 0, 1, 1)
        r.check("倍率：2X 的 INI 带 MaxGeneratedFrames=1", "MaxGeneratedFrames=1" in ini2x)
        ini3x = installer.build_ini("SM86", 0, 2, 1)
        r.check("倍率：3X 的 INI 带 MaxGeneratedFrames=2", "MaxGeneratedFrames=2" in ini3x)

        # ------------------------------------------------------------------
        # 2d. 扫描档位
        # ------------------------------------------------------------------
        r.check("扫描档位：三档都存在",
                set(games.SCAN_PRESETS) == {"快速", "标准", "彻底"},
                f"{list(games.SCAN_PRESETS)}")
        r.check("扫描档位：彻底 > 标准 > 快速（解析上限）",
                games.SCAN_PRESETS["彻底"]["max_exes"]
                > games.SCAN_PRESETS["标准"]["max_exes"]
                > games.SCAN_PRESETS["快速"]["max_exes"],
                f"{[v['max_exes'] for v in games.SCAN_PRESETS.values()]}")

        # ------------------------------------------------------------------
        # 2e. 游戏能力预判
        # ------------------------------------------------------------------
        _capdir = tmp / "cap_test"
        _capdir.mkdir(parents=True, exist_ok=True)
        (_capdir / "nvngx_dlssg.dll").write_bytes(b"x")
        cp1 = capability.predict("g.exe", _capdir, "confirmed", True)
        r.check("预判：有帧生成组件 → 大概率可以",
                cp1.level == capability.Support.GOOD, cp1.level.value)
        (_capdir / "nvngx_dlssg.dll").unlink()
        (_capdir / "nvngx_dlss.dll").write_bytes(b"x")
        cp2 = capability.predict("g.exe", _capdir, "confirmed", True)
        r.check("预判：只有超分组件 → 不太可能",
                cp2.level == capability.Support.UNLIKELY, cp2.level.value)
        cp3 = capability.predict("g.exe", _capdir, "vulkan", False)
        r.check("预判：Vulkan → 不支持且不允许装",
                cp3.level == capability.Support.NO and not cp3.installable, cp3.level.value)
        cp4 = capability.predict("Palworld-Win64-Shipping.exe", _capdir, "confirmed", True, "Palworld")
        r.check("预判：已知游戏被识别", cp4.level == capability.Support.UNLIKELY, cp4.level.value)

        # ------------------------------------------------------------------
        # 2f. 双 profile 适配层（0.2.4 / 0.3.0）
        # ------------------------------------------------------------------
        r.check("profile：两个版本都已定义", set(profiles.PROFILES) == {"0.2.4", "0.3.0"},
                f"{list(profiles.PROFILES)}")
        r.check("profile：0.2.4 支持 SM75", profiles.PROFILE_024.supports_sm75)
        r.check("profile：0.3.0 **不**支持 SM75（上游已移除内核）",
                not profiles.PROFILE_030.supports_sm75)
        r.eq("profile：SM86 自动选 0.3.0", profiles.for_router("SM86").version, "0.3.0")
        r.eq("profile：SM75 自动选 0.2.4", profiles.for_router("SM75").version, "0.2.4")
        r.check("profile：0.3.0 上限 6X", profiles.PROFILE_030.max_multiplier == 6)
        r.check("profile：0.2.4 上限 4X", profiles.PROFILE_024.max_multiplier == 4)

        # INI 结构差异
        i030 = installer.build_ini("SM86", 0, 5, 1)
        r.check("profile：0.3.0 的 INI 含 [Runtime] 段", "[Runtime]" in i030)
        r.check("profile：0.3.0 的 INI 含 Optimized", "Optimized=" in i030)
        r.check("profile：0.3.0 的 INI 不含 Router（该键已删除）", "Router=" not in i030)
        r.check("profile：0.3.0 支持 6X（MaxGeneratedFrames=5）",
                "MaxGeneratedFrames=5" in i030)
        i024 = installer.build_ini("SM75", 0, 3, 1)
        r.check("profile：0.2.4 的 INI 含 Router", "Router=" in i024)
        r.check("profile：0.2.4 的 INI 不含 [Runtime]", "[Runtime]" not in i024)
        r.eq("profile：版本识别（0.3.0）",
             profiles.detect_ini_version(_write_tmp(tmp, "a.ini", i030)), "0.3.0")
        r.eq("profile：版本识别（0.2.4）",
             profiles.detect_ini_version(_write_tmp(tmp, "b.ini", i024)), "0.2.4")

        # 入口差异
        r.check("profile：0.3.0 没有 winhttp（上游已移除）",
                "winhttp.dll" not in profiles.PROFILE_030.proxy_names,
                f"{profiles.PROFILE_030.proxy_names}")
        r.check("profile：0.3.0 新增 dbghelp 和 d3d12",
                "dbghelp.dll" in profiles.PROFILE_030.proxy_names
                and "d3d12.dll" in profiles.PROFILE_030.proxy_names)
        r.check("profile：0.2.4 有 winhttp",
                "winhttp.dll" in profiles.PROFILE_024.proxy_names)
        r.check("profile：两个 profile 的 version.dll 哈希不同（确实是不同构建）",
                profiles.PROFILE_024.manifest["version.dll"][0]
                != profiles.PROFILE_030.manifest["version.dll"][0])

        # 两个 profile 的 payload 都要齐全可用
        for _v in profiles.all_versions():
            _ok, _why = installer.profile_available(_v)
            r.check(f"profile：{_v} 的 payload 齐全", _ok, _why)

        # 关键回归：SM75 的完整链路必须走 0.2.4 并成功
        # （重构时踩过：execute_plan 用错基线导致 SM75 装不上）
        gdirP = tmp / "GameP"
        edP = _make_fake_game(gdirP, foreign=False)
        planP = installer.make_plan(
            edP, edP / "FakeGame-Win64-Shipping.exe", "version.dll", "SM75", "GameP"
        )
        r.eq("profile：SM75 的 plan 选中 0.2.4", planP.version, "0.2.4")
        resP = installer.execute_plan(planP)
        r.check("profile：SM75 能装上（基线按 profile 取）", resP.success, resP.message)
        r.eq("profile：SM75 落盘的 INI 是 0.2.4 结构",
             profiles.detect_ini_version(edP / INI_NAME), "0.2.4")
        installer.uninstall(edP)

        # ------------------------------------------------------------------
        # 3. 环境检测（真机）
        # ------------------------------------------------------------------
        env = gpu.detect()
        r.check("环境：能读到显卡列表", len(env.gpus) > 0, f"{len(env.gpus)} 个适配器")
        if env.primary:
            r.check(
                "环境：主显卡结论自洽",
                env.primary.verdict in (gpu.VERDICT_OK, gpu.VERDICT_NOT_NEEDED, gpu.VERDICT_UNSUPPORTED)
                and (env.router in ("SM86", "SM75")),
                f"{env.primary.name} → {env.primary.family} / Router={env.router}",
            )
        r.eq("环境：RTX 3070 判为 SM86", gpu.classify("NVIDIA GeForce RTX 3070")[1], "SM86")
        r.eq("环境：RTX 2080 Ti 判为 SM75", gpu.classify("NVIDIA GeForce RTX 2080 Ti")[1], "SM75")
        r.eq("环境：RTX 4060 不给路由", gpu.classify("NVIDIA GeForce RTX 4060")[1], "")
        r.eq("环境：GTX 1660 不给路由", gpu.classify("NVIDIA GeForce GTX 1660 SUPER")[1], "")

        # ------------------------------------------------------------------
        # 4. PE 解析（用自带的合成 PE，不依赖任何系统文件）
        # ------------------------------------------------------------------
        synth = tmp / "synth_x64.exe"
        synth.write_bytes(synth_pe(("KERNEL32.dll", "USER32.dll", "d3d12.dll", "dxgi.dll")))
        info = pe.inspect(synth)
        r.check("PE：能解析合成的 64 位 PE", info.ok and info.is_x64, f"arch={info.arch} err={info.error}")
        r.check(
            "PE：导入表里全是合法 DLL 名",
            all(x.lower().endswith((".dll", ".drv", ".ocx", ".exe", ".cpl", ".sys")) for x in info.imports),
            f"{len(info.imports)} 项: {info.imports}",
        )
        r.check(
            "PE：能正确读出 d3d12 导入",
            "d3d12.dll" in {i.lower() for i in info.imports},
            f"imports={info.imports}",
        )
        r.check("PE：能正确判定为 D3D12 游戏", info.uses_d3d12)

        synth_dx11 = tmp / "synth_dx11.exe"
        synth_dx11.write_bytes(synth_pe(("KERNEL32.dll", "d3d11.dll")))
        info11 = pe.inspect(synth_dx11)
        v11 = gfxapi.judge(
            info11.static_imports, info11.delayed_imports, info11.text_hits,
            synth_dx11.parent, info11.is_x64,
        )
        r.check(
            "PE：仅 D3D11 的程序被判为不支持（不再用布尔值）",
            v11.level == gfxapi.ApiLevel.D3D11_ONLY and not v11.can_install,
            f"level={v11.level.value} evidence={v11.evidence} counter={v11.counter}",
        )
        r.check(
            "PE：判定给出了可读依据",
            len(v11.detail_lines()) >= 2,
            f"{v11.detail_lines()}",
        )
        junk = tmp / "junk.exe"
        junk.write_bytes(b"not a pe")
        r.check("PE：非 PE 文件被安全拒绝", not pe.inspect(junk).ok)
        r.check("PE：不存在的文件不抛异常", not pe.inspect(tmp / "no-such.exe").ok)

        # ---- 分级判定的核心场景（这是「明明 DX12 却提示 DX11」的修复点）----
        both = tmp / "synth_both.exe"
        both.write_bytes(synth_pe(("KERNEL32.dll", "d3d11.dll", "d3d12.dll", "dxgi.dll")))
        ib = pe.inspect(both)
        vb = gfxapi.judge(ib.static_imports, ib.delayed_imports, ib.text_hits, both.parent, ib.is_x64)
        r.check(
            "分级：同时导入 d3d11 + d3d12 仍判为可安装（UE 游戏常态）",
            vb.can_install and vb.level == gfxapi.ApiLevel.CONFIRMED,
            f"level={vb.level.value}",
        )
        r.check(
            "分级：会说明「同时导入属正常」",
            any("同时导入" in e for e in vb.evidence),
            f"{vb.evidence}",
        )
        vul = tmp / "synth_vulkan.exe"
        vul.write_bytes(synth_pe(("KERNEL32.dll", "vulkan-1.dll", "dxgi.dll")))
        iv = pe.inspect(vul)
        vv = gfxapi.judge(iv.static_imports, iv.delayed_imports, iv.text_hits, vul.parent, iv.is_x64)
        r.check(
            "分级：纯 Vulkan 游戏被判为不支持",
            vv.level == gfxapi.ApiLevel.VULKAN and not vv.can_install,
            f"level={vv.level.value}",
        )

        # ------------------------------------------------------------------
        # 5. 安装 / 校验 / 卸载 全链路（含外来文件备份还原）
        # ------------------------------------------------------------------
        gdir = tmp / "GameA"
        exe_dir = _make_fake_game(gdir)
        fake_exe = exe_dir / "FakeGame-Win64-Shipping.exe"

        pf = installer.preflight(
            exe_dir, fake_exe, "version.dll", "SM86", env=env, game_name="GameA"
        )
        r.check("预检：有写权限的目录通过", pf.ok, "; ".join(c.title for c in pf.errors))
        r.check(
            "预检：识别出目标文件名被外来 Mod 占用",
            any("已被占用" in c.title for c in pf.warnings),
        )

        plan = installer.make_plan(exe_dir, fake_exe, "version.dll", "SM86", "GameA")
        r.eq("计划：恰好 2 个写入项", len(plan.writes), 2)

        # dry run 不能改动任何东西
        before = sorted(p.name for p in exe_dir.iterdir())
        installer.execute_plan(plan, dry_run=True)
        r.check(
            "预演：dry-run 不产生任何改动",
            sorted(p.name for p in exe_dir.iterdir()) == before,
            f"{before}",
        )

        res = installer.execute_plan(plan, dry_run=False)
        r.check("安装：执行成功", res.success, res.message)
        r.check("安装：DLL 已就位", (exe_dir / "version.dll").is_file())
        r.check("安装：INI 已就位", (exe_dir / INI_NAME).is_file())
        r.eq(
            "安装：DLL 哈希与 payload 一致",
            installer.sha256_file(exe_dir / "version.dll"),
            profiles.PROFILES[profiles.DEFAULT_VERSION].manifest["version.dll"][0],
        )
        after_install = sorted(p.name for p in exe_dir.iterdir())
        r.check(
            "安装：游戏目录里只多了预期的 1 个文件（DLL 是覆盖同名文件）",
            after_install == sorted(before + [INI_NAME]),
            f"实际：{after_install}，期望：{sorted(before + [INI_NAME])}",
        )
        r.check(
            "安装：外来 version.dll 已被备份",
            res.backup_dir is not None and (res.backup_dir / "version.dll").is_file(),
            f"backup={res.backup_dir}",
        )
        if res.backup_dir:
            r.eq(
                "安装：备份内容与原始外来文件逐字节一致",
                (res.backup_dir / "version.dll").read_bytes(),
                FOREIGN_DLL,
            )

        vr = installer.verify(exe_dir)
        r.check("校验：识别为本工具安装且健康", vr.installed and vr.healthy, "; ".join(c.title for c in vr.details))

        # 幂等重装
        plan2 = installer.make_plan(exe_dir, fake_exe, "version.dll", "SM86", "GameA")
        r.check(
            "幂等：重复安装被识别为无需写入",
            all(i.action == installer.ACT_SKIP for i in plan2.items),
            "; ".join(f"{i.name}:{i.action}" for i in plan2.items),
        )
        installer.execute_plan(plan2)

        # ------------------------------------------------------------------
        # 6. 篡改检测
        # ------------------------------------------------------------------
        (exe_dir / "version.dll").write_bytes(b"tampered")
        vr2 = installer.verify(exe_dir)
        r.check("篡改：体检能发现哈希不符", not vr2.healthy)

        # ------------------------------------------------------------------
        # 7. 卸载 + 还原（DLL 被改过，但有原始备份，应当还原成外来原件）
        # ------------------------------------------------------------------
        ur = installer.uninstall(exe_dir)
        r.check("卸载：执行成功", ur.success, ur.message)
        r.check("卸载：本工具的文件已删除", not (exe_dir / INI_NAME).exists())
        r.check(
            "卸载：外来 version.dll 被完整还原",
            (exe_dir / "version.dll").read_bytes() == FOREIGN_DLL,
            f"restored={ur.restored}",
        )
        r.check(
            "卸载：目录回到安装前的样子（无 version__1.dll 之类残留）",
            sorted(p.name for p in exe_dir.iterdir()) == before,
            f"{sorted(p.name for p in exe_dir.iterdir())}",
        )
        r.check("卸载：安装记录已清除", installer.find_install(exe_dir) is None)

        # ------------------------------------------------------------------
        # 7b. 用户改过"本工具新建的文件"时，卸载不误删
        # ------------------------------------------------------------------
        gdirE = tmp / "GameE"
        edE = _make_fake_game(gdirE, foreign=False)
        installer.execute_plan(
            installer.make_plan(edE, edE / "FakeGame-Win64-Shipping.exe", "version.dll", "SM86", "GameE")
        )
        (edE / INI_NAME).write_text("[Compatibility]\nRouter=SM75\n", "utf-8")
        urE = installer.uninstall(edE)
        r.check(
            "误删防护：用户手改过的 INI 不会被删掉",
            (edE / INI_NAME).is_file() and "已被改动" in urE.message,
            urE.message,
        )
        r.check("误删防护：未被改动的 DLL 正常删除", not (edE / "version.dll").exists())

        # ------------------------------------------------------------------
        # 8. 代理入口自动选择
        # ------------------------------------------------------------------
        gdir2 = tmp / "GameB"
        ed2 = _make_fake_game(gdir2, foreign=False)
        (ed2 / "version.dll").write_bytes(b"FOREIGN")
        pick, why = installer.auto_pick_proxy(ed2)
        r.eq("入口：version.dll 被占用时改用备用入口", pick, "winmm.dll")
        gdir3 = tmp / "GameC"
        ed3 = _make_fake_game(gdir3, foreign=False)
        r.eq("入口：空闲时优先用 version.dll", installer.auto_pick_proxy(ed3)[0], "version.dll")

        # ------------------------------------------------------------------
        # 9. 失败回滚
        # ------------------------------------------------------------------
        gdir4 = tmp / "GameD"
        ed4 = _make_fake_game(gdir4, foreign=True)
        snap_before = {p.name: p.read_bytes() for p in ed4.iterdir()}
        plan4 = installer.make_plan(ed4, ed4 / "FakeGame-Win64-Shipping.exe", "version.dll", "SM86", "GameD")
        # 把计划里的 payload 源换成被篡改的副本：写入前的完整性复核必须拦下并整体回滚
        tampered = tmp / "tampered_version.dll"
        tampered.write_bytes(b"tampered payload content")
        swapped = 0
        for item in plan4.items:
            if item.action == installer.ACT_COPY and item.name == "version.dll":
                item.src = tampered
                swapped += 1
        r.eq("回滚：测试已把 payload 源替换为损坏文件", swapped, 1)
        bad = installer.execute_plan(plan4)
        r.check("回滚：payload 损坏时安装失败", not bad.success, bad.message)
        snap_after = {p.name: p.read_bytes() for p in ed4.iterdir()}
        r.check(
            "回滚：失败后目录逐字节回到原状",
            snap_before == snap_after,
            f"before={sorted(snap_before)} after={sorted(snap_after)}",
        )
        r.check("回滚：失败后不留安装记录", installer.find_install(ed4) is None)

        # ------------------------------------------------------------------
        # 10. 安全护栏
        # ------------------------------------------------------------------
        pf_run = installer.preflight(
            ed4, ed4 / "FakeGame-Win64-Shipping.exe",
            "version.dll", "SM86", env=env, running_names=["explorer.exe"],
        )
        r.check("护栏：游戏运行中会被拦下", any("正在运行" in c.title for c in pf_run.errors))

        pf_ac = installer.preflight(
            ed4, ed4 / "FakeGame-Win64-Shipping.exe", "version.dll", "SM86",
            env=env, anticheat=ac.AntiCheatReport(found=["Easy Anti-Cheat"], running=[]),
        )
        r.check("护栏：检测到反作弊会拦下", any("反作弊" in c.title for c in pf_ac.errors))

        pf_rd = installer.preflight(
            ed4, ed4 / "FakeGame-Win64-Shipping.exe", "version.dll", "SM99", env=env
        )
        r.check("护栏：路由非法会拦下", any("路由" in c.title for c in pf_rd.errors))

        pf_missing = installer.preflight(
            tmp / "no-such-dir", None, "version.dll", "SM86", env=env
        )
        r.check("护栏：目标目录不存在会拦下", not pf_missing.ok)

        # 40 系显卡必须被拒绝
        class _G:
            name = "NVIDIA GeForce RTX 4070"
            family = "Ada"
            driver = "1.0"
            verdict = gpu.VERDICT_NOT_NEEDED
            reason = "原生支持"

        class _E:
            primary = _G()

        pf_40 = installer.preflight(
            ed4, ed4 / "FakeGame-Win64-Shipping.exe", "version.dll", "SM86", env=_E()
        )
        r.check("护栏：40 系显卡会给出警告", any("原生支持" in c.title for c in pf_40.warnings))

        # ------------------------------------------------------------------
        # 11. 幂等/清理：残留临时文件
        # ------------------------------------------------------------------
        (ed4 / f"version.dll{installer.TMP_SUFFIX}").write_bytes(b"leftover")
        installer.clean_stale_temps(ed4)
        r.check("清理：残留临时文件被清掉", not (ed4 / f"version.dll{installer.TMP_SUFFIX}").exists())

        # ------------------------------------------------------------------
        # 12. 反作弊识别
        # ------------------------------------------------------------------
        acdir = tmp / "ACGame"
        (acdir / "EasyAntiCheat").mkdir(parents=True, exist_ok=True)
        (acdir / "EasyAntiCheat" / "EasyAntiCheat.exe").write_bytes(b"x")
        found = ac.scan_directory(acdir)
        r.check("反作弊：能识别 EasyAntiCheat", "Easy Anti-Cheat" in found, f"found={found}")
        clean = tmp / "CleanGame"
        clean.mkdir(exist_ok=True)
        r.eq("反作弊：干净目录不误报", ac.scan_directory(clean), [])

        # ------------------------------------------------------------------
        # 13. 进程工具
        # ------------------------------------------------------------------
        r.check("进程：能列出当前进程", len(proc.list_process_names()) > 0)
        r.check("进程：不存在的进程名判为未运行", not proc.is_running("definitely-not-running-xyz.exe"))

        # ------------------------------------------------------------------
        # 14. HAGS（硬件加速 GPU 计划）—— 帧生成的硬性系统前置条件
        # ------------------------------------------------------------------
        from . import winenv

        hags_on, hags_raw = winenv.read_hags()
        r.check(
            "HAGS：能读出硬件加速 GPU 计划状态",
            hags_on is not None or hags_raw is None,
            f"enabled={hags_on} raw={hags_raw}",
        )
        r.check(
            "HAGS：状态描述可读",
            isinstance(winenv.describe(hags_on, hags_raw), str)
            and len(winenv.describe(hags_on, hags_raw)) > 0,
            winenv.describe(hags_on, hags_raw),
        )
        r.eq(
            "HAGS：needs_enabling 与读取结果一致",
            winenv.needs_enabling(),
            (None if hags_on is None else (not hags_on)),
        )
        r.check(
            "HAGS：非管理员时拒绝修改（不会静默改系统设置）",
            winenv.is_admin() or (winenv.set_hags(True)[0] is False),
            "在无管理员权限时必须返回失败而不是强行写入",
        )
        # 环境报告必须带上 HAGS —— 这是最容易误判成"Mod 失效"的一项
        rep_txt = None
        try:
            from . import report as _report

            rep_txt = _report.environment_text(env)
        except Exception as exc:
            rep_txt = f"EXC {exc}"
        r.check("HAGS：会出现在环境报告里", rep_txt is not None and "硬件加速GPU计划" in rep_txt)

        # 预检必须就 HAGS 给出结论
        pf_hags = installer.preflight(
            ed4, ed4 / "FakeGame-Win64-Shipping.exe", "version.dll", "SM86", env=env
        )
        r.check(
            "HAGS：预检里会明确提示",
            any("硬件加速" in c.title for c in pf_hags.checks),
            "; ".join(c.title for c in pf_hags.checks if "硬件加速" in c.title) or "缺失",
        )

        # ------------------------------------------------------------------
        # 15. 卸载安全护栏：游戏运行中必须拒绝
        #     （用 explorer.exe 这个必定在运行的进程名造场景）
        # ------------------------------------------------------------------
        gdirF = tmp / "GameF"
        edF = _make_fake_game(gdirF, foreign=False)
        installer.execute_plan(
            installer.make_plan(edF, edF / "FakeGame-Win64-Shipping.exe", "version.dll", "SM86", "GameF")
        )
        # 用合成的 PE 造一个"正在运行"的进程名场景：
        # explorer.exe 是必定在运行的进程，把它复制进游戏目录即可
        (edF / "explorer.exe").write_bytes(synth_pe())
        urF = installer.uninstall(edF)
        r.check(
            "护栏：游戏运行中拒绝卸载",
            (not urF.success) and (edF / "version.dll").is_file() and "正在运行" in urF.message,
            urF.message,
        )
        (edF / "explorer.exe").unlink()
        urF2 = installer.uninstall(edF)
        r.check("护栏：退出游戏后卸载成功", urF2.success, urF2.message)

        # ------------------------------------------------------------------
        # 15. 删除失败绝不许谎报成功
        # ------------------------------------------------------------------
        gdirH = tmp / "GameH"
        edH = _make_fake_game(gdirH, foreign=False)
        installer.execute_plan(
            installer.make_plan(edH, edH / "FakeGame-Win64-Shipping.exe", "version.dll", "SM86", "GameH")
        )
        import ctypes as _ct
        from ctypes import wintypes as _wt

        _k32 = _ct.WinDLL("kernel32", use_last_error=True)
        _k32.CreateFileW.restype = _wt.HANDLE
        _k32.CreateFileW.argtypes = [
            _wt.LPCWSTR, _wt.DWORD, _wt.DWORD, _ct.c_void_p, _wt.DWORD, _wt.DWORD, _wt.HANDLE
        ]
        INVALID = _ct.c_void_p(-1).value
        _h = _k32.CreateFileW(str(edH / "version.dll"), 0x80000000, 0, None, 3, 0, None)  # 共享模式 0 = 独占
        r.check("占用测试：成功独占锁定 version.dll", _h != INVALID, f"handle={_h}")
        urH = installer.uninstall(edH, force=True)
        r.check(
            "删除失败：不谎报成功",
            (not urH.success) and "未能删除" in urH.message,
            urH.message,
        )
        r.check("删除失败：保留安装记录以便重试", installer.find_install(edH) is not None)
        _k32.CloseHandle(_h)
        urH2 = installer.uninstall(edH)
        r.check("解锁后：卸载成功", urH2.success, urH2.message)

        # ------------------------------------------------------------------
        # 16. 状态文件丢失后仍能识别并干净卸载（孤儿安装）
        # ------------------------------------------------------------------
        gdirG = tmp / "GameG"
        edG = _make_fake_game(gdirG, foreign=False)
        installer.execute_plan(
            installer.make_plan(edG, edG / "FakeGame-Win64-Shipping.exe", "version.dll", "SM86", "GameG")
        )
        keep_state = state_p.read_bytes() if state_p.is_file() else None
        if state_p.is_file():
            state_p.unlink()
        vrG = installer.verify(edG)
        r.check(
            "孤儿：状态丢失后仍能按内置哈希识别出本工具的文件",
            vrG.installed and any("没有安装记录" in c.title for c in vrG.details),
            "; ".join(c.title for c in vrG.details),
        )
        urG = installer.uninstall(edG)
        r.check(
            "孤儿：无记录也能干净卸载",
            urG.success and not (edG / "version.dll").exists() and not (edG / INI_NAME).exists(),
            urG.message,
        )
        if keep_state is not None:
            state_p.write_bytes(keep_state)

        # ------------------------------------------------------------------
        # 17. 以 SM75 路由走一遍完整安装/校验/卸载（RTX 20 系用户的路径）
        # ------------------------------------------------------------------
        gdirI = tmp / "GameI"
        edI = _make_fake_game(gdirI, foreign=False)
        resI = installer.execute_plan(
            installer.make_plan(edI, edI / "FakeGame-Win64-Shipping.exe", "version.dll", "SM75", "GameI")
        )
        r.check("SM75 链路：安装成功", resI.success, resI.message)
        r.eq(
            "SM75 链路：落盘 INI 的 Router 是 SM75",
            installer.read_ini_router(edI / INI_NAME),
            "SM75",
        )
        r.check(
            "SM75 链路：落盘 INI 里 HardwareBilinear=0",
            "HardwareBilinear=0" in (edI / INI_NAME).read_text("utf-8"),
        )
        r.check(
            "SM75 链路：DLL 与 0.2.4 的 payload 哈希一致",
            installer.sha256_file(edI / "version.dll")
            == profiles.PROFILE_024.manifest["version.dll"][0],
            f"{profiles.PROFILE_024.manifest['version.dll'][0][:16]}…",
        )
        vrI = installer.verify(edI)
        r.check("SM75 链路：体检健康", vrI.installed and vrI.healthy)
        urI = installer.uninstall(edI)
        r.check("SM75 链路：卸载还原干净", urI.success and not (edI / INI_NAME).exists())

        # ------------------------------------------------------------------
        # 17b. 反复安装/卸载不会把"我们自己生成的 INI"当原始文件还原回去
        #      （真机实测踩到过：多次往返后目录里残留一份工具生成的 ini）
        # ------------------------------------------------------------------
        gdirJ = tmp / "GameJ"
        edJ = _make_fake_game(gdirJ, foreign=False)
        ur_last = None
        for round_no in range(3):
            installer.execute_plan(
                installer.make_plan(
                    edJ, edJ / "FakeGame-Win64-Shipping.exe",
                    "version.dll", "SM86", f"GameJ-{round_no}",
                )
            )
            ur_last = installer.uninstall(edJ)
            if not ur_last.success:
                break
        r.check("反复往返：三次安装卸载都成功",
                ur_last is not None and ur_last.success,
                ur_last.message if ur_last else "没跑")
        r.check(
            "反复往返：最终不残留本工具的任何文件",
            not (edJ / INI_NAME).exists() and not (edJ / "version.dll").exists(),
            f"残留：{sorted(p.name for p in edJ.iterdir())}",
        )

        # ------------------------------------------------------------------
        # 17c. CLI 输出绝不能因为打印失败而崩
        #      （真实反馈：管道提前关闭导致 OSError [Errno 22]，
        #        异常没人接，--windowed 打包的 exe 直接弹了个报错窗口）
        # ------------------------------------------------------------------
        from .cli import out as _out

        class _BadStream:
            def write(self, s):
                raise OSError(22, "Invalid argument")

            def flush(self):
                raise OSError(22, "Invalid argument")

            @property
            def buffer(self):
                raise OSError(22, "Invalid argument")

        _saved_out, _saved_err = sys.stdout, sys.stderr
        try:
            sys.stdout = _BadStream()
            _out("stdout 坏了")
            r.check("CLI：stdout 写入抛 OSError 时不崩", True)
        except Exception as _e:
            r.check("CLI：stdout 写入抛 OSError 时不崩", False, f"{type(_e).__name__}: {_e}")
        finally:
            sys.stdout = _saved_out

        try:
            sys.stdout = _BadStream()
            sys.stderr = _BadStream()
            _out("两个都坏了")
            r.check("CLI：stdout 和 stderr 同时坏也不崩", True)
        except Exception as _e:
            r.check("CLI：stdout 和 stderr 同时坏也不崩", False, f"{type(_e).__name__}: {_e}")
        finally:
            sys.stdout = _saved_out
            sys.stderr = _saved_err

        try:
            sys.stdout = None
            _out("stdout 是 None")
            r.check("CLI：stdout 为 None 时不崩", True)
        except Exception as _e:
            r.check("CLI：stdout 为 None 时不崩", False, f"{type(_e).__name__}: {_e}")
        finally:
            sys.stdout = _saved_out

        # 含中文 / emoji 也要能正常输出
        try:
            _out("中文 ★ ✓ ⚠ emoji😀")
            r.check("CLI：含中文和特殊字符时正常输出", True)
        except Exception as _e:
            r.check("CLI：含中文和特殊字符时正常输出", False, f"{type(_e).__name__}: {_e}")

        # ------------------------------------------------------------------
        # 17d. 卸载绝不能把"工具自己生成的 INI"还原回去
        #      真实踩过：残留检测用的标记字符串漏了「帧生成」三个字，
        #      导致防护静默失效，卸载后游戏目录里多出一份工具生成的 ini。
        #      这里对两个 upstream 版本各测一遍，并直接校验标记函数。
        # ------------------------------------------------------------------
        for _v in ("0.2.4", "0.3.0"):
            _ini_text = installer.build_ini("SM86", 0, 3, 1, version=_v)
            r.check(
                f"INI 标记：{_v} 生成的 INI 能被 is_our_ini 认出",
                profiles.is_our_ini(_ini_text),
                _ini_text.splitlines()[0] if _ini_text else "(空)",
            )
        r.check("INI 标记：非本工具的 INI 不会被误认",
                not profiles.is_our_ini("[Compatibility]\nRouter=SM86\n"))
        r.check("INI 标记：空内容不会被误认", not profiles.is_our_ini(""))

        # 端到端：先装一次（产生一份"我们自己的 ini"），卸载后目录必须干净
        gdirK = tmp / "GameK"
        edK = _make_fake_game(gdirK, foreign=False)
        installer.execute_plan(
            installer.make_plan(
                edK, edK / "FakeGame-Win64-Shipping.exe", "version.dll", "SM86", "GameK"
            )
        )
        # 把 ini 改掉，让下一次安装把它当作"需要备份的原始文件"
        (edK / INI_NAME).write_text(
            installer.build_ini("SM86", 0, 3, 1) + "; 用户随手加了点东西\n", "utf-8"
        )
        installer.execute_plan(
            installer.make_plan(
                edK, edK / "FakeGame-Win64-Shipping.exe", "version.dll", "SM86", "GameK"
            )
        )
        urK = installer.uninstall(edK)
        r.check(
            "反复往返：卸载后不会把工具自己的 INI 还原回来",
            not (edK / INI_NAME).exists(),
            f"残留 {sorted(p.name for p in edK.iterdir())}；{urK.message}",
        )

        # ------------------------------------------------------------------
        # 18. 路径安全：状态与备份只落在 LOCALAPPDATA
        # ------------------------------------------------------------------
        r.check(
            "路径：备份根目录位于 LOCALAPPDATA",
            str(installer.backups_root()).lower().startswith(
                (os.environ.get("LOCALAPPDATA") or "").lower()
            ),
            str(installer.backups_root()),
        )

    finally:
        # 恢复用户真实状态
        try:
            if saved_state is not None:
                state_p.write_bytes(saved_state)
            elif state_p.is_file():
                state_p.unlink()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
        # 清掉自检自己产生的备份目录 —— 用户按一下"安全自检"不该留下垃圾
        try:
            broot = installer.backups_root()
            if broot.is_dir():
                for d in broot.iterdir():
                    if "dlssg-selftest-" in d.name:
                        shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

    if verbose:
        for status, name, detail in r.rows:
            mark = "  OK  " if status == "PASS" else "  !!  "
            line = f"[{status}]{mark}{name}"
            if detail and status == "FAIL":
                line += f"\n         → {detail}"
            print(line)
        print()
        print(f"自检结果：{r.passed} 项通过，{r.failed} 项失败，共 {r.passed + r.failed} 项")
    return r


def main() -> int:
    r = run(verbose=True)
    return 0 if r.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
