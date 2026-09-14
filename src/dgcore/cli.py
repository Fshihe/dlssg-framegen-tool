"""命令行界面 —— 既能给玩家当高级用法，也是自动化测试的入口。

python -m dgcore.cli <命令> ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import APP_NAME, INI_NAME, UPSTREAM_REPO, UPSTREAM_VERSION, VERSION
from . import anticheat as ac
from . import games, gpu, installer, pe, profiles, report
from .paths import logs_root, reports_root


def out(msg: str = "") -> None:
    """输出一行。**任何情况下都不允许抛异常。**

    控制台输出失败的原因很杂：代码页不对（UnicodeEncodeError）、
    stdout 被重定向到已关闭的管道 / 无效句柄（OSError: [Errno 22]）、
    打包成 GUI 子程序后根本没有标准输出（ValueError / AttributeError）。
    命令行工具因为"打印失败"而崩掉是最没道理的，所以这里兜到底。
    """
    text = f"{msg}\n"
    # 逐个尝试，任一条成功就返回
    for attempt in (
        lambda: sys.stdout.write(text),
        lambda: sys.stdout.buffer.write(text.encode("utf-8", "replace")),
        lambda: sys.stderr.write(text),
        lambda: sys.stderr.buffer.write(text.encode("utf-8", "replace")),
    ):
        try:
            attempt()
            try:
                sys.stdout.flush()
            except Exception:
                pass
            return
        except Exception:
            continue
    # 全失败（比如完全没有标准流）：静默丢弃，绝不崩


def _hdr(title: str) -> None:
    out("")
    out("=" * 66)
    out(f"  {title}")
    out("=" * 66)


# --------------------------------------------------------------------------
# 目标解析
# --------------------------------------------------------------------------

def resolve_target(path: str, router: str = "SM86") -> tuple[Path, Path | None, str]:
    """把用户给的路径解析成 (游戏目录, 主程序, 安装目录)。

    支持三种输入：
      - 游戏主程序 .exe  → 直接用它所在目录
      - 游戏根目录        → 自动找出真正的渲染 EXE
      - 已经是正确的目录  → 直接在目录里找 EXE
    """
    p = Path(path).expanduser()
    if not p.exists():
        raise SystemExit(f"路径不存在：{p}")

    if p.is_file():
        if p.suffix.lower() != ".exe":
            raise SystemExit(f"不是 EXE 文件：{p}")
        return p.parent, p, str(p.parent)

    cands = games.find_render_exes(p)
    elig = [c for c in cands if c.eligible]
    if elig:
        best = max(elig, key=lambda c: c.score)
        return p, best.path, str(best.directory)
    if cands:
        best = cands[0]
        return p, best.path, str(best.directory)
    return p, None, str(p)


def _print_candidates(cands: list[games.ExeCandidate], limit: int = 8) -> None:
    for c in cands[:limit]:
        tag = "可安装" if c.eligible else "不适用"
        out(f"  [{tag}] 评分 {c.score:>4}  {c.name}  ({c.size/1048576:.1f} MB)")
        out(f"            {c.directory}")
        for r in c.reasons:
            out(f"            · {r}")


def _print_checks(checks: list[installer.Check]) -> None:
    icon = {"ok": "  [OK]  ", "warn": "  [警告]", "error": "  [错误]"}
    for c in checks:
        out(f"{icon.get(c.level, '  [??]  ')}{c.title}")
        if c.detail and c.level != "ok":
            for line in str(c.detail).splitlines():
                out(f"           {line}")


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------

def cmd_detect(args) -> int:
    env = gpu.detect()
    if args.json:
        out(json.dumps(
            {
                "system": vars(env.system),
                "router": env.router,
                "nvidia_smi": env.nvidia_smi,
                "notes": env.notes,
                "gpus": [vars(g) for g in env.gpus],
            },
            ensure_ascii=False, indent=2,
        ))
        return 0
    _hdr(f"{APP_NAME} v{VERSION} — 环境检测")
    out(report.environment_text(env))
    return 0


def cmd_list(args) -> int:
    _hdr("扫描已安装的游戏" if not args.d3d12 else "扫描支持 D3D12 的游戏")
    libs = games.scan_libraries()
    out(f"游戏库中发现 {len(libs)} 个条目，正在分析主程序…")
    rows = []
    for g in libs:
        games.inspect_game(g)
        b = g.best
        if args.d3d12 and not (b and b.eligible):
            continue
        rows.append(g)
    rows.sort(key=lambda g: g.name.lower())
    if args.json:
        out(json.dumps(
            [
                {
                    "name": g.name, "root": str(g.root), "source": g.source, "appid": g.appid,
                    "exe": str(g.best.path) if g.best else "",
                    "eligible": bool(g.best and g.best.eligible),
                    "score": g.best.score if g.best else 0,
                    "has_dlssg": bool(g.best and g.best.has_dlssg),
                }
                for g in rows
            ],
            ensure_ascii=False, indent=2,
        ))
        return 0
    out("")
    for g in rows:
        b = g.best
        mark = "★" if (b and b.eligible) else " "
        fg = " [原生支持帧生成]" if (b and b.has_dlssg) else ""
        out(f"{mark} {g.name}{fg}")
        out(f"    {b.path if b else '(未找到主程序)'}")
        out(f"    来源 {g.source} · {g.root}")
    out("")
    out(f"共 {len(rows)} 个。★ = 可以安装本 Mod（D3D12 + 64 位）")
    return 0


def cmd_inspect(args) -> int:
    p = Path(args.path).expanduser()
    _hdr(f"检查：{p}")
    if p.is_file() and p.suffix.lower() == ".exe":
        info = pe.inspect(p)
        out(f"架构      : {info.arch}   64 位: {'是' if info.is_x64 else '否'}")
        out(f"体积      : {info.size/1048576:.1f} MB")
        out(f"D3D12     : {'是' if info.uses_d3d12 else '否'}")
        out(f"D3D11     : {'是' if info.uses_d3d11 else '否'}")
        out(f"Vulkan    : {'是' if info.uses_vulkan else '否'}")
        out(f"版本资源  : {pe.file_version_string(p)}")
        out(f"导入表({len(info.imports)}):")
        for i in info.imports:
            out(f"   {i}")
        return 0

    out("候选主程序：")
    cands = games.find_render_exes(p)
    _print_candidates(cands, limit=args.limit)
    if not cands:
        out("  (没找到符合条件的 EXE)")
    acr = ac.scan(p)
    out("")
    out(f"反作弊：{acr.summary}")
    return 0


def cmd_check(args) -> int:
    env = gpu.detect()
    root, exe, target = resolve_target(args.path, args.router)
    router = args.router if args.router != "auto" else env.router
    proxy = args.proxy or installer.auto_pick_proxy(Path(target))[0]
    _hdr("安装前预检")
    out(f"游戏目录  : {target}")
    out(f"主程序    : {exe or '(未找到)'}")
    out(f"代理入口  : {proxy}")
    out(f"计算路由  : {router}")
    out("")
    acr = ac.scan(root)
    pf = installer.preflight(Path(target), exe, proxy, router, env=env, anticheat=acr)
    _print_checks(pf.checks)
    out("")
    out(f"结论：{'可以安装' if pf.ok else '存在阻断问题，不能安装'}")
    return 0 if pf.ok else 2


def cmd_plan(args) -> int:
    env = gpu.detect()
    root, exe, target = resolve_target(args.path, args.router)
    router = args.router if args.router != "auto" else env.router
    prof = profiles.for_router(router, prefer=getattr(args, "upstream", None))
    proxy, why = ((args.proxy, "手动指定") if args.proxy
                  else installer.auto_pick_proxy(Path(target), profile=prof))
    plan = installer.make_plan(
        Path(target), exe, proxy, router,
        game_name=root.name, hardware_bilinear=args.bilinear,
        max_generated_frames=args.frames, log_level=args.log_level,
        version=prof.version,
    )
    _hdr("将要执行的改动（预演，不会真的写入）")
    out(f"上游版本：{plan.version}（{prof.explanation}）")
    out(f"入口选择：{why}")
    out("")
    for it in plan.items:
        act = {
            installer.ACT_COPY: "写入",
            installer.ACT_SKIP: "跳过（内容已一致）",
            installer.ACT_REMOVE: "删除",
        }.get(it.action, it.action)
        out(f"  [{act}] {it.dst}   {it.note}")
    out("")
    out("生成的 dlssg_sm86.ini 内容：")
    out("-" * 40)
    out(plan.ini_text)
    out("-" * 40)
    return 0


def cmd_install(args) -> int:
    env = gpu.detect()
    root, exe, target = resolve_target(args.path, args.router)
    router = args.router if args.router != "auto" else env.router
    prof = profiles.for_router(router, prefer=getattr(args, "upstream", None))
    proxy, why = ((args.proxy, "手动指定") if args.proxy
                  else installer.auto_pick_proxy(Path(target), profile=prof))
    tdir = Path(target)

    _hdr("一键安装 DLSS 帧生成支持")
    out(f"游戏      : {root.name}")
    out(f"游戏目录  : {tdir}")
    out(f"主程序    : {exe or '(未找到)'}")
    out(f"上游版本  : {prof.version}（{prof.explanation}）")
    out(f"代理入口  : {proxy}（{why}）")
    out(f"计算路由  : {router}")
    if router == "SM75":
        out("")
        out("⚠ 20 系为实验性支持：上游 0.3.0 已移除 SM75 内核，本工具自动改用 0.2.4。")
        out("  可能出现画面闪烁、拖影或闪退；出问题可随时卸载还原。")

    acr = ac.scan(root)
    pf = installer.preflight(tdir, exe, proxy, router, env=env, anticheat=acr,
                             version=prof.version)
    out("")
    _print_checks(pf.checks)

    if not pf.ok:
        if not (args.force and all("反作弊" in c.title or "正在运行" in c.title for c in pf.errors)):
            out("")
            out("存在阻断问题，已中止。请按上面的提示处理后重试。")
            return 2
        out("")
        out("警告：你用 --force 跳过了上述阻断项，风险自负。")

    plan = installer.make_plan(
        tdir, exe, proxy, router, game_name=root.name,
        hardware_bilinear=args.bilinear, max_generated_frames=args.frames,
        log_level=args.log_level, version=prof.version,
    )
    out("")
    out("改动清单：")
    for it in plan.items:
        act = {installer.ACT_COPY: "写入", installer.ACT_SKIP: "跳过", installer.ACT_REMOVE: "删除"}.get(it.action, it.action)
        out(f"  [{act}] {it.dst}")

    if args.dry_run:
        out("")
        out("（--dry-run：到此为止，未做任何改动）")
        return 0

    if not args.yes:
        out("")
        try:
            ans = input("确认执行？输入 y 回车继续：").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            out("已取消，未做任何改动。")
            return 1

    res = installer.execute_plan(plan)
    out("")
    if not res.success:
        out(f"安装失败：{res.message}")
        for e in res.errors:
            out(f"  {e}")
        return 3
    out(f"安装成功：{res.message}")
    if res.backup_dir:
        out(f"原文件已备份到：{res.backup_dir}")

    vr = installer.verify(tdir)
    out("")
    out("安装后体检：")
    _print_checks(vr.details)
    out("")
    out("现在启动游戏，在画面设置里把「帧生成 / Frame Generation」打开即可。")
    out("如果游戏里没有出现该选项，请用日志级别 2 重装后再看游戏目录下的 dlssg_sm86\\logs。")
    return 0 if vr.healthy else 4


def cmd_uninstall(args) -> int:
    root, exe, target = resolve_target(args.path)
    tdir = Path(target)
    _hdr("卸载并还原")
    out(f"游戏目录：{tdir}")
    res = installer.uninstall(tdir, force=args.force)
    out("")
    out(res.message)
    if res.restored:
        out(f"已还原：{'、'.join(res.restored)}")
    out(f"结果：{'成功' if res.success else '未完成'}")
    return 0 if res.success else 1


def cmd_verify(args) -> int:
    root, exe, target = resolve_target(args.path)
    _hdr("安装体检")
    vr = installer.verify(Path(target))
    out(f"目录：{target}")
    out(f"本工具安装过：{'是' if vr.installed else '否'}")
    out("")
    _print_checks(vr.details)
    out("")
    out(f"结论：{'完好' if vr.healthy else ('未安装' if not vr.installed else '异常')}")
    return 0 if vr.healthy else 1


def cmd_hags(args) -> int:
    from . import winenv

    enabled, raw = winenv.read_hags()
    if args.enable or args.disable:
        want = bool(args.enable)
        if not args.yes:
            out(f"即将{'开启' if want else '关闭'}「硬件加速 GPU 计划」")
            out(f"  注册表：HKLM\\{winenv.HAGS_KEY}\\{winenv.HAGS_VALUE}")
            out(f"  改动：HwSchMode {raw} → {winenv.HAGS_ON if want else winenv.HAGS_OFF}")
            out("  （这与 Windows 设置界面里的开关是同一件事，改完需重启电脑）")
            try:
                ans = input("确认执行？输入 y 回车继续：").strip().lower()
            except EOFError:
                ans = ""
            if ans not in ("y", "yes"):
                out("已取消，未做任何改动。")
                return 1
        ok, msg = winenv.set_hags(want)
        out(msg)
        return 0 if ok else 3

    _hdr("硬件加速 GPU 计划 (HAGS)")
    out(f"当前状态：{winenv.describe(enabled, raw)}")
    out(f"注册表位置：HKLM\\{winenv.HAGS_KEY}  →  {winenv.HAGS_VALUE}")
    out("")
    if enabled is False:
        out("⚠ DLSS 帧生成硬性依赖这一项。关闭时，即使 Mod 装对了，")
        out("  游戏也会提示「您的显卡不支持 DLSS 帧生成技术」。")
        out("")
        out(f"开启方法（任选其一）：")
        out(f"  1. {winenv.HAGS_GUI_PATH}")
        out(f"  2. 本工具图形界面里的「开启硬件加速 GPU 计划」按钮")
        out(f"  3. 命令行：dlssg-tool hags --enable -y")
        out("")
        out("改完必须重启电脑才会生效。")
        return 2
    if enabled is True:
        out("✓ 前置条件满足。")
        return 0
    out("读不到状态（可能需要管理员权限）。")
    return 3


def cmd_collect(args) -> int:
    """把环境 + 安装状态 + Mod 运行日志汇总成一个文件，方便远程排查。

    在出问题的机器上跑一次，把生成的文件发出去，对方就能看清卡在哪一环。
    """
    import time as _time

    env = gpu.detect()
    L: list[str] = [report.environment_text(env), ""]
    L.append("=" * 70)
    L.append("诊断信息")
    L.append("=" * 70)

    if args.path:
        _, _, target = resolve_target(args.path)
        targets = [Path(target)]
    else:
        targets = [Path(i["target_dir"]) for i in installer.all_installs()]

    if not targets:
        L.append("(没有安装记录，也没有指定游戏目录)")
    for t in targets:
        L.append("")
        L.append("-" * 70)
        L.append(f"游戏目录：{t}")
        L.append("-" * 70)

        present = [
            p.name for p in t.iterdir()
            if p.name.lower() in ("version.dll", "winmm.dll", "dinput8.dll", "winhttp.dll", "dxgi.dll")
        ] if t.is_dir() else []
        L.append(f"[目录里的代理入口 DLL] {present or '(无)'}")

        ini = t / INI_NAME
        if ini.is_file():
            L.append("")
            L.append(f"[{INI_NAME} 内容]")
            L.extend(ini.read_text("utf-8", "ignore").splitlines())
        else:
            L.append(f"[{INI_NAME}] 不存在")

        vr = installer.verify(t)
        L.append("")
        L.append(f"[体检] 本工具安装过={vr.installed}  健康={vr.healthy}")
        for c in vr.details:
            L.append(f"    [{c.level}] {c.title}  {c.detail}")

        logdir = t / "dlssg_sm86" / "logs"
        if logdir.is_dir():
            logs = sorted(logdir.glob("*.jsonl"))
            if logs:
                for lg in logs:
                    L.append("")
                    L.append(f"[Mod 运行日志 {lg.name}]（最多 300 行）")
                    L.extend(lg.read_text("utf-8", "ignore").splitlines()[:300])
            else:
                L.append("")
                L.append("[Mod 日志] 目录存在但没有日志文件")
        else:
            L.append("")
            L.append("[Mod 日志] 没有 dlssg_sm86 目录")
            L.append("    → 可能原因：① 游戏没加载这个代理 DLL；② 日志级别是 1（只记错误）")
            L.append("    → 建议：用 --log-level 2 重新安装后再进一次游戏")

    out_file = reports_root() / f"诊断包-{_time.strftime('%Y%m%d-%H%M%S')}.txt"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("\n".join(L), encoding="utf-8")

    _hdr("诊断包已生成")
    out(f"文件：{out_file}")
    out(f"大小：{out_file.stat().st_size:,} 字节")
    out("")
    out("把这个文件发出去即可 —— 里面包含环境、安装状态、INI 内容和 Mod 运行日志。")
    return 0


def cmd_report(args) -> int:
    env = gpu.detect()
    p = report.write_report(env, reports_root())
    out(f"环境报告已生成：{p}")
    out("")
    out(report.environment_text(env))
    return 0


def cmd_selftest(args) -> int:
    from .selftest import run

    _hdr("内置安全自检")
    out("在临时目录里完整跑一遍安装/校验/卸载/回滚，不碰你的任何游戏。")
    out("")
    r = run(verbose=True)
    return 0 if r.failed == 0 else 1


def cmd_paths(args) -> int:
    _hdr("本工具用到的目录")
    out(f"数据目录：{installer.backups_root().parent}")
    out(f"备份目录：{installer.backups_root()}")
    out(f"日志目录：{logs_root()}")
    out(f"状态文件：{installer.state_file()}")
    out(f"报告目录：{reports_root()}")
    out(f"DLL 来源：{installer.payload_dir()}")
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dlssg-tool",
        description=f"{APP_NAME} v{VERSION} —— 为 RTX 20/30 系显卡一键开启 DLSS 帧生成",
        epilog=f"上游 Mod：{UPSTREAM_REPO}  ({UPSTREAM_VERSION})",
    )
    p.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    sub = p.add_subparsers(dest="cmd")

    def add_common(sp):
        sp.add_argument("--proxy", help="代理入口 DLL 名（默认自动挑选）")
        sp.add_argument("--router", default="auto", choices=["auto", "SM86", "SM75"],
                        help="计算路由（默认自动按显卡判断）")
        sp.add_argument("--upstream", choices=list(profiles.PROFILES),
                        help="强制指定上游版本（默认按路由自动选：SM86→0.3.0，SM75→0.2.4）")

    sp = sub.add_parser("detect", help="检测显卡与系统环境")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_detect)

    sp = sub.add_parser("list", help="列出已安装的游戏")
    sp.add_argument("--d3d12", action="store_true", help="只列出可安装的（D3D12）游戏")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("inspect", help="检查某个游戏目录或 EXE")
    sp.add_argument("path")
    sp.add_argument("--limit", type=int, default=8)
    sp.set_defaults(func=cmd_inspect)

    sp = sub.add_parser("check", help="安装前预检")
    sp.add_argument("path")
    add_common(sp)
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("plan", help="预演将要做的改动")
    sp.add_argument("path")
    add_common(sp)
    sp.add_argument("--bilinear", type=int, default=0, choices=[0, 1])
    sp.add_argument("--frames", type=int, default=3, choices=[1, 2, 3])
    sp.add_argument("--log-level", type=int, default=1, choices=[0, 1, 2, 3])
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("install", help="一键安装")
    sp.add_argument("path")
    add_common(sp)
    sp.add_argument("--bilinear", type=int, default=0, choices=[0, 1])
    sp.add_argument("--frames", type=int, default=3, choices=[1, 2, 3])
    sp.add_argument("--log-level", type=int, default=1, choices=[0, 1, 2, 3])
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("-y", "--yes", action="store_true", help="不询问直接执行")
    sp.add_argument("--force", action="store_true", help="跳过反作弊/进程运行等阻断项（风险自负）")
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("uninstall", help="卸载并还原")
    sp.add_argument("path")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_uninstall)

    sp = sub.add_parser("verify", help="安装体检")
    sp.add_argument("path")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("report", help="生成环境报告")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("collect", help="收集诊断包（环境+安装状态+Mod 日志），方便发给别人排查")
    sp.add_argument("path", nargs="?", help="游戏目录（省略则收集所有安装记录）")
    sp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("hags", help="检查/开启「硬件加速 GPU 计划」（帧生成的前置条件）")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--enable", action="store_true", help="开启（需管理员 + 重启）")
    g.add_argument("--disable", action="store_true", help="关闭")
    sp.add_argument("-y", "--yes", action="store_true", help="不询问直接执行")
    sp.set_defaults(func=cmd_hags)

    sp = sub.add_parser("selftest", help="运行内置安全自检")
    sp.set_defaults(func=cmd_selftest)

    sp = sub.add_parser("paths", help="显示工具用到的目录")
    sp.set_defaults(func=cmd_paths)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
