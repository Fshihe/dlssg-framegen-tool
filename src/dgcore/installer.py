"""安装 / 校验 / 卸载 / 回滚引擎 —— 本工具的安全核心。

写盘范围被死死限制在：
  A. 用户选定的游戏目录里，恰好 2 个文件：<代理>.dll 与 dlssg_sm86.ini
  B. %LOCALAPPDATA%\\DLSSG-SM86-Tool\\ 下的备份与状态

不写注册表、不装驱动、不改系统设置、不动杀软、不改 PATH。
每一步都先备份、再原子替换、再校验哈希，任何一步失败立即整体回滚。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import DEFAULT_PROXY, INI_NAME, PROXY_ENTRIES, UPSTREAM_VERSION
from . import proc
from . import winenv
from .gpu import VERDICT_OK, VERDICT_NOT_NEEDED
from .paths import (
    backups_root,
    ensure_dirs,
    exe_dir,
    journal,
    log,
    resource_root,
    state_file,
)
from .payload_manifest import MANIFEST, PAYLOAD_VERSION

# 代理入口优先级：version.dll 是上游默认，其余是备用
PROXY_ORDER = ["version.dll", "winmm.dll", "dinput8.dll", "winhttp.dll", "dxgi.dll"]

TMP_SUFFIX = ".dlssgtool.tmp"

# 动作类型
ACT_BACKUP = "backup"
ACT_COPY = "copy"
ACT_SKIP = "skip"
ACT_REMOVE = "remove"
ACT_RESTORE = "restore"


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def sha256_file(path: str | Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def try_hash(path: str | Path) -> str:
    """读不到就返回空串 —— 文件被游戏独占锁定时读哈希会抛 PermissionError，
    卸载/体检流程绝不能被这个异常打断。"""
    try:
        return sha256_file(path)
    except OSError:
        return ""


def same_content(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return sha256_file(a) == sha256_file(b)
    except OSError:
        return False


def sanitize_key(path: str | Path) -> str:
    s = str(path)
    s = re.sub(r"^[A-Za-z]:[\\/]", "", s)
    s = re.sub(r"[^A-Za-z0-9._\u4e00-\u9fff-]+", "_", s)
    return s.strip("_")[:120] or "root"


# --------------------------------------------------------------------------
# payload 解析与校验
# --------------------------------------------------------------------------

@dataclass
class PayloadFile:
    name: str
    path: Path
    sha256: str
    size: int
    ok: bool
    message: str = ""


def payload_dir() -> Path:
    """payload 目录：优先用 exe 旁边的（便于用户自行更新），否则用内置解包目录。"""
    side = exe_dir() / "payload"
    if side.is_dir() and any(side.glob("*.dll")):
        return side
    return resource_root() / "payload"


def resolve_payload(proxy: str) -> PayloadFile:
    """取出指定代理 DLL 并做完整性校验。"""
    name = proxy if proxy.lower().endswith(".dll") else proxy + ".dll"
    path = payload_dir() / name
    expect = MANIFEST.get(name)
    if not path.is_file():
        return PayloadFile(name, path, "", 0, False, f"找不到内置 payload：{path}")
    size = path.stat().st_size
    digest = sha256_file(path)
    if expect is None:
        return PayloadFile(name, path, digest, size, False, "该文件不在完整性基线内")
    exp_hash, exp_size = expect
    if size != exp_size:
        return PayloadFile(name, path, digest, size, False, f"体积不符：{size} != {exp_size}")
    if digest != exp_hash:
        return PayloadFile(
            name, path, digest, size, False,
            f"SHA256 不符，文件可能已损坏或被篡改（{digest[:16]}…）",
        )
    return PayloadFile(name, path, digest, size, True, "完整性校验通过")


def available_payloads() -> dict[str, bool]:
    return {p: resolve_payload(p).ok for p in PROXY_ORDER}


# --------------------------------------------------------------------------
# INI 生成
# --------------------------------------------------------------------------

def build_ini(
    router: str,
    hardware_bilinear: int = 0,
    max_generated_frames: int = 3,
    log_level: int = 1,
) -> str:
    """按上游 docs/NATIVE_INI.md 生成配置。

    SM75 不支持近似采样，强制写 0。
    """
    router = (router or "SM86").upper()
    if router not in ("SM86", "SM75"):
        router = "SM86"
    if router == "SM75":
        hardware_bilinear = 0
    hardware_bilinear = 1 if int(hardware_bilinear) else 0
    max_generated_frames = max(1, min(3, int(max_generated_frames)))
    log_level = max(0, min(3, int(log_level)))

    return (
        f"; 由 DLSSG 一键开启工具生成 — 上游 {UPSTREAM_VERSION}\n"
        f"; 修改后需要重启游戏才会生效。\n"
        f"; Router={router} 对应 {'RTX 30 系列 (Ampere)' if router == 'SM86' else 'RTX 20 系列 (Turing)'}\n"
        "\n"
        "[Compatibility]\n"
        f"Router={router}\n"
        "KernelImage=PTX\n"
        f"HardwareBilinear={hardware_bilinear}\n"
        "\n"
        "[FrameGeneration]\n"
        f"MaxGeneratedFrames={max_generated_frames}\n"
        "\n"
        "[Logging]\n"
        f"Level={log_level}\n"
    )


def read_ini_router(path: str | Path) -> str:
    try:
        text = Path(path).read_text("utf-8", "ignore")
        m = re.search(r"^\s*Router\s*=\s*(\w+)", text, re.M)
        return m.group(1).upper() if m else ""
    except OSError:
        return ""


# --------------------------------------------------------------------------
# 状态存储
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
    st = load_state()
    st.setdefault("installs", [])
    # 同一目标目录只保留最新一条
    st["installs"] = [i for i in st["installs"] if i.get("target_dir", "").lower() != rec["target_dir"].lower()]
    st["installs"].append(rec)
    save_state(st)


def forget_install(target_dir: str | Path) -> None:
    st = load_state()
    key = str(target_dir).lower()
    st["installs"] = [i for i in st.get("installs", []) if i.get("target_dir", "").lower() != key]
    save_state(st)


def find_install(target_dir: str | Path) -> dict | None:
    key = str(target_dir).lower()
    for i in load_state().get("installs", []):
        if i.get("target_dir", "").lower() == key:
            return i
    return None


def all_installs() -> list[dict]:
    return load_state().get("installs", [])


# --------------------------------------------------------------------------
# 预检
# --------------------------------------------------------------------------

@dataclass
class Check:
    level: str      # ok / warn / error
    title: str
    detail: str = ""


@dataclass
class Preflight:
    checks: list[Check] = field(default_factory=list)

    def add(self, level: str, title: str, detail: str = "") -> None:
        self.checks.append(Check(level, title, detail))

    @property
    def errors(self) -> list[Check]:
        return [c for c in self.checks if c.level == "error"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors


def check_write_access(directory: Path) -> tuple[bool, str]:
    """真写一个临时文件来确认有写权限（不猜）。"""
    probe = directory / f".dlssgtool_write_test{TMP_SUFFIX}"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
        return True, ""
    except Exception as exc:
        try:
            probe.unlink()
        except Exception:
            pass
        return False, f"{type(exc).__name__}: {exc}"


def preflight(
    target_dir: Path,
    exe_path: Path | None,
    proxy: str,
    router: str,
    env=None,
    game_name: str = "",
    running_names: list[str] | None = None,
    anticheat=None,
) -> Preflight:
    """安装前把所有风险点查一遍，一条都不放过。"""
    pf = Preflight()
    target_dir = Path(target_dir)

    # 1. 目录
    if not target_dir.is_dir():
        pf.add("error", "目标目录不存在", str(target_dir))
        return pf

    # 2. 写权限
    writable, why = check_write_access(target_dir)
    if writable:
        pf.add("ok", "目录可写", str(target_dir))
    else:
        pf.add(
            "error",
            "没有写入权限",
            f"{target_dir}\n{why}\n该目录可能位于 Program Files 等受保护位置，请用管理员身份重跑本工具。",
        )

    # 3. 磁盘空间（留 200MB 余量）
    try:
        free = shutil.disk_usage(str(target_dir)).free
        need = 200 * 1024 * 1024
        if free < need:
            pf.add("error", "磁盘空间不足", f"剩余 {free/1048576:.0f} MB，建议至少留 200 MB")
        else:
            pf.add("ok", "磁盘空间充足", f"剩余 {free/1073741824:.1f} GB")
    except Exception:
        pass

    # 4. 显卡与路由
    if router == "SM86":
        pf.add("ok", "计算路由 SM86", "对应 RTX 30 系列 (Ampere)")
    elif router == "SM75":
        pf.add("ok", "计算路由 SM75", "对应 RTX 20 系列 (Turing)")
    else:
        pf.add("error", "计算路由无法确定", f"检测到的 Router = {router!r}")

    if env is not None:
        g = getattr(env, "primary", None)
        if g is None:
            pf.add("error", "未检测到 NVIDIA 显卡", "本 Mod 依赖 NVIDIA 驱动提供的 NGX/NVAPI/CUDA 接口")
        else:
            if g.verdict == VERDICT_OK:
                pf.add("ok", "显卡受支持", f"{g.name}（{g.family}），驱动 {g.driver or '未知'}")
            elif g.verdict == VERDICT_NOT_NEEDED:
                pf.add("warn", "这台机器的显卡原生支持帧生成", f"{g.name}：{g.reason}")
            else:
                pf.add("error", "显卡不受支持", f"{g.name}：{g.reason}")
            if not g.driver:
                pf.add("warn", "读不到驱动版本", "请确认已安装较新的 NVIDIA 官方驱动")

        # 硬件加速 GPU 计划：帧生成的硬性系统前置条件
        hags = getattr(env, "hags", None)
        if hags is True:
            pf.add("ok", "硬件加速 GPU 计划已开启", "DLSS 帧生成的前置条件满足")
        elif hags is False:
            pf.add(
                "warn",
                "硬件加速 GPU 计划（HAGS）已关闭",
                "DLSS 帧生成硬性依赖这一项。不开启的话，即使 Mod 装好、"
                "游戏也不会提供帧生成选项，并会提示「您的显卡不支持 DLSS 帧生成技术」。\n"
                f"开启方法：{winenv.HAGS_GUI_PATH}（改完需重启电脑）\n"
                "本工具的环境面板里有「一键开启」按钮。",
            )
        else:
            pf.add("warn", "读不到硬件加速 GPU 计划状态", "如果游戏提示显卡不支持帧生成，请优先检查这一项")

    # 5. 目标 EXE
    if exe_path is not None:
        p = Path(exe_path)
        if p.is_file():
            pf.add("ok", "已定位游戏主程序", str(p))
        else:
            pf.add("error", "游戏主程序不存在", str(p))

    # 6. 游戏是否在运行
    #    除了目标 EXE，还要看同目录其他 EXE 和调用方额外给的候选名。
    cand_names = set()
    if exe_path is not None:
        cand_names.add(Path(exe_path).name)
    try:
        cand_names.update(p.name for p in Path(target_dir).glob("*.exe"))
    except OSError:
        pass
    cand_names.update(running_names or [])
    hit = sorted(n for n in cand_names if n and proc.is_running(n))
    if hit:
        pf.add(
            "error",
            "游戏/相关进程正在运行",
            "请完全退出游戏后再安装：" + "、".join(hit),
        )
    else:
        pf.add("ok", "游戏未在运行")

    # 7. 反作弊
    if anticheat is not None and getattr(anticheat, "risky", False):
        pf.add(
            "error",
            "检测到反作弊组件",
            f"{anticheat.summary}\n给带反作弊的游戏注入 DLL 可能导致封号，本工具默认阻止。",
        )
    elif anticheat is not None:
        pf.add("ok", "未检测到反作弊组件")

    # 8. 代理入口冲突
    dll_name = proxy if proxy.lower().endswith(".dll") else proxy + ".dll"
    dest = target_dir / dll_name
    ini = target_dir / INI_NAME
    prev = find_install(target_dir)
    ours = bool(prev and prev.get("proxy") == dll_name)
    if dest.exists():
        if ours:
            pf.add("ok", "该入口是本工具此前安装的", f"{dll_name}（会先备份再覆盖）")
        else:
            pf.add(
                "warn",
                "目标文件名已被占用",
                f"{dll_name} 已存在，可能来自其他 Mod。\n本工具会先把它备份到工具目录，再放入自己的文件，卸载时可完整还原。",
            )
    else:
        pf.add("ok", "代理入口可用", dll_name)

    if ini.exists() and not ours:
        pf.add("warn", "已存在 dlssg_sm86.ini", "会先备份再覆盖（可能是你手改过的配置）")

    # 9. payload 完整性
    pl = resolve_payload(dll_name)
    if pl.ok:
        pf.add("ok", "内置 DLL 完整性校验通过", f"{dll_name}  SHA256 {pl.sha256[:16]}…")
    else:
        pf.add("error", "内置 DLL 校验失败", pl.message)

    # 10. 目录里是否有别的代理冲突（同一游戏装多个代理会互相打架）
    others = [p for p in PROXY_ORDER if p != dll_name and (target_dir / p).exists()]
    if others:
        pf.add(
            "warn",
            "同目录还有其他代理 DLL",
            "如果它们也是帧生成类代理，可能与本 Mod 冲突：" + "、".join(others),
        )

    return pf


# --------------------------------------------------------------------------
# 计划（dry-run）
# --------------------------------------------------------------------------

@dataclass
class PlanItem:
    action: str
    name: str
    src: Path | None
    dst: Path
    note: str = ""


@dataclass
class InstallPlan:
    target_dir: Path
    exe_path: Path | None
    proxy: str
    router: str
    items: list[PlanItem] = field(default_factory=list)
    ini_text: str = ""
    game_name: str = ""
    cleanup_proxy: str = ""

    @property
    def writes(self) -> list[PlanItem]:
        return [i for i in self.items if i.action in (ACT_COPY, ACT_REMOVE)]


def auto_pick_proxy(target_dir: Path) -> tuple[str, str]:
    """挑一个没被占用的代理入口。返回 (proxy, 说明)。"""
    target_dir = Path(target_dir)
    prev = find_install(target_dir)
    if prev and prev.get("proxy"):
        # 继续用之前那个，避免留下孤儿文件
        return prev["proxy"], f"沿用上次安装的入口 {prev['proxy']}"
    for p in PROXY_ORDER:
        if not (target_dir / p).exists():
            if p == DEFAULT_PROXY:
                return p, "使用上游默认入口 version.dll"
            return p, f"version.dll 已被占用，改用备用入口 {p}"
    return DEFAULT_PROXY, "所有入口名都被占用，将覆盖默认入口 version.dll（会先备份）"


def make_plan(
    target_dir: Path,
    exe_path: Path | None,
    proxy: str,
    router: str,
    game_name: str = "",
    hardware_bilinear: int = 0,
    max_generated_frames: int = 3,
    log_level: int = 1,
) -> InstallPlan:
    target_dir = Path(target_dir)
    dll_name = proxy if proxy.lower().endswith(".dll") else proxy + ".dll"
    ini_text = build_ini(router, hardware_bilinear, max_generated_frames, log_level)
    plan = InstallPlan(
        target_dir=target_dir,
        exe_path=exe_path,
        proxy=dll_name,
        router=router,
        ini_text=ini_text,
        game_name=game_name,
    )

    # 若之前用的是别的代理入口，顺手清理掉旧的那个（会先备份）
    prev = find_install(target_dir)
    if prev and prev.get("proxy") and prev["proxy"] != dll_name:
        old = target_dir / prev["proxy"]
        if old.exists():
            plan.cleanup_proxy = prev["proxy"]
            plan.items.append(
                PlanItem(ACT_REMOVE, prev["proxy"], None, old, "移除本工具上次安装的旧入口")
            )

    pl = resolve_payload(dll_name)
    dst_dll = target_dir / dll_name
    if dst_dll.exists() and pl.ok and same_content(dst_dll, pl.path):
        plan.items.append(PlanItem(ACT_SKIP, dll_name, pl.path, dst_dll, "内容已一致，无需写入"))
    else:
        note = "备份后覆盖" if dst_dll.exists() else "新建"
        plan.items.append(PlanItem(ACT_COPY, dll_name, pl.path, dst_dll, note))

    dst_ini = target_dir / INI_NAME
    if dst_ini.exists() and dst_ini.read_text("utf-8", "ignore") == ini_text:
        plan.items.append(PlanItem(ACT_SKIP, INI_NAME, None, dst_ini, "内容已一致，无需写入"))
    else:
        note = "备份后覆盖" if dst_ini.exists() else "新建"
        plan.items.append(PlanItem(ACT_COPY, INI_NAME, None, dst_ini, note))

    return plan


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------

@dataclass
class InstallResult:
    success: bool
    message: str = ""
    backup_dir: Path | None = None
    installed: list[str] = field(default_factory=list)
    rolled_back: bool = False
    errors: list[str] = field(default_factory=list)


def _backup_dir_for(target_dir: Path) -> Path:
    """每次安装一个独立的备份目录（时间戳 + 短随机后缀），避免互相覆盖。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    uniq = hashlib.sha256(f"{target_dir}{time.time()}{os.getpid()}".encode()).hexdigest()[:6]
    d = backups_root() / sanitize_key(target_dir) / f"{stamp}-{uniq}"
    d.mkdir(parents=True, exist_ok=False)
    return d


def clean_stale_temps(target_dir: Path) -> None:
    """清理上次异常退出留下的临时文件。"""
    try:
        for p in Path(target_dir).glob(f"*{TMP_SUFFIX}"):
            try:
                p.unlink()
            except OSError:
                pass
    except Exception:
        pass


def execute_plan(plan: InstallPlan, dry_run: bool = False) -> InstallResult:
    """执行安装计划。任何一步失败 → 完整回滚，游戏目录回到原样。

    备份语义（重要）：
      只在一个文件"第一次"被本工具覆盖时记录它的原始内容，之后重复安装
      不再覆盖这份原始备份。这样卸载时还原的一定是**安装本工具之前**的样子，
      而不是中间某一版本工具自己的文件。
    """
    target_dir = Path(plan.target_dir)
    res = InstallResult(success=False)

    if dry_run:
        res.success = True
        res.message = "预演完成，未做任何改动"
        return res

    if not target_dir.is_dir():
        res.message = f"目标目录不存在：{target_dir}"
        return res

    clean_stale_temps(target_dir)

    # 继承上一轮记录的"原始文件"备份，避免链式覆盖
    prev = find_install(target_dir)
    originals: dict[str, str] = dict((prev or {}).get("originals") or {})

    backup_dir = _backup_dir_for(target_dir)
    res.backup_dir = backup_dir

    undo: list[tuple[str, Path, Path | None]] = []  # (kind, path, backup)

    def do_backup(src: Path) -> Path:
        dst = backup_dir / src.name
        if dst.exists():  # 同一轮里同名文件只备份一次
            return dst
        shutil.copy2(src, dst)
        if not same_content(src, dst):
            raise RuntimeError(f"备份校验失败：{src}")
        return dst

    def rollback() -> None:
        log("开始回滚", "warn")
        for kind, path, backup in reversed(undo):
            try:
                if kind == "created":
                    if path.exists():
                        path.unlink()
                elif kind in ("replaced", "removed"):
                    if backup and backup.exists():
                        shutil.copy2(backup, path)
            except Exception as exc:
                res.errors.append(f"回滚 {path} 失败：{exc}")
                log(f"回滚 {path} 失败: {exc}", "error")
        res.rolled_back = True

    try:
        installed: list[str] = []

        # 1) 先清理旧入口
        if plan.cleanup_proxy:
            old = target_dir / plan.cleanup_proxy
            if old.exists():
                if plan.cleanup_proxy not in originals:
                    originals[plan.cleanup_proxy] = str(do_backup(old))
                b = Path(originals[plan.cleanup_proxy])
                undo.append(("removed", old, b))
                old.unlink()
                journal("cleanup_old_proxy", path=str(old), backup=str(b))

        # 2) 逐个文件处理
        for item in plan.items:
            if item.action in (ACT_REMOVE, ACT_SKIP):
                if item.action == ACT_SKIP:
                    installed.append(item.name)
                continue

            dst: Path = item.dst
            existed = dst.exists()

            # 写入前再次复核 payload 完整性（防止计划生成后源文件被换掉）
            if item.src is not None:
                base = MANIFEST.get(item.name)
                if base is None:
                    raise RuntimeError(f"{item.name} 不在完整性基线内，拒绝安装")
                got_hash = sha256_file(item.src)
                if got_hash != base[0]:
                    raise RuntimeError(
                        f"{item.name} 源文件完整性校验失败（{got_hash[:12]}… != {base[0][:12]}…），已中止"
                    )

            # 只记录第一次被覆盖时的原始内容
            if existed and item.name not in originals:
                originals[item.name] = str(do_backup(dst))

            tmp = dst.with_name(dst.name + TMP_SUFFIX)
            if item.src is not None:
                shutil.copy2(item.src, tmp)
                expect_hash = sha256_file(item.src)
            else:
                tmp.write_text(plan.ini_text, encoding="utf-8", newline="\n")
                expect_hash = hashlib.sha256(plan.ini_text.encode("utf-8")).hexdigest()

            got = sha256_file(tmp)
            if got != expect_hash:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"{item.name} 写入校验失败（{got[:12]}… != {expect_hash[:12]}…）")

            # 原子替换：不会留下写了一半的文件
            os.replace(tmp, dst)

            if sha256_file(dst) != expect_hash:
                raise RuntimeError(f"{item.name} 落盘后校验失败")

            undo.append(("replaced" if existed else "created", dst, Path(originals[item.name]) if item.name in originals else None))
            installed.append(item.name)
            journal(
                "file_installed",
                path=str(dst),
                sha256=expect_hash,
                existed_before=existed,
                original_backup=originals.get(item.name, ""),
            )

        res.installed = installed

        # 3) 记录状态
        rec = {
            "game_name": plan.game_name,
            "target_dir": str(target_dir),
            "exe": str(plan.exe_path) if plan.exe_path else "",
            "proxy": plan.proxy,
            "router": plan.router,
            "files": [
                {
                    "name": n,
                    "sha256": sha256_file(target_dir / n) if (target_dir / n).is_file() else "",
                }
                for n in installed
            ],
            "originals": originals,
            "backup_dir": str(backup_dir),
            "payload_version": PAYLOAD_VERSION,
            "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        record_install(rec)

        res.success = True
        res.message = f"已安装 {len(installed)} 个文件到 {target_dir}"
        journal("install_ok", target_dir=str(target_dir), proxy=plan.proxy, router=plan.router)
        return res

    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        log(f"安装失败，回滚：{msg}", "error")
        res.errors.append(msg)
        rollback()
        res.message = f"安装失败，已回滚：{msg}"
        journal("install_failed", target_dir=str(target_dir), error=msg)
        # 回滚成功的话备份目录已无意义
        try:
            if backup_dir.is_dir() and not any(backup_dir.iterdir()):
                backup_dir.rmdir()
                res.backup_dir = None
        except OSError:
            pass
        return res


# --------------------------------------------------------------------------
# 体检
# --------------------------------------------------------------------------

def detect_ours(target_dir: str | Path) -> dict[str, str]:
    """不依赖状态记录，按内置 payload 的 SHA256 识别目录里哪些文件是本工具放的。

    用于状态文件丢失 / 被清理后的恢复，也能识别"孤儿安装"。
    """
    target_dir = Path(target_dir)
    found: dict[str, str] = {}
    for name, (h, size) in MANIFEST.items():
        p = target_dir / name
        try:
            if p.is_file() and p.stat().st_size == size and sha256_file(p) == h:
                found[name] = h
        except OSError:
            continue
    # 只有确认 DLL 是我们的，才连带认下同目录的 INI
    if found and (target_dir / INI_NAME).is_file():
        found[INI_NAME] = ""
    return found


def _running_candidates(target_dir: Path, extra: str = "") -> set[str]:
    names: set[str] = set()
    if extra:
        names.add(Path(extra).name)
    try:
        names.update(p.name for p in Path(target_dir).glob("*.exe"))
    except OSError:
        pass
    return names


@dataclass
class VerifyResult:
    installed: bool
    healthy: bool
    details: list[Check] = field(default_factory=list)
    record: dict | None = None


def verify(target_dir: str | Path) -> VerifyResult:
    """体检：确认文件都在、哈希都对、INI 与当前环境匹配。"""
    target_dir = Path(target_dir)
    out: list[Check] = []
    rec = find_install(target_dir)
    orphan = False
    if not rec:
        detected = detect_ours(target_dir)
        if not detected:
            return VerifyResult(
                False, False, [Check("warn", "没有安装记录", "该目录不是本工具安装的")], None
            )
        orphan = True
        rec = {
            "files": [{"name": n, "sha256": h} for n, h in detected.items()],
            "originals": {},
            "router": "",
            "proxy": next((n for n in detected if n.lower().endswith(".dll")), ""),
        }
        out.append(
            Check("warn", "检测到本工具安装的文件，但没有安装记录",
                  "可能状态文件被清理过。仍可正常卸载（会按内置哈希识别）。")
        )

    healthy = True
    for f in rec.get("files", []):
        p = target_dir / f["name"]
        if not p.is_file():
            out.append(Check("error", f"{f['name']} 缺失", "文件不存在，可能被删除或还原"))
            healthy = False
            continue
        digest = try_hash(p)
        if not digest:
            out.append(Check("error", f"{f['name']} 无法读取",
                             "文件可能正被游戏占用，请退出游戏后重新体检"))
            healthy = False
            continue
        exp = f.get("sha256") or ""
        if exp and digest != exp:
            out.append(Check("error", f"{f['name']} 哈希不符", f"当前 {digest[:16]}… 期望 {exp[:16]}…"))
            healthy = False
        elif f["name"].lower().endswith(".dll"):
            base = MANIFEST.get(f["name"])
            if base and digest != base[0]:
                out.append(Check("warn", f"{f['name']} 已非原始文件", "可能被更新或被其他程序替换"))
            else:
                out.append(Check("ok", f"{f['name']} 完好", f"SHA256 {digest[:16]}…"))
        else:
            out.append(Check("ok", f"{f['name']} 完好", ""))

    ini = target_dir / INI_NAME
    if ini.is_file():
        r = read_ini_router(ini)
        want = rec.get("router", "")
        if want and r and r != want:
            out.append(Check("warn", "INI 路由与安装记录不一致", f"文件里是 {r}，记录是 {want}"))
        else:
            out.append(Check("ok", "INI 路由正确", f"Router={r}" if r else ""))

    # 运行状态提示：文件被占用时卸载会失败
    running = sorted(n for n in _running_candidates(target_dir, rec.get("exe", "")) if proc.is_running(n))
    if running:
        out.append(Check("warn", "游戏正在运行", "请退出游戏后再卸载：" + "、".join(running)))

    return VerifyResult(True, healthy, out, rec)


# --------------------------------------------------------------------------
# 卸载
# --------------------------------------------------------------------------

@dataclass
class UninstallResult:
    success: bool
    message: str = ""
    removed: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)


def uninstall(target_dir: str | Path, force: bool = False) -> UninstallResult:
    """卸载：删掉本工具放的文件，并把**安装前的原始文件**还原回去。

    force=False 时：
      · 游戏在运行 → 直接拒绝（文件被占用，删一半最危险）
      · 只删除哈希与安装记录一致的文件 —— 用户后来自己换过的不动
    还原只按记录里显式的 originals 映射做，绝不会把备份目录里的东西
    一股脑倒回游戏目录。

    删除后会**逐个复核**；只要还有文件没删掉，就报失败并保留记录，
    绝不在留下残留的情况下谎报成功。
    """
    target_dir = Path(target_dir)
    res = UninstallResult(success=False)
    rec = find_install(target_dir)
    synthesized = False

    if not rec:
        detected = detect_ours(target_dir)
        if not detected:
            res.message = "没有该目录的安装记录，未做任何改动"
            return res
        synthesized = True
        rec = {
            "files": [{"name": n, "sha256": h} for n, h in detected.items()],
            "originals": {},
            "router": "",
            "proxy": next((n for n in detected if n.lower().endswith(".dll")), ""),
        }
        res.message = "状态记录已丢失，已按内置 DLL 哈希识别出本工具安装的文件；"

    recorded = {f["name"]: f.get("sha256", "") for f in rec.get("files", [])}
    originals: dict[str, str] = rec.get("originals") or {}

    # 0) 游戏在运行时不要动文件：删一半会让游戏加载到残缺代理
    running = sorted(n for n in _running_candidates(target_dir, rec.get("exe", "")) if proc.is_running(n))
    if running and not force:
        res.message = (
            "游戏/相关进程正在运行，已中止卸载：" + "、".join(running)
            + "。\n请完全退出游戏后重试 —— 运行中删除文件可能让游戏加载到残缺的代理 DLL。"
        )
        return res

    # 1) 删除我们放进去的文件（原文件会被后面的还原步骤补回来）
    for name, exp in recorded.items():
        p = target_dir / name
        if not p.is_file():
            continue
        digest = try_hash(p)
        if not digest:
            res.message += f"{name} 无法读取（很可能被游戏占用），未能删除；"
            log(f"读取 {p} 失败，跳过删除", "warn")
            continue
        if exp and digest != exp and not force and name not in originals:
            res.message += f"{name} 已被改动，为安全起见未删除；"
            log(f"跳过删除 {p}：哈希与记录不符", "warn")
            continue
        if exp and digest != exp and name in originals:
            log(f"{p} 内容已被改动，将由原始备份还原", "warn")
        try:
            p.unlink()
            res.removed.append(name)
            journal("file_removed", path=str(p))
        except Exception as exc:
            res.message += f"删除 {name} 失败：{exc}；"
            log(f"删除 {p} 失败: {exc}", "error")

    # 2) 逐个复核，确认真删掉了 —— 没删掉就不许报成功
    remaining = sorted(n for n in recorded if (target_dir / n).is_file())
    if remaining:
        res.success = False
        res.message += (
            f" 有 {len(remaining)} 个文件仍未能删除：{'、'.join(remaining)}。"
            "通常是游戏正在运行占用了文件，请完全退出游戏后再次卸载。"
        )
        if not synthesized:
            # 记录保留，下次还能继续卸载
            pass
        journal("uninstall_incomplete", target_dir=str(target_dir), remaining=remaining)
        return res

    # 3) 还原"第一次覆盖前"记录的原始文件
    for name, raw in originals.items():
        b = Path(raw)
        if not b.is_file():
            res.message += f"原始备份已丢失：{name}；"
            continue
        dst = target_dir / name
        try:
            shutil.copy2(b, dst)
            res.restored.append(name)
            journal("file_restored", path=str(dst), source=str(b))
        except Exception as exc:
            res.message += f"还原 {name} 失败：{exc}；"

    forget_install(target_dir)
    res.success = True
    res.message = (res.message or "卸载完成") + (
        f" 删除 {len(res.removed)} 个文件"
        + (f"，还原 {len(res.restored)} 个：{'、'.join(res.restored)}" if res.restored else "")
    )

    # 4) 顺手清掉 Mod 运行时自己产生的日志目录（纯运行产物，不含用户数据）
    logdir = target_dir / "dlssg_sm86"
    if logdir.is_dir():
        try:
            leftovers = {p.name.lower() for p in logdir.iterdir()}
            if leftovers <= {"logs"}:
                shutil.rmtree(logdir)
                res.message += "，并清除了 Mod 运行日志目录 dlssg_sm86\\"
                journal("mod_logs_removed", path=str(logdir))
            else:
                res.message += f"，但 {logdir} 里还有非日志内容，已保留"
        except Exception as exc:
            log(f"清理 {logdir} 失败: {exc}", "warn")

    journal("uninstall_ok", target_dir=str(target_dir))
    return res


def restore_backup(backup_dir: str | Path, target_dir: str | Path) -> UninstallResult:
    """从指定备份目录还原到目标目录。"""
    backup_dir = Path(backup_dir)
    target_dir = Path(target_dir)
    res = UninstallResult(success=False)
    if not backup_dir.is_dir():
        res.message = f"备份目录不存在：{backup_dir}"
        return res
    for b in sorted(backup_dir.iterdir()):
        if not b.is_file():
            continue
        dst = target_dir / b.name
        try:
            shutil.copy2(b, dst)
            res.restored.append(b.name)
        except Exception as exc:
            res.message += f"还原 {b.name} 失败：{exc}；"
    res.success = True
    res.message = res.message or f"已还原 {len(res.restored)} 个文件"
    return res


__all__ = [
    "sha256_file",
    "resolve_payload",
    "available_payloads",
    "build_ini",
    "read_ini_router",
    "preflight",
    "make_plan",
    "execute_plan",
    "auto_pick_proxy",
    "verify",
    "detect_ours",
    "uninstall",
    "restore_backup",
    "find_install",
    "all_installs",
    "load_state",
    "Check",
    "Preflight",
    "InstallPlan",
    "PlanItem",
    "InstallResult",
    "VerifyResult",
    "UninstallResult",
    "PROXY_ORDER",
]
