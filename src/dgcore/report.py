"""环境报告生成 —— 出问题时可以把它贴给作者。"""

from __future__ import annotations

import platform
import time
from pathlib import Path

from . import APP_NAME, UPSTREAM_REPO, UPSTREAM_VERSION, VERSION
from .gpu import EnvReport
from .installer import VerifyResult, all_installs, verify
from .payload_manifest import MANIFEST, PAYLOAD_VERSION
from .paths import log as _log


def environment_text(env: EnvReport) -> str:
    L: list[str] = []
    L.append(f"=== {APP_NAME} v{VERSION} 环境报告 ===")
    L.append(f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    L.append(f"上游 Mod: {UPSTREAM_VERSION}  {UPSTREAM_REPO}")
    L.append("")
    L.append("--- 系统 ---")
    L.append(f"系统      : {env.system.os_caption}")
    L.append(f"内核版本  : {env.system.os_build}")
    L.append(f"架构      : {env.system.arch}")
    L.append(f"管理员权限: {'是' if env.system.is_admin else '否'}")
    L.append(f"Python    : {platform.python_version()}")
    L.append("")
    L.append("--- 显卡 ---")
    if not env.gpus:
        L.append("(未检测到显示适配器)")
    for g in env.gpus:
        L.append(f"名称      : {g.name}")
        L.append(f"  架构    : {g.family or '未知'}  (SM={g.sm or '?'})")
        L.append(f"  驱动    : {g.driver or '未知'}")
        L.append(f"  显存    : {g.vram_mib} MiB")
        L.append(f"  笔记本版: {'是' if g.is_laptop else '否'}")
        L.append(f"  结论    : {g.verdict} / Router={g.router or '(不适用)'}")
        L.append(f"  说明    : {g.reason}")
    L.append("")
    L.append(f"nvidia-smi 可用: {'是' if env.nvidia_smi else '否'}")
    from . import winenv

    L.append(f"硬件加速GPU计划(HAGS): {winenv.describe(env.hags, env.hags_raw)}")
    if env.hags is False:
        L.append(f"  ↑ DLSS 帧生成的硬性前置条件，开启路径：{winenv.HAGS_GUI_PATH}")
    for n in env.notes:
        L.append(f"备注: {n}")
    L.append("")
    L.append("--- 内置 DLL 完整性 ---")
    for name, (h, size) in MANIFEST.items():
        L.append(f"{name:14} {h}  {size} bytes")
    L.append(f"(payload 版本 {PAYLOAD_VERSION})")
    L.append("")
    L.append("--- 本工具的安装记录 ---")
    inst = all_installs()
    if not inst:
        L.append("(无)")
    for i in inst:
        vr = verify(i["target_dir"])
        L.append(f"游戏    : {i.get('game_name')}")
        L.append(f"  目录  : {i.get('target_dir')}")
        L.append(f"  主程序: {i.get('exe')}")
        L.append(f"  入口  : {i.get('proxy')}  Router={i.get('router')}")
        L.append(f"  时间  : {i.get('installed_at')}")
        L.append(f"  体检  : {'健康' if vr.healthy else '异常'}")
        for c in vr.details:
            if c.level != "ok":
                L.append(f"    [{c.level}] {c.title} {c.detail}")
    return "\n".join(L)


def write_report(env: EnvReport, directory: str | Path) -> Path:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"dlssg-env-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    p.write_text(environment_text(env), encoding="utf-8")
    _log(f"环境报告已写入 {p}")
    return p


__all__ = ["environment_text", "write_report"]
