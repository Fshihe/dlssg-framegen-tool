"""代理 DLL 加载验证器（独立子进程运行，崩溃不影响主进程）。

在游戏目录里按 DLL 搜索规则加载代理 DLL，观察 Mod 是否真的初始化
（Level=2 时会在 EXE 旁创建 dlssg_sm86/logs/native_<PID>.jsonl）。

这不是替代真机跑游戏，而是排除"文件放对了但 DLL 根本没被加载"这一类问题。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: load_probe.py <game_dir> <dll_name> [wait_seconds]")
        return 2
    game_dir = Path(sys.argv[1])
    dll_name = sys.argv[2]
    wait_s = float(sys.argv[3]) if len(sys.argv) > 3 else 4.0

    dll_path = game_dir / dll_name
    if not dll_path.is_file():
        print(f"FAIL 找不到 {dll_path}")
        return 3

    # 让 Windows 优先从游戏目录解析依赖，模拟游戏启动时的加载顺序
    os.chdir(game_dir)
    try:
        os.add_dll_directory(str(game_dir))
    except Exception:
        pass

    print(f"PID={os.getpid()}  目标={dll_path}")
    try:
        handle = ctypes.WinDLL(str(dll_path))
    except OSError as exc:
        print(f"FAIL LoadLibrary 失败：{exc}")
        return 4

    print(f"OK   LoadLibrary 成功，句柄=0x{handle._handle:X}")

    # 确认它导出了 system32 同名 DLL 的关键转发函数
    key = {
        "version.dll": ["GetFileVersionInfoW", "GetFileVersionInfoSizeW", "VerQueryValueW"],
        "winmm.dll": ["timeGetTime", "mciSendStringW", "PlaySoundW"],
        "dinput8.dll": ["DirectInput8Create"],
        "winhttp.dll": ["WinHttpOpen", "WinHttpConnect", "WinHttpSendRequest"],
        "dxgi.dll": ["CreateDXGIFactory", "CreateDXGIFactory1", "CreateDXGIFactory2"],
    }.get(dll_name.lower(), [])

    missing = []
    for fn in key:
        try:
            getattr(handle, fn)
        except AttributeError:
            missing.append(fn)
    if key:
        if missing:
            print(f"WARN 缺少转发导出：{missing}")
        else:
            print(f"OK   关键转发导出齐全（{len(key)} 项）：{', '.join(key)}")

    time.sleep(wait_s)
    print("DONE 保持加载状态结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
