"""程序入口。

不带参数 → 打开图形界面。
带命令（detect / list / install / selftest …）→ 走命令行。

打包成 --windowed 的单文件 exe 后，命令行模式下会主动附着到父控制台，
因此同一个 exe 既能双击当 GUI 用，也能在 cmd 里当命令行工具用。
"""

from __future__ import annotations

import os
import sys

CLI_COMMANDS = {
    "detect", "list", "inspect", "check", "plan", "install", "uninstall",
    "verify", "report", "collect", "selftest", "paths", "hags", "help",
}


def _attach_console() -> None:
    """让 --windowed 打包的 exe 在命令行下也能正常输出。

    打包成 GUI 子系统后 Python 的 sys.stdout 是 None，需要自己接回来：
      1. 优先复用进程已有的标准句柄 —— 这样 cmd 直接运行、以及被管道重定向
         （构建脚本要靠它抓取自检输出）两种情况都能正常工作；
      2. 都没有时才挂到父控制台，最后才自己开一个控制台窗口。
    顺带把代码页切成 UTF-8，否则默认 GBK 下中文会变乱码。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        import msvcrt

        k32 = ctypes.windll.kernel32
        INVALID = ctypes.c_void_p(-1).value

        def handle(std_id: int):
            h = k32.GetStdHandle(std_id)
            if not h or h == INVALID:
                return None
            return h

        if handle(-11) is None and handle(-12) is None:
            if not k32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
                k32.AllocConsole()

        k32.SetConsoleOutputCP(65001)
        k32.SetConsoleCP(65001)

        for std_id, attr, mode in ((-11, "stdout", "w"), (-12, "stderr", "w")):
            h = handle(std_id)
            if h is None:
                continue
            try:
                fd = msvcrt.open_osfhandle(h, os.O_WRONLY)
                setattr(sys, attr, os.fdopen(fd, mode, encoding="utf-8", errors="replace", buffering=1))
            except Exception:
                pass

        h = handle(-10)
        if h is not None and sys.stdin is None:
            try:
                fd = msvcrt.open_osfhandle(h, os.O_RDONLY)
                sys.stdin = os.fdopen(fd, "r", encoding="utf-8", errors="replace")
            except Exception:
                pass
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    first = argv[0] if argv else ""

    wants_cli = first in CLI_COMMANDS or first in ("-h", "--help", "--version")

    if wants_cli:
        if getattr(sys, "frozen", False):
            _attach_console()
        from dgcore.cli import main as cli_main

        if first == "help":
            argv = ["--help"] + argv[1:]
        return cli_main(argv)

    # 默认进 GUI
    try:
        from dgcore.gui import launch

        return launch()
    except Exception as exc:  # GUI 起不来时给个能看懂的错误
        import traceback

        tb = traceback.format_exc()
        try:
            import tkinter.messagebox as mb

            mb.showerror("启动失败", f"{exc}\n\n{tb}")
        except Exception:
            sys.stderr.write(tb)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
