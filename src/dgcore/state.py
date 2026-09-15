"""安装状态库与引擎互斥策略。

为什么单独一个模块
------------------
本工具现在有两个**互斥**的引擎（DLSSG 与 OptiScaler）。两边都要读写同一份
安装记录，也都需要"这个目录是不是已经被别的引擎占了"这个判断。

把状态放在 installer.py 里会造成循环依赖（installer → optiscaler → installer），
放在这里则两边都能安全引用：本模块只依赖 paths。

互斥为什么是硬策略而不是提醒
----------------------------
两个引擎都是从代理 DLL 钩住 D3D12 渲染路径。同时装上去的结果不是"效果差一点"，
而是游戏起不来或者画面彻底坏掉 —— 而且用户完全看不出是谁干的。
所以在预检阶段就拦住，不给"装上试试"的选项。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .paths import ensure_dirs, log, state_file

# 引擎标识
ENGINE_DLSSG = "dlssg"
ENGINE_OPTISCALER = "optiscaler"

ENGINE_NAMES = {
    ENGINE_DLSSG: "DLSSG（NVIDIA DLSS 帧生成）",
    ENGINE_OPTISCALER: "OptiScaler（XeSS 帧生成 / DLSS 5）",
}


def engine_of(record: dict | None) -> str:
    """读取一条安装记录的引擎。

    老记录没有 engine 字段 —— 那时候只有 DLSSG 一个引擎，所以缺省即 dlssg。
    """
    if not record:
        return ""
    return record.get("engine") or ENGINE_DLSSG


def engine_label(engine: str) -> str:
    return ENGINE_NAMES.get(engine, engine or "未知")


def path_key(path: str | Path) -> str:
    """把目标目录归一化成查表用的键。

    必须归一化：调用方可能给相对路径（测试）、也可能给已解析的绝对路径。
    不归一化就会出现"明明装了却查不到记录"，互斥保护随之失效 ——
    这是真正危险的那类 bug，因为失败的姿态是静默放行。
    """
    try:
        return str(Path(path).resolve()).lower()
    except Exception:
        return str(path).lower()


# --------------------------------------------------------------------------
# 状态读写
# --------------------------------------------------------------------------

def load_state() -> dict:
    try:
        p = state_file()
        if p.is_file():
            return json.loads(p.read_text("utf-8"))
    except Exception as exc:
        log(f"读取状态文件失败: {exc}", "warn")
    return {"version": 1, "installs": []}


def save_state(state: dict) -> None:
    ensure_dirs()
    p = state_file()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, p)


def record_install(rec: dict) -> None:
    """记一条安装。按 (游戏目录, 引擎) 唯一。

    共存模式下同一个目录会同时存在 DLSSG 引擎与 OptiScaler 引擎两条记录，
    各自独立卸载。早期版本按目录唯一 —— 理由是"互斥，同时只可能有一个引擎"。
    共存打破了这个前提：按目录去重会把另一个引擎的记录冲掉，于是它再也
    卸载不掉、变成残留。
    """
    rec = dict(rec)
    rec["target_dir_key"] = path_key(rec["target_dir"])
    eng = engine_of(rec)
    st = load_state()
    st.setdefault("installs", [])
    key = rec["target_dir_key"]
    st["installs"] = [
        i for i in st["installs"]
        if not (
            (i.get("target_dir_key") or path_key(i.get("target_dir", ""))) == key
            and engine_of(i) == eng
        )
    ]
    st["installs"].append(rec)
    save_state(st)


def forget_install(target_dir: str | Path, engine: str | None = None) -> None:
    """丢掉安装记录。

    engine=None  → 丢掉该目录的**全部**记录（清场用，旧行为）；
    engine=指定  → 只丢该引擎那条 —— 共存模式下卸载一个绝不动另一个。
    """
    st = load_state()
    key = path_key(target_dir)

    def keep(i: dict) -> bool:
        if (i.get("target_dir_key") or path_key(i.get("target_dir", ""))) != key:
            return True
        return engine is not None and engine_of(i) != engine

    st["installs"] = [i for i in st.get("installs", []) if keep(i)]
    save_state(st)


def find_install(target_dir: str | Path, engine: str | None = None) -> dict | None:
    """找该目录的安装记录。

    engine=None 返回任意一条；指定时只匹配该引擎（用于"记录还在、引擎换了"的识别）。
    """
    key = path_key(target_dir)
    for i in load_state().get("installs", []):
        if (i.get("target_dir_key") or path_key(i.get("target_dir", ""))) != key:
            continue
        if engine is None or engine_of(i) == engine:
            return i
    return None


def all_installs() -> list[dict]:
    return load_state().get("installs", [])


# --------------------------------------------------------------------------
# 互斥判断
# --------------------------------------------------------------------------

def other_engine_install(target_dir: str | Path, want_engine: str) -> dict | None:
    """该目录是否已经装了**另一个**引擎。是的话返回那条记录。"""
    rec = find_install(target_dir)
    if rec and engine_of(rec) != want_engine:
        return rec
    return None


def conflict_message(record: dict, want_engine: str) -> str:
    """给用户看的互斥说明。要能直接照着做。"""
    have = engine_of(record)
    return (
        f"这个目录已经装了「{engine_label(have)}」，"
        f"而你现在要装的是「{engine_label(want_engine)}」。\n\n"
        "两者都会从代理 DLL 钩住游戏的渲染路径，同时存在会让游戏起不来"
        "或者画面异常 —— 所以本工具不允许叠加安装。\n\n"
        "请先卸载现有的引擎（本工具「卸载」页，或命令行 uninstall），再装新的。\n"
        "卸载会把游戏目录还原成安装前的样子，不会留下残渣。"
    )


__all__ = [
    "ENGINE_DLSSG",
    "ENGINE_OPTISCALER",
    "ENGINE_NAMES",
    "engine_of",
    "engine_label",
    "path_key",
    "load_state",
    "save_state",
    "record_install",
    "forget_install",
    "find_install",
    "all_installs",
    "other_engine_install",
    "conflict_message",
]
