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


def _install_excepthook(cli_mode: bool) -> None:
    """防止 --windowed 打包的 exe 在异常时弹一个原始回溯窗口。

    打包成 GUI 子程序后，Python 默认的异常处理会弹 MessageBox 显示 traceback。
    用户看到的就是一个莫名其妙的技术弹窗（真实案例：管道提前关闭导致
    OSError: [Errno 22]，本该静默处理，却弹窗报错）。

    这里换掉默认行为：能写日志就写日志，能往控制台打就往控制台打，
    绝不再弹原始回溯。
    """
    import traceback

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        # 记到工具自己的日志里，方便事后排查
        try:
            from dgcore.paths import log as _log

            _log(f"未捕获异常：{exc}\n{text}", "error")
        except Exception:
            pass
        # 命令行模式下打到 stderr（同样要防住 stderr 坏掉）
        if cli_mode:
            for stream in (sys.stderr, sys.stdout):
                try:
                    if stream is not None:
                        stream.write(f"\n[内部错误] {exc_type.__name__}: {exc}\n")
                        return
                except Exception:
                    continue
        # GUI 模式下什么都不弹 —— 界面上已经有日志区了

    sys.excepthook = hook


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    first = argv[0] if argv else ""

    wants_cli = first in CLI_COMMANDS or first in ("-h", "--help", "--version")

    if wants_cli:
        if getattr(sys, "frozen", False):
            _attach_console()
        _install_excepthook(cli_mode=True)
        from dgcore.cli import main as cli_main

        if first == "help":
            argv = ["--help"] + argv[1:]
        try:
            return cli_main(argv)
        except SystemExit:
            raise
        except KeyboardInterrupt:
            return 130
        except Exception as exc:
            # 命令行工具不该因为未预期的异常弹窗或吐 traceback
            import traceback

            text = traceback.format_exc()
            try:
                from dgcore.paths import log as _log

                _log(f"CLI 未捕获异常：{exc}\n{text}", "error")
            except Exception:
                pass
            try:
                sys.stderr.write(f"\n[出错了] {type(exc).__name__}: {exc}\n")
                sys.stderr.write("详细信息已记入 %LOCALAPPDATA%\\DLSSG-SM86-Tool\\logs\\\n")
            except Exception:
                pass
            return 2

    # 默认进 GUI
    _install_excepthook(cli_mode=False)
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
