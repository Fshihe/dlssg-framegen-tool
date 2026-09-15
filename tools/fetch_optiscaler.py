#!/usr/bin/env python
"""从本地压缩包提取 OptiScaler 帧生成 bundle，产出 payload 与完整性基线。

为什么不把这些文件放进仓库
--------------------------
和 DLSSG 部分同样的原则：仓库只放代码和哈希基线，不放第三方二进制。
这里的每一份都属于别人：

  OptiScaler 本体            GPLv3，源码公开
  libxess_fg / libxell       Intel XeSS SDK —— 允许原样再分发，但必须带许可文件，
                             且许可明确禁止逆向与"运行时修改"
  fakenvapi / dlssg_to_fsr3  GPLv3
  nvngx_dlssnr.dll           NVIDIA 未签名的预发布组件，驱动里没有这一份

本脚本只做**本地提取 + 记账**：把压缩包里的文件原样搬进 payload/，
并算出 SHA256 生成 src/dgcore/optiscaler_bundles.py。
运行时安装器按这份基线逐个校验，装进游戏目录的每个文件都可追溯、可删除。

用法：
    python tools/fetch_optiscaler.py                    # 自动找 "xess多帧生成" 目录
    python tools/fetch_optiscaler.py --src "D:\\下载\\xess多帧生成"
    python tools/fetch_optiscaler.py --only xess        # 只提取 xess bundle
    python tools/fetch_optiscaler.py --list             # 只看会提取什么，不落盘
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = ROOT / "payload"
WORK = ROOT / "work" / "optiscaler-extract"
GENERATED = ROOT / "src" / "dgcore" / "optiscaler_bundles.py"

SEVENZIP_CANDIDATES = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
    r"C:\Program Files\WinRAR\UnRAR.exe",
    "7z",
    "7za",
    "7zr",
]

# 默认去这里找压缩包（用户机器上的实际位置）
DEFAULT_SRC = ROOT.parent / "xess多帧生成"


# --------------------------------------------------------------------------
# bundle 定义：从哪个包、取哪些文件
# --------------------------------------------------------------------------
#
# allow 的两种写法：
#   "dxgi.dll"            —— 精确一个文件
#   "Optiscaler/**"       —— 整棵子树
#
# 只取白名单里的东西，压缩包里的 docx、说明、其他版本一律不碰 ——
# 这样"这个 bundle 到底装了什么"永远是一份可读的清单。

# bundle 定义：从哪个包、取哪些文件
# --------------------------------------------------------------------------
#
# 只取运行必需的，不搬整套包。判据来自 OptiScaler 自己的配置语义：
#
#   FGInput  = upscaler   → 不需要游戏自带 DLSSG，也就不需要 streamline/
#   FGOutput = xefg       → 只需要 libxess_fg + libxell（+ fakenvapi 转延迟信号）
#
# 因此 amd_fidelityfx_*（78 MB）、libxess.dll（77 MB）、dlss-enabler-headless
# （30 MB）、streamline/*（6 MB）这些"别的输出路径"的 provider 一律不要 ——
# OptiScaler 只加载配置点名的 provider，缺了别的不会影响本路径，而且是显式报错。
#
# 唯一例外是 DLSS5：那个 158 MB 的 nvngx_dlssnr.dll 就是它本体，没法省。

_COMMON = [
    "dxgi.dll",                 # 代理入口（包内已把 OptiScaler.dll 改名好）
    "OptiScaler.ini",           # 配置模板：保留全部键，我们只改必要的几项
    "fakenvapi.dll",            # XeFG 输出必需
    "fakenvapi.ini",
    "libxell.dll",              # 低延迟，MFG 必需
    "libxess_fg.dll",           # XeSS 帧生成 provider
    "D3D12_Optiscaler/**",      # Agility SDK（D3D12Core.dll）
    "Licenses/**",              # Intel 许可要求随二进制一起分发
]

BUNDLE_SOURCES: dict[str, dict] = {
    "optiscaler-xess": {
        "short": "XeSS 多帧生成",
        "display_name": "XeSS 多帧生成（OptiScaler · Coldwood 构建）",
        "keywords": ["版本2", "Coldwood"],
        "exclude": ["DLSS5"],
        "allow": list(_COMMON),
    },
    "optiscaler-dlss5": {
        "short": "DLSS 5 神经网络渲染",
        "display_name": "DLSS 5 神经网络渲染 + XeSS 多帧生成（OptiScaler）",
        "keywords": ["DLSS5"],
        "exclude": [],
        # 这个包的目录结构和版本 2 不一样：provider 不在根目录，而在
        # Optiscaler\ 子目录里。原因见模板 [Libraries] 的注释 ——
        # 「OptiDllPath 默认为 .\OptiScaler」，也就是 OptiScaler 本来就是在
        # 这个子目录里找这些 dll 的。
        #
        # 曾经踩过的坑：白名单照抄版本 2（写根目录的 libxess_fg.dll 等），
        # 于是这些文件一个都没匹配到、静默跳过，产出的是一个做不了帧生成的
        # 残包 —— 装进游戏后 OptiScaler 报 "Can't find libxess_fg.dll"，
        # 然后因为 FGOutput=XeFG 建不起来而把游戏弄崩。
        "allow": [
            "dxgi.dll",
            "OptiScaler.ini",
            "nvngx_dlssnr.dll",             # DLSS 5 模型本体（158 MB，未签名）
            "nvngx.dll_dlssnr.dll",         # 转发器：模块路径必须含 "nvngx.dll" 才放行
            "D3D12_Optiscaler/**",
            "Licenses/**",
            # FGOutput = xefg 需要的 provider（23.8 MB）
            "Optiscaler/libxess_fg.dll",
            "Optiscaler/libxell.dll",
            "Optiscaler/fakenvapi.dll",
            "Optiscaler/fakenvapi.ini",
            # FGOutput = dlssg（DLSS 帧生成输出槽）需要（12.6 MB）
            "Optiscaler/streamline/**",
            "Optiscaler/dlssg_to_fsr3_amd_is_better.dll",   # FGNvngxReplacement=Nukems
        ],
        # 这个包里还有这些"别的输出路径"的 provider，出于体积没收录。
        # 需要时把它们加进上面的 allow 即可（括号内为体积）：
        #   Optiscaler/dlss-enabler-headless.dll            (31 MB)  Arturs / FSR3 MFG
        #   Optiscaler/amd_fidelityfx_*.dll                 (78 MB)  FGOutput=fsrfg / FFX / Combo
        #   Optiscaler/libxess.dll + libxess_dx11.dll       (78 MB)  XeSS 超分输入 / DX11
        "excluded_note": "dlss-enabler-headless / amd_fidelityfx_* / libxess*（见源码注释）",
    },
}


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def find_7z() -> str:
    for cand in SEVENZIP_CANDIDATES:
        if Path(cand).is_file():
            return cand
        got = shutil.which(cand)
        if got:
            return got
    print("找不到 7z / unrar，无法解压。请安装 7-Zip 后重试。", file=sys.stderr)
    raise SystemExit(2)


def pick_rar(src: Path, keywords: list[str], exclude: list[str]) -> Path | None:
    """按关键词挑压缩包。关键词全部命中、且不含任何排除词才算。"""
    for f in sorted(src.glob("*.rar")):
        name = f.name
        if not all(k in name for k in keywords):
            continue
        if any(x in name for x in exclude):
            continue
        return f
    return None


def extract_all(sevenzip: str, rar: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [sevenzip, "x", "-y", f"-o{dest}", str(rar)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(r.stdout[-3000:])
        print(r.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"解压失败（退出码 {r.returncode}）：{rar.name}")


def bundle_root(dest: Path) -> Path:
    """压缩包解出来通常套一层同名目录，把它剥掉。"""
    entries = [p for p in dest.iterdir() if not p.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest


def collect(root: Path, allow: list[str]) -> list[tuple[str, Path]]:
    """按白名单收集文件，返回 (相对路径, 绝对路径)，顺序稳定。"""
    out: dict[str, Path] = {}
    for pattern in allow:
        if pattern.endswith("/**"):
            base = root / pattern[:-3]
            if not base.is_dir():
                continue
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    rel = p.relative_to(root).as_posix()
                    out.setdefault(rel, p)
        else:
            p = root / pattern
            if p.is_file():
                out.setdefault(pattern, p)
    return sorted(out.items())


# --------------------------------------------------------------------------
# 生成基线模块
# --------------------------------------------------------------------------

HEADER = '''"""OptiScaler bundle 完整性基线 —— 自动生成，不要手改。

由 tools/fetch_optiscaler.py 生成于 {stamp}。

这里记录的是"装进游戏目录的每一个文件"的 SHA256 与体积。
安装器按它逐个校验、逐个备份、逐个删除 —— 所以这份清单同时也是
卸载白名单：不在清单里的文件，卸载时一个都不动。

来源压缩包（仅为可追溯，仓库不含这些二进制）：
{archives}
"""

from __future__ import annotations

GENERATED_AT = "{stamp}"

# bundle key -> 规格
BUNDLES: dict[str, dict] = {{
'''


def emit_module(specs: dict[str, dict], archives: dict[str, str]) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    arch_lines = "\n".join(f"  {k}: {v}" for k, v in sorted(archives.items()))
    parts = [HEADER.format(stamp=stamp, archives=arch_lines)]

    for key, s in specs.items():
        files = s["files"]
        total = sum(f[2] for f in files)
        parts.append(f'    "{key}": {{\n')
        parts.append(f'        "short": {s["short"]!r},\n')
        parts.append(f'        "display_name": {s["display_name"]!r},\n')
        parts.append(f'        "proxy": {s["proxy"]!r},\n')
        parts.append(f'        "ini_rel": {s["ini_rel"]!r},\n')
        parts.append(f'        "template_rel": {s["template_rel"]!r},\n')
        parts.append(f'        "source_archive": {archives.get(key, "")!r},\n')
        parts.append(f'        "total_bytes": {total},\n')
        parts.append('        "files": [\n')
        for rel, digest, size in files:
            parts.append(f'            ({rel!r}, {digest!r}, {size}),\n')
        parts.append('        ],\n')
        parts.append('    },\n')

    parts.append("}\n")
    parts.append('\n__all__ = ["BUNDLES", "GENERATED_AT"]\n')

    GENERATED.parent.mkdir(parents=True, exist_ok=True)
    GENERATED.write_text("".join(parts), encoding="utf-8")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(DEFAULT_SRC), help="含 *.rar 的目录")
    ap.add_argument("--only", choices=list(BUNDLE_SOURCES), help="只处理一个 bundle")
    ap.add_argument("--list", action="store_true", help="只列出将要提取的内容")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_dir():
        print(f"源目录不存在：{src}\n用 --src 指定包含压缩包的目录。", file=sys.stderr)
        return 2

    keys = [args.only] if args.only else list(BUNDLE_SOURCES)
    if not args.list:
        sevenzip = find_7z()

    specs: dict[str, dict] = {}
    archives: dict[str, str] = {}
    grand_total = 0

    for key in keys:
        conf = BUNDLE_SOURCES[key]
        rar = pick_rar(src, conf["keywords"], conf["exclude"])
        if rar is None:
            print(f"[跳过] {key}：在 {src} 找不到匹配的压缩包 "
                  f"(需含 {conf['keywords']}，不含 {conf['exclude']})")
            continue

        print(f"\n=== {key} ===")
        print(f"  源    : {rar.name}  ({rar.stat().st_size:,} bytes)")

        if args.list:
            # 只解压清单不现实（RAR 列表编码不可靠），直接提示需要完整解压
            print("  （--list 需要先解压才能列出文件树，这里只报告源包）")
            archives[key] = rar.name
            continue

        stage = WORK / key
        extract_all(sevenzip, rar, stage)
        root = bundle_root(stage)

        pairs = collect(root, conf["allow"])
        if not pairs:
            print(f"  白名单没匹配到任何文件 —— 压缩包结构与预期不符，跳过", file=sys.stderr)
            continue

        # 落到 payload/<key>/，保持相对路径
        out_root = PAYLOAD / key
        if out_root.exists():
            shutil.rmtree(out_root, ignore_errors=True)
        out_root.mkdir(parents=True, exist_ok=True)

        files: list[tuple[str, str, int]] = []
        for rel, abs_path in pairs:
            # 配置模板单独改名：运行时要在它的基础上打补丁，不能直接照抄。
            # 保留原版 ini 的好处是：上游给的上百个键一个不丢，我们只改必须改的那几项。
            target_rel = "OptiScaler.ini.template" if rel == "OptiScaler.ini" else rel
            dst = out_root / target_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(abs_path, dst)
            digest = sha256_file(dst)
            size = dst.stat().st_size
            files.append((target_rel, digest, size))

        # 主代理入口必须在
        proxy = "dxgi.dll"
        rels = {f[0] for f in files}
        if proxy not in rels:
            print(f"  缺少主入口 {proxy}，跳过该 bundle", file=sys.stderr)
            continue

        total = sum(f[2] for f in files)
        grand_total += total
        specs[key] = {
            "short": conf["short"],
            "display_name": conf["display_name"],
            "proxy": proxy,
            "ini_rel": "OptiScaler.ini",
            "template_rel": "OptiScaler.ini.template",
            "files": files,
        }
        archives[key] = rar.name

        print(f"  文件  : {len(files)} 个，共 {total:,} bytes ({total/1048576:.1f} MB)")
        for rel, _h, size in sorted(files, key=lambda x: -x[2])[:6]:
            print(f"          {size:>12,}  {rel}")

    if args.list:
        return 0

    if not specs:
        print("\n没有提取到任何 bundle。", file=sys.stderr)
        return 1

    emit_module(specs, archives)
    print(f"\n已写入基线：{GENERATED.relative_to(ROOT)}")
    print(f"payload 合计：{grand_total:,} bytes ({grand_total/1048576:.1f} MB)")
    print("\n下一步：python build.py   （会把这些 bundle 一起打包）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
