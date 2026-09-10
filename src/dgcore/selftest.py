"""内置自检 —— 在临时目录里完整跑一遍安装/校验/卸载/回滚。

给用户一个可以自己按的按钮：证明这个工具只会改游戏目录里那两个文件，
并且一定会备份、一定会还原、失败一定会回滚。

同时也是本项目的自动化回归测试。
运行： python -m dgcore.selftest   （或 exe --selftest）
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

from . import INI_NAME
from . import anticheat as ac
from . import games, gpu, installer, pe, proc
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


def _make_fake_game(root: Path, exe_name: str = "FakeGame-Win64-Shipping.exe", foreign: bool = True):
    """造一个假的游戏目录。"""
    exe_dir = root / "Game" / "Binaries" / "Win64"
    exe_dir.mkdir(parents=True, exist_ok=True)
    # 造一个带 d3d12 导入特征的假 EXE：直接复制系统里真导入 d3d12 的 dll 不行，
    # 用一段真实 PE（复制自身 python.exe 不行）——改为构造最小 PE 太脆弱，
    # 这里直接复制一个已知导入 d3d12.dll 的系统组件；找不到就跳过 D3D12 相关断言。
    (exe_dir / exe_name).write_bytes(b"MZ" + b"\x00" * 2048)
    if foreign:
        (exe_dir / "version.dll").write_bytes(FOREIGN_DLL)
    return exe_dir


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
        r.check(
            "payload：5 个代理 DLL 全部存在且哈希匹配",
            all(avail.values()) and len(avail) == 5,
            ", ".join(f"{k}={'OK' if v else 'BAD'}" for k, v in avail.items()),
        )
        r.eq("payload：版本号", len(MANIFEST), 5)
        v = installer.resolve_payload("version.dll")
        r.check(
            "payload：version.dll 与上游公布哈希一致",
            v.sha256.startswith("c844646d"),
            f"sha256={v.sha256[:24]}…",
        )
        bad = installer.PayloadFile("x.dll", Path("nope"), "", 0, False, "t")
        r.check("payload：缺失文件被判为不可用", not bad.ok)

        # ------------------------------------------------------------------
        # 2. INI 生成
        # ------------------------------------------------------------------
        i86 = installer.build_ini("SM86", 0, 3, 1)
        r.check("INI：SM86 路由正确", "Router=SM86" in i86 and "KernelImage=PTX" in i86)
        i75 = installer.build_ini("SM75", 1, 3, 1)
        r.check("INI：SM75 强制关闭近似采样", "Router=SM75" in i75 and "HardwareBilinear=0" in i75)
        perf = installer.build_ini("SM86", 1, 3, 1)
        r.check("INI：性能档写入 HardwareBilinear=1", "HardwareBilinear=1" in perf)
        clamp = installer.build_ini("SM86", 0, 99, 99)
        r.check("INI：倍率与日志级别被夹到合法区间", "MaxGeneratedFrames=3" in clamp and "Level=3" in clamp)
        r.check("INI：非法路由回落到 SM86", "Router=SM86" in installer.build_ini("XX99"))

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
        # 4. PE 解析
        # ------------------------------------------------------------------
        sys_exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "notepad.exe"
        if sys_exe.is_file():
            info = pe.inspect(sys_exe)
            r.check("PE：能解析系统 EXE", info.ok and info.is_x64, f"arch={info.arch}")
            r.check(
                "PE：导入表里全是合法 DLL 名",
                all(x.lower().endswith((".dll", ".drv", ".ocx", ".exe", ".cpl", ".sys")) for x in info.imports),
                f"{len(info.imports)} 项",
            )
            r.check("PE：notepad 不含 D3D12", not info.uses_d3d12)
        junk = tmp / "junk.exe"
        junk.write_bytes(b"not a pe")
        r.check("PE：非 PE 文件被安全拒绝", not pe.inspect(junk).ok)

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
            MANIFEST["version.dll"][0],
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
            ed4, Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "notepad.exe",
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
        shutil.copy2(
            Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "notepad.exe",
            edF / "explorer.exe",
        )
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
            "SM75 链路：DLL 与内置 payload 哈希一致",
            installer.sha256_file(edI / "version.dll") == MANIFEST["version.dll"][0],
        )
        vrI = installer.verify(edI)
        r.check("SM75 链路：体检健康", vrI.installed and vrI.healthy)
        urI = installer.uninstall(edI)
        r.check("SM75 链路：卸载还原干净", urI.success and not (edI / INI_NAME).exists())

        # ------------------------------------------------------------------
        # 17. 路径安全：状态与备份只落在 LOCALAPPDATA
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
