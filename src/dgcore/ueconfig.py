"""虚幻引擎配置（Engine.ini）读写 —— 让 XeSS 帧生成真正能工作。

为什么需要动这个文件
--------------------
OptiScaler 官方的 XeFG 说明里明确写着：

  对虚幻引擎游戏，用 Upscaler 输入配 DLSS 时，需要**关闭 dilated motion vectors**
      [SystemSettings]
      r.NGX.DLSS.DilateMotionVectors=0

不设这一条，XeFG 每一帧都会失败：

  [E] XeFG Log: XeFG: Invalid argument.
      motion vector and depth resource resolutions must match.

表现是"装上了、菜单里显示 4X、但帧数一点没变" —— 非常容易误判成
"这游戏不支持"。黑神话和幻兽帕鲁都栽在这里。

为什么单独一个模块
------------------
Engine.ini 可能在两个地方，而且可能不在游戏目录里：

  <游戏>\\<项目名>\\Saved\\Config\\Windows\\Engine.ini
  %LOCALAPPDATA%\\<项目名>\\Saved\\Config\\Windows\\Engine.ini

（帕鲁就是第二种 —— 游戏目录下压根没有 Saved 文件夹。）

所以它没法套用"只写游戏目录"的那套逻辑，得单独定位、单独记账。

安全语义不变：改之前备份到工具目录，只认自己加的那一行，
卸载时精确删掉那一行（而不是整文件还原，避免把用户后来的改动也冲掉）。
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .paths import backups_root, journal, log

# 我们要加的 cvar
CVAR_SECTION = "SystemSettings"
CVAR_KEY = "r.NGX.DLSS.DilateMotionVectors"
CVAR_VALUE = "0"

# 我们写在 Engine.ini 里的标记，用于识别"这行是我们加的"
MARK_LINE = "; added by DLSSG frame-gen tool"


@dataclass
class UeConfigResult:
    ok: bool
    path: Path | None = None
    changed: bool = False
    detail: str = ""
    backup: Path | None = None
    created: bool = False        # 这个 Engine.ini 是不是我们新建的
    edit: CvarEdit | None = None  # 还原所需信息（节是否存在、键原值、换行风格）


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def _try_hash(path: str | Path) -> str:
    try:
        return sha256_file(path)
    except OSError:
        return ""


# --------------------------------------------------------------------------
# 定位
# --------------------------------------------------------------------------

def project_root(target_dir: str | Path) -> Path | None:
    """从 <项目>\\Binaries\\Win64 往上三层拿到项目根目录。"""
    p = Path(target_dir).resolve()
    if p.name.lower() == "win64" and p.parent.name.lower() == "binaries":
        return p.parent.parent
    # 容错：往上找带 Binaries 的那层
    cur = p
    for _ in range(4):
        if (cur / "Binaries").is_dir():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def find_engine_ini(target_dir: str | Path) -> tuple[Path | None, str]:
    """找到该游戏的 Engine.ini。返回 (路径, 说明)。

    只看**已经存在**的 Config 目录，不凭空造 —— 造错地方等于白改。
    """
    root = project_root(target_dir)
    if root is None:
        return None, "无法从主程序目录推断虚幻项目根目录"

    proj = root.name
    rel = Path("Saved") / "Config" / "Windows" / "Engine.ini"

    # 1) 游戏目录内（黑神话基准测试走这条）
    local = root / rel
    if local.is_file():
        return local, f"游戏目录内：{local}"

    # 2) %LOCALAPPDATA%\<项目名>\（帕鲁走这条）
    lad = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if lad:
        alt = Path(lad) / proj / rel
        if alt.is_file():
            return alt, f"用户配置目录：{alt}"

    # 3) 都没有：如果 Saved\Config\Windows 目录存在，就在那儿新建
    for base in ([root] + ([Path(lad) / proj] if lad else [])):
        cfgdir = base / "Saved" / "Config" / "Windows"
        if cfgdir.is_dir():
            return cfgdir / "Engine.ini", f"配置目录已存在，将在其中新建：{cfgdir / 'Engine.ini'}"

    return None, (f"没找到 Engine.ini（游戏内 {local} 和用户目录下都没有，"
                  f"且 Saved\\Config\\Windows 目录不存在）")


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------

def read_text(path: Path) -> str:
    """按字节读再解码。

    不能用 Path.read_text —— 它走通用换行，会把 CRLF 悄悄转成 LF。
    那样 _eol() 就检测不出真实的换行风格，写回去时会把整个文件
    从 CRLF 变成 LF，对用户的配置文件来说这是不该发生的改动。
    """
    try:
        return path.read_bytes().decode("utf-8", "ignore")
    except OSError:
        return ""


def has_cvar(text: str) -> bool:
    """是否已经设了我们要的那个 cvar（且值正确）。"""
    cur = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].strip()
            continue
        if cur.lower() != CVAR_SECTION.lower() or s.startswith(";") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        if k.strip().lower() == CVAR_KEY.lower():
            return v.strip() == CVAR_VALUE
    return False


def _scan(text: str) -> tuple[bool, str | None]:
    """看 [SystemSettings] 节是否存在，以及它里面该键原来的值。"""
    cur = ""
    sec = False
    prev: str | None = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].strip()
            if cur.lower() == CVAR_SECTION.lower():
                sec = True
            continue
        if cur.lower() != CVAR_SECTION.lower() or s.startswith(";") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        if k.strip().lower() == CVAR_KEY.lower():
            prev = v.strip()
    return sec, prev


def _eol(text: str) -> str:
    """原文件用的换行风格 —— 改完要按原样写回去。"""
    return "\r\n" if "\r\n" in text else "\n"


@dataclass
class CvarEdit:
    """记录这次改动前 Engine.ini 长什么样，供精确还原用。"""
    section_existed: bool = False
    prev_value: str | None = None      # 该键原本的值；None = 原本没有这个键
    eol: str = "\n"
    trailing_eol: bool = True          # 原文件是否以换行结尾

    @property
    def we_added_section(self) -> bool:
        return not self.section_existed

    def to_dict(self) -> dict:
        """存进状态记录用（JSON 可序列化）。"""
        return {"section_existed": self.section_existed,
                "prev_value": self.prev_value,
                "eol": self.eol,
                "trailing_eol": self.trailing_eol}

    @classmethod
    def from_dict(cls, d: dict | None) -> "CvarEdit | None":
        if not isinstance(d, dict):
            return None
        return cls(
            section_existed=bool(d.get("section_existed", False)),
            prev_value=d.get("prev_value", None),
            eol="\r\n" if d.get("eol") == "\r\n" else "\n",
            trailing_eol=bool(d.get("trailing_eol", True)),
        )


def apply_cvar(text: str) -> tuple[str, CvarEdit]:
    """在 [SystemSettings] 里写入 cvar。

    返回 (新内容, 还原所需的信息)。只动这一个键，其余内容原样保留。
    """
    sec, prev = _scan(text)
    eol = _eol(text)
    edit = CvarEdit(section_existed=sec, prev_value=prev, eol=eol,
                    trailing_eol=text.endswith(eol) if text else False)

    lines = text.splitlines()
    out: list[str] = []
    cur = ""
    key_line = -1
    sec_end = -1
    has_sec = False

    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if cur.lower() == CVAR_SECTION.lower() and sec_end < 0:
                sec_end = len(out)
            cur = s[1:-1].strip()
            if cur.lower() == CVAR_SECTION.lower():
                has_sec = True
            out.append(line)
            continue
        if cur.lower() == CVAR_SECTION.lower():
            m = re.match(r"^\s*([A-Za-z0-9_.]+)\s*=", line)
            if m and m.group(1).lower() == CVAR_KEY.lower():
                key_line = len(out)
        out.append(line)

    if not has_sec:
        # 插在**结尾空行之前**，撤销时才能精确还原、不吃掉文件原本的收尾空行
        at = len(out)
        while at > 0 and not out[at - 1].strip():
            at -= 1
        if at > 0:
            out.insert(at, "")
            at += 1
        out[at:at] = [f"[{CVAR_SECTION}]", MARK_LINE, f"{CVAR_KEY}={CVAR_VALUE}"]
    elif key_line >= 0:
        out[key_line] = f"{CVAR_KEY}={CVAR_VALUE}"
    else:
        at = sec_end if sec_end > 0 else len(out)
        while at > 0 and not out[at - 1].strip():
            at -= 1
        out[at:at] = [MARK_LINE, f"{CVAR_KEY}={CVAR_VALUE}"]

    result = edit.eol.join(out) + edit.eol
    if not edit.trailing_eol and result.endswith(edit.eol):
        result = result[: -len(edit.eol)]
    return result, edit


def strip_cvar(text: str, edit: CvarEdit | None = None) -> tuple[str, bool]:
    """把我们对 Engine.ini 的改动撤销掉。

    还原策略按"当初到底改了什么"分四种，才能做到逐字节还原：

      · 原本有这把键、值是别的   → 把值改回去（不能删，那是用户自己设的）
      · 原本有 [SystemSettings] 节 → 只删键和标记行，节头留着
      · 原本没有这个节           → 连节头一起删
      · 文件是本工具新建的       → 调用方直接删文件（不走这里）

    最后那条最关键：早先的版本一律删节头，会把用户自己设的
    `r.NGX.DLSS.DilateMotionVectors=1` 连同整节一起抹掉。
    """
    if edit is None:
        # 没有还原信息时退化处理：至少别留下键
        edit = CvarEdit(section_existed=True, prev_value=None, eol=_eol(text))

    eol = edit.eol
    if CVAR_KEY not in text and MARK_LINE not in text:
        return text, False

    def finish(rows: list[str]) -> str:
        s = eol.join(rows) + eol
        if not edit.trailing_eol and s.endswith(eol):
            s = s[: -len(eol)]
        return s

    # 情形一：键本来就有别的值 —— 改回去即可
    if edit.prev_value is not None and edit.prev_value != CVAR_VALUE:
        lines = text.splitlines()
        cur = ""
        changed = False
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                cur = s[1:-1].strip()
                continue
            if cur.lower() != CVAR_SECTION.lower():
                continue
            m = re.match(r"^\s*([A-Za-z0-9_.]+)\s*=", line)
            if m and m.group(1).lower() == CVAR_KEY.lower():
                lines[i] = f"{CVAR_KEY}={edit.prev_value}"
                changed = True
        return finish(lines), changed

    lines = text.splitlines()
    out: list[str] = []
    cur = ""
    changed = False

    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].strip()
            out.append(line)
            continue
        if cur.lower() == CVAR_SECTION.lower():
            if s == MARK_LINE:
                changed = True
                continue
            m = re.match(r"^\s*([A-Za-z0-9_.]+)\s*=", line)
            if m and m.group(1).lower() == CVAR_KEY.lower():
                changed = True
                continue
        out.append(line)

    if not changed:
        return text, False

    # 只有"这个节本来就是空的/是我们加的"，才允许把节头一起删掉
    if edit.we_added_section:
        sec_i = -1
        for i, line in enumerate(out):
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                if s[1:-1].strip().lower() == CVAR_SECTION.lower():
                    sec_i = i
                    break
        if sec_i >= 0:
            body_has_content = False
            for line in out[sec_i + 1:]:
                s = line.strip()
                if s.startswith("[") and s.endswith("]"):
                    break
                if s and not s.startswith(";"):
                    body_has_content = True
                    break
            if not body_has_content:
                end = sec_i + 1
                while end < len(out):
                    s = out[end].strip()
                    if s.startswith("[") and s.endswith("]"):
                        break
                    end += 1
                if end >= len(out):
                    while end > sec_i + 1 and not out[end - 1].strip():
                        end -= 1
                start = sec_i
                if start > 0 and not out[start - 1].strip():
                    start -= 1
                del out[start:end]

    return finish(out), True


# --------------------------------------------------------------------------
# 备份
# --------------------------------------------------------------------------

def _backup_dir(tag: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    uniq = hashlib.sha256(f"{tag}{time.time()}{os.getpid()}".encode()).hexdigest()[:6]
    d = backups_root() / "_ueconfig" / f"{stamp}-{uniq}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_dilate_off(target_dir: str | Path) -> UeConfigResult:
    """确保游戏的 Engine.ini 里关了 dilated motion vectors。

    已经设好就什么都不做（幂等）。
    """
    path, how = find_engine_ini(target_dir)
    if path is None:
        return UeConfigResult(False, None, False, how)

    text = read_text(path)
    if has_cvar(text):
        return UeConfigResult(True, path, False, f"已经是关闭状态（{how}）")

    existed = path.is_file()
    res = UeConfigResult(True, path, True, how, created=not existed)

    try:
        if existed:
            bdir = _backup_dir(path.name)
            b = bdir / f"{path.name}.orig"
            b.write_bytes(path.read_bytes())
            if _try_hash(b) != _try_hash(path):
                return UeConfigResult(False, path, False, "备份校验失败，未做改动")
            res.backup = b
        else:
            path.parent.mkdir(parents=True, exist_ok=True)

        new, edit = apply_cvar(text)
        res.edit = edit
        tmp = path.with_name(path.name + ".dlssgtool.tmp")
        tmp.write_text(new, encoding="utf-8", newline="")
        os.replace(tmp, path)

        if not has_cvar(read_text(path)):
            return UeConfigResult(False, path, False, "写入后复核失败，改动可能未生效")

        journal("ueconfig_dilate_off", path=str(path), backup=str(res.backup or ""),
                created=not existed)
        log(f"已关闭 {CVAR_KEY}（{path}）")
        res.detail = ("新建并写入" if not existed else "已写入") + f"：{path}"
        return res

    except Exception as exc:
        return UeConfigResult(False, path, False, f"{type(exc).__name__}: {exc}")


def has_our_mark(text: str) -> bool:
    """文件里有没有我们留的标记行。

    用来在没有安装记录时判断"这一行是不是我们加的"。
    用户自己手写同样的键不会带这行注释，所以不会误伤。
    """
    return MARK_LINE in text


def _section_key_count(text: str) -> int:
    """[SystemSettings] 节里有多少个有效键（不含注释和空行）。"""
    cur = ""
    n = 0
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].strip()
            continue
        if cur.lower() != CVAR_SECTION.lower():
            continue
        if s and not s.startswith(";") and "=" in s:
            n += 1
    return n


def strip_orphan(target_dir: str | Path) -> UeConfigResult:
    """没有安装记录时，也能清掉我们留下的 cvar。

    为什么需要它：状态文件一旦被清理（实测发生过），卸载就再也找不到
    "当初改了什么"，于是 Engine.ini 里那一行会永远留在用户的游戏配置里。

    两级判据：

      1. 有我们写的标记行 → 确定是我们加的，直接清
      2. 没有标记行，但 [SystemSettings] 节里**只有这一个键** → 几乎可以
         肯定是本工具建的节，清掉

    第 2 条为什么必要：**虚幻引擎退出时会重写 Engine.ini 并丢掉注释行**，
    实测黑神话就是这么把标记行弄没的。只认标记行等于失效。

    第 2 条为什么不误伤：用户自己写的配置不会恰好是一个只含这一把键的空节 ——
    除非他照着 OptiScaler 文档手改。那种情况下删掉也无害（值本来就是我们
    要的那个），而且卸载信息里会写明改了什么。
    """
    path, how = find_engine_ini(target_dir)
    if path is None or not path.is_file():
        return UeConfigResult(True, path, False, "Engine.ini 不存在")

    text = read_text(path)
    marked = has_our_mark(text)
    if not marked:
        if not has_cvar(text):
            return UeConfigResult(True, path, False, "没有本工具留下的配置项")
        if _section_key_count(text) != 1:
            # 这一节里还有别的键 —— 大概率是用户自己的配置，不要动
            return UeConfigResult(True, path, False, "该节还有其他配置项，未做改动")

    # 有记录时按记录还原；没记录就按"当前没有这个节"来清
    edit = CvarEdit(section_existed=False, prev_value=None, eol=_eol(text),
                    trailing_eol=text.endswith(_eol(text)) if text else False)
    new, changed = strip_cvar(text, edit)
    if not changed:
        return UeConfigResult(True, path, False, "无可清理内容")

    try:
        tmp = path.with_name(path.name + ".dlssgtool.tmp")
        tmp.write_text(new, encoding="utf-8", newline="")
        os.replace(tmp, path)
        journal("ueconfig_orphan_stripped", path=str(path), marked=marked)
        how_found = "按标记行" if marked else "按空节判定"
        return UeConfigResult(True, path, True,
                              f"已清理本工具残留的配置项（{how_found}）：{path}")
    except Exception as exc:
        return UeConfigResult(False, path, False, f"{type(exc).__name__}: {exc}")


def restore_dilate(target_dir: str | Path, edit: CvarEdit | None = None,
                   created: bool = False) -> UeConfigResult:
    """撤销我们对 Engine.ini 的改动。

    created=True 表示这个文件本来不存在、是我们新建的 —— 直接删掉。
    否则按 edit 里记的信息精确还原（该改回值的改回值，该删行的才删行）。
    """
    path, how = find_engine_ini(target_dir)
    if path is None or not path.is_file():
        return UeConfigResult(True, path, False, "Engine.ini 不存在，无需还原")

    if created:
        try:
            path.unlink()
            journal("ueconfig_removed", path=str(path))
            return UeConfigResult(True, path, True, f"已删除本工具新建的 {path}")
        except OSError as exc:
            return UeConfigResult(False, path, False, f"删除失败：{exc}")

    text = read_text(path)
    new, changed = strip_cvar(text, edit)
    if not changed:
        return UeConfigResult(True, path, False, "没有本工具添加的配置项，未做改动")

    try:
        tmp = path.with_name(path.name + ".dlssgtool.tmp")
        tmp.write_text(new, encoding="utf-8", newline="")
        os.replace(tmp, path)
        journal("ueconfig_restored", path=str(path))
        return UeConfigResult(True, path, True, f"已撤销本工具对配置的改动：{path}")
    except Exception as exc:
        return UeConfigResult(False, path, False, f"{type(exc).__name__}: {exc}")


def describe(target_dir: str | Path) -> str:
    """给预检/体检用的一句话状态。"""
    path, how = find_engine_ini(target_dir)
    if path is None:
        return how
    if not path.is_file():
        return f"将新建：{path}"
    return ("已关闭 dilated motion vectors" if has_cvar(read_text(path))
            else f"尚未关闭 dilated motion vectors（{path}）")


# --------------------------------------------------------------------------
# 游戏内的超分设置
# --------------------------------------------------------------------------
#
# FGInput=upscaler 的含义是"拿超分器的输入来插帧" —— 所以**超分器必须真的在跑**。
# 虚幻引擎自带的 TSR 是引擎内部实现，OptiScaler 钩不到它。
# 游戏里要是选的 TSR（而不是 DLSS/FSR/XeSS），XeFG 就永远拿不到输入、
# 永远不激活 —— 界面上只是"没效果"，看不出任何原因。
#
# 帕鲁实测就栽在这：AntiAliasingType=AAM_TSR。

# 认得出是"外部超分库"的类型（OptiScaler 能钩）
_UPSCALER_OK = ("dlss", "fsr", "xess", "amf", "dlaa")
# 引擎内建 / 关掉的
_UPSCALER_INTERNAL = ("tsr", "taa", "none", "off", "fxaa")


def find_game_user_settings(target_dir: str | Path) -> Path | None:
    """找 GameUserSettings.ini（游戏里的画质设置存在这儿）。"""
    ini, _how = find_engine_ini(target_dir)
    if ini is not None:
        p = ini.parent / "GameUserSettings.ini"
        if p.is_file():
            return p
    # 兜底：项目目录里翻一下
    root = project_root(target_dir)
    if root is not None:
        for base in (root, root.parent):
            p = base / "Saved" / "Config" / "Windows" / "GameUserSettings.ini"
            if p.is_file():
                return p
    return None


def read_upscaler(target_dir: str | Path) -> tuple[str, str]:
    """读游戏当前使用的抗锯齿/超分类型。

    返回 (类别, 原始值)：
        "external" —— DLSS/FSR/XeSS 之类，OptiScaler 能钩
        "internal" —— TSR/TAA 之类引擎内建，钩不到（upscaler 输入会失效）
        "unknown"  —— 读不到

    全文件搜索而不是按节找：这个键的位置是**游戏自定义**的。
    帕鲁放在 `/Script/Pal.PalGameLocalSettings` 里，别的游戏可能在
    `[ScalabilitySettings]`。写死节名必然漏。
    """
    p = find_game_user_settings(target_dir)
    if p is None:
        return "unknown", ""

    text = read_text(p)
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(";") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        if k.strip().lower() != "antialiasingtype":
            continue
        val = v.strip()
        bare = val.lower().replace("aam_", "")
        if any(x in bare for x in _UPSCALER_OK):
            return "external", val
        if any(x in bare for x in _UPSCALER_INTERNAL):
            return "internal", val
        return "unknown", val

    return "unknown", ""


__all__ = [
    "CVAR_SECTION",
    "CVAR_KEY",
    "CVAR_VALUE",
    "UeConfigResult",
    "CvarEdit",
    "project_root",
    "find_engine_ini",
    "find_game_user_settings",
    "read_upscaler",
    "has_cvar",
    "has_our_mark",
    "apply_cvar",
    "strip_cvar",
    "strip_orphan",
    "ensure_dilate_off",
    "restore_dilate",
    "describe",
]
