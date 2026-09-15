"""图形界面：三步走 —— 看环境 → 选游戏 → 一键安装。

线程模型：所有耗时操作（扫描游戏库、解析 PE、写文件）都丢到后台线程，
结果通过 queue 回主线程刷新，界面全程不卡死。
"""

from __future__ import annotations

import ctypes
import os
import queue
import re
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import APP_NAME, UPSTREAM_REPO, UPSTREAM_VERSION, VERSION
from . import anticheat as ac
from . import games, gpu, installer, optiscaler, state, winenv
from . import capability
from . import profiles
from .gpu import VERDICT_NOT_NEEDED, VERDICT_OK
from .paths import log as _log
from .paths import reports_root

FONT = ("Microsoft YaHei UI", 9)
FONT_B = ("Microsoft YaHei UI", 9, "bold")
FONT_H = ("Microsoft YaHei UI", 11, "bold")
FONT_T = ("Microsoft YaHei UI", 15, "bold")
FONT_M = ("Consolas", 9)

C_OK = "#1a7f37"
C_WARN = "#9a6700"
C_ERR = "#cf222e"
C_DIM = "#57606a"
C_BG_OK = "#e6f4ea"
C_BG_WARN = "#fff8e1"
C_BG_ERR = "#fdecea"

# 引擎下拉框的取值。DLSS 5 那一项不进下拉框 —— 它跟常规的 XeSS 多帧生成不是一回事
# （目前与帧生成冲突），单独放到「实验性」区块里，免得混在一起被顺手选中。
# 这三段文本同时也是 _engine_map 的键。
ENGINE_CHOICE_DLSSG = "DLSSG（NVIDIA DLSS 帧生成）"
ENGINE_CHOICE_XESS = "OptiScaler · XeSS 多帧生成"
ENGINE_CHOICE_DLSS5 = "OptiScaler · DLSS 5 神经网络渲染"

# 选中 DLSS 5 引擎时摆出来的警告。只写已确认的后果，不写建议。
DLSS5_WARNING = (
    "已确认的后果：\n"
    "· 无法与帧生成同时使用 —— 开启后帧生成仍会产出帧，但产出的帧不进入画面。\n"
    "· 所用 nvngx_dlssnr.dll 未签名，且不在当前驱动中（本工具原样写入，不做修改）。\n"
    "· 只能在游戏内通过叠加层快捷键启用；写入 ini 后该值会被回写为 true，"
    "而 true 会导致下一次启动的游戏退出。\n"
    "\n"
    "卸载可完整还原。"
)


class Task:
    """把一个后台任务的结果送回主线程。"""

    def __init__(self, app: "App", name: str):
        self.app = app
        self.name = name

    def __call__(self, fn, on_done=None, on_error=None):
        def runner():
            try:
                res = fn()
            except Exception as exc:
                tb = traceback.format_exc()
                _log(f"任务 {self.name} 异常: {exc}\n{tb}", "error")
                self.app.q.put(("error", self.name, (exc, tb), on_error))
            else:
                self.app.q.put(("done", self.name, res, on_done))

        threading.Thread(target=runner, daemon=True).start()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} v{VERSION}")
        self.geometry("1020x840")
        self.minsize(900, 780)

        self.q: queue.Queue = queue.Queue()
        self.task = Task(self, "main")
        self.busy = False

        self.env: gpu.EnvReport | None = None
        self.game_list: list[games.Game] = []
        self.selected: games.Game | None = None
        self.candidates: list[games.ExeCandidate] = []
        self.chosen_exe: Path | None = None
        self.prediction = None
        self.anticheat = ac.AntiCheatReport([], [])

        self.proxy_var = tk.StringVar(value="自动选择")
        self.sample_var = tk.StringVar(value="精确档（HardwareBilinear=0）")
        self.loglv_var = tk.StringVar(value="仅错误（Level=1）")
        self.frames_var = tk.StringVar(value=installer.FRAME_OPTIONS[0])
        self.preset_var = tk.StringVar(value="标准")
        self.filter_var = tk.BooleanVar(value=True)
        self.exe_var = tk.StringVar(value="")
        self.router_var = tk.StringVar(value="自动（按显卡判断）")

        # 引擎选择：两个引擎族互斥，同一个游戏目录只能装一个
        self._engine_map: dict[str, tuple[str, str]] = {
            ENGINE_CHOICE_DLSSG: (state.ENGINE_DLSSG, ""),
            ENGINE_CHOICE_XESS: (state.ENGINE_OPTISCALER, "optiscaler-xess"),
            ENGINE_CHOICE_DLSS5: (state.ENGINE_OPTISCALER, "optiscaler-dlss5"),
        }
        self.engine_var = tk.StringVar(value=ENGINE_CHOICE_DLSSG)
        # 引擎是否已按显卡自动定过。用户自己改过之后就不再覆盖他的选择。
        self._engine_autoset = False
        # 实验性那段警告的换行宽度，跟着窗口走（见 _on_wrap）
        self._wrap_w = 0

        self._build()
        self.after(80, self._pump)
        self._log(f"{APP_NAME} v{VERSION} 已启动")
        self._log(f"上游 Mod：DLSSG Native {UPSTREAM_VERSION} — {UPSTREAM_REPO}")
        self._log("本工具只在游戏目录里写入自己的文件，且写入前一律先备份、写入后校验哈希。")
        self._sync_engine_widgets()
        self.refresh_env()

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------

    def _build(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure(".", font=FONT)

        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        # 分页在上、日志在下：日志区不属于任何一页，切标签页也不会跟着切走
        root.rowconfigure(1, weight=1)

        # ---------------- 标题 ----------------
        head = ttk.Frame(root)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text="DLSS 帧生成 一键开启工具", font=FONT_T).grid(row=0, column=0, sticky="w")
        ttk.Label(
            head,
            text=f"适用 RTX 20 / 30 系列 · 上游 DLSSG Native {UPSTREAM_VERSION}",
            font=FONT, foreground=C_DIM,
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        # ---------------- 分页 ----------------
        # 三段分开：先看环境能不能用 → 再挑游戏装 → 平时不动的旋钮丢到高级。
        # 日志区留在分页外面，三页都看得见。
        nb = ttk.Notebook(root)
        nb.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        tab_env = ttk.Frame(nb, padding=10)
        tab_game = ttk.Frame(nb, padding=10)
        tab_adv = ttk.Frame(nb, padding=10)
        nb.add(tab_env, text=" 环境 ")
        nb.add(tab_game, text=" 游戏与安装 ")
        nb.add(tab_adv, text=" 高级 ")
        for _tab in (tab_env, tab_adv):
            _tab.columnconfigure(0, weight=1)
        tab_game.columnconfigure(0, weight=1)
        tab_game.rowconfigure(1, weight=1)

        # ---------------- ① 环境 ----------------
        env_box = ttk.LabelFrame(tab_env, text=" 系统与显卡环境 ", padding=10)
        env_box.grid(row=0, column=0, sticky="ew")
        env_box.columnconfigure(0, weight=1)

        self.env_frame = tk.Frame(env_box, bg=C_BG_WARN, bd=1, relief="solid")
        self.env_frame.grid(row=0, column=0, sticky="ew")
        self.env_frame.columnconfigure(0, weight=1)

        self.env_title = tk.Label(
            self.env_frame, text="正在检测显卡与驱动…", font=FONT_H,
            bg=C_BG_WARN, fg=C_WARN, anchor="w", padx=10, pady=4,
        )
        self.env_title.grid(row=0, column=0, sticky="ew")
        self.env_detail = tk.Label(
            self.env_frame, text="", font=FONT, bg=C_BG_WARN, fg="#333",
            anchor="w", justify="left", padx=10, pady=4,
        )
        self.env_detail.grid(row=1, column=0, sticky="ew")

        btns = ttk.Frame(env_box)
        btns.grid(row=1, column=0, sticky="e", pady=(6, 0))
        self.btn_hags = ttk.Button(
            btns, text="开启硬件加速 GPU 计划", command=self.do_enable_hags
        )
        self.btn_hags.pack(side="right", padx=(0, 6))
        self.btn_env = ttk.Button(btns, text="重新检测环境", command=self.refresh_env)
        self.btn_env.pack(side="right")
        self.btn_report = ttk.Button(btns, text="生成环境报告", command=self.do_report)
        self.btn_report.pack(side="right", padx=(0, 6))
        self.btn_selftest = ttk.Button(btns, text="安全自检", command=self.do_selftest)
        self.btn_selftest.pack(side="right", padx=(0, 6))

        ttk.Label(
            tab_env,
            text="硬件加速 GPU 计划（HAGS）是帧生成的前置条件，状态见上方。",
            font=FONT, foreground=C_DIM, justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))

        # ---------------- 安装设置（引擎 + 倍率） ----------------
        # 引擎和倍率决定下面所有选项的含义，也是日常真正要动的东西，
        # 所以摆在「游戏与安装」页最上面。
        setup = ttk.LabelFrame(tab_game, text=" 安装设置 ", padding=10)
        setup.grid(row=0, column=0, sticky="ew")
        for col in (1, 3):
            setup.columnconfigure(col, weight=1)

        ttk.Label(setup, text="帧生成引擎：", font=FONT).grid(row=0, column=0, sticky="w")
        self.engine_combo = ttk.Combobox(
            setup, textvariable=self.engine_var, font=FONT, state="readonly",
            # 下拉框只列两个主引擎。DLSS 5 是实验性的第三个引擎，放在「高级」页
            # （见 _build_advanced 里的 dlss5_check）—— 不放在这里，避免误选。
            values=[ENGINE_CHOICE_DLSSG, ENGINE_CHOICE_XESS],
        )
        self.engine_combo.grid(row=0, column=1, columnspan=3, sticky="ew")
        self.engine_combo.bind("<<ComboboxSelected>>", self._on_engine_change)

        self.engine_hint = ttk.Label(setup, text="", font=FONT, foreground=C_DIM, justify="left")
        self.engine_hint.grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # 倍率上限：这是评论区明确要求加的功能（0.3.0 最高 6X）
        ttk.Label(setup, text="最高倍率：", font=FONT).grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.frames_combo = ttk.Combobox(
            setup, textvariable=self.frames_var, font=FONT, state="readonly",
            values=list(installer.FRAME_OPTIONS),
        )
        self.frames_combo.grid(row=3, column=1, sticky="ew", padx=(0, 12), pady=(6, 0))
        # 倍率一变，下面的说明（尤其是"实验性档位"警告）要跟着变
        self.frames_combo.bind("<<ComboboxSelected>>", self._on_frames_change)

        self.frames_hint = ttk.Label(setup, text="", font=FONT, foreground=C_DIM, justify="left")
        self.frames_hint.grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # ---------------- ② 选游戏 ----------------
        game_box = ttk.LabelFrame(tab_game, text=" 选择要开启帧生成的游戏 ", padding=10)
        game_box.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        game_box.columnconfigure(0, weight=0, minsize=290)
        game_box.columnconfigure(1, weight=1)
        game_box.rowconfigure(1, weight=1)

        bar = ttk.Frame(game_box)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        bar.columnconfigure(2, weight=1)
        ttk.Button(bar, text="浏览游戏文件夹…", command=self.browse_game).grid(row=0, column=0)
        ttk.Button(bar, text="直接指定游戏主程序…", command=self.browse_exe).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(bar, text="重新扫描游戏库", command=self.scan_games).grid(row=0, column=2, padx=(6, 0), sticky="w")
        # 只对 DLSSG 引擎有意义：那个引擎的成败取决于游戏自带不带 DLSS 帧生成。
        # XeSS 引擎靠超分输入插帧，跟这个无关，所以选 XeSS 时这一项置灰。
        self.filter_check = ttk.Checkbutton(
            bar, text="只显示能开启帧生成的", variable=self.filter_var,
            command=self._apply_filter,
        )
        self.filter_check.grid(row=0, column=3, sticky="w", padx=(12, 0))
        self.scan_status = ttk.Label(bar, text="", font=FONT, foreground=C_DIM)
        self.scan_status.grid(row=0, column=4, sticky="e")

        left = ttk.Frame(game_box)
        left.grid(row=1, column=0, sticky="nsew")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.listbox = tk.Listbox(
            left, font=FONT, activestyle="none", exportselection=False,
            selectmode="browse", bd=1, relief="solid", highlightthickness=0,
            # 只给 6 行的最小高度：窗口拉小、或实验性那段警告展开时，
            # 列表被挤扁也不会把下面的按钮顶出可见范围
            height=6,
        )
        self.listbox.grid(row=0, column=0, sticky="nsew")
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.listbox.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=sb.set)

        right = ttk.Frame(game_box)
        right.grid(row=1, column=1, sticky="nsew", padx=(10, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        self.detail = tk.Text(
            right, font=FONT, wrap="word", height=7, bd=1, relief="solid",
            highlightthickness=0, state="disabled", cursor="arrow",
            background="#fbfbfd",
        )
        self.detail.grid(row=0, column=0, sticky="nsew")
        self.detail.tag_configure("h", font=FONT_H, foreground="#111", spacing1=2, spacing3=4)
        self.detail.tag_configure("ok", foreground=C_OK)
        self.detail.tag_configure("warn", foreground=C_WARN)
        self.detail.tag_configure("err", foreground=C_ERR)
        self.detail.tag_configure("dim", foreground=C_DIM)
        self.detail.tag_configure("mono", font=FONT_M)

        exe_row = ttk.Frame(right)
        exe_row.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        exe_row.columnconfigure(1, weight=1)
        ttk.Label(exe_row, text="游戏主程序：", font=FONT).grid(row=0, column=0, sticky="w")
        self.exe_combo = ttk.Combobox(exe_row, textvariable=self.exe_var, font=FONT, state="readonly")
        self.exe_combo.grid(row=0, column=1, sticky="ew")
        self.exe_combo.bind("<<ComboboxSelected>>", self._on_exe_change)

        # ---------------- ③ 高级 ----------------
        # 平时不用动的旋钮全在这页，主流程那页就不会被淹掉。
        opt = ttk.LabelFrame(tab_adv, text=" 高级选项 ", padding=10)
        opt.grid(row=0, column=0, sticky="ew")
        for col in (1, 3):
            opt.columnconfigure(col, weight=1)

        ttk.Label(opt, text="计算路由：", font=FONT).grid(row=0, column=0, sticky="w")
        self.router_combo = ttk.Combobox(
            opt, textvariable=self.router_var, font=FONT, state="readonly",
            values=["自动（按显卡判断）", "SM86（RTX 30 系列）", "SM75（RTX 20 系列，实验性）"],
        )
        self.router_combo.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        self.router_combo.bind("<<ComboboxSelected>>", self._on_router_change)

        ttk.Label(opt, text="代理入口：", font=FONT).grid(row=0, column=2, sticky="w")
        self.proxy_combo = ttk.Combobox(
            opt, textvariable=self.proxy_var, font=FONT, state="readonly",
            values=["自动选择"] + list(profiles.PROFILE_024.proxy_names),
        )
        self.proxy_combo.grid(row=0, column=3, sticky="ew")

        ttk.Label(opt, text="采样档：", font=FONT).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.sample_combo = ttk.Combobox(
            opt, textvariable=self.sample_var, font=FONT, state="readonly",
            values=["精确档（HardwareBilinear=0）", "性能档（HardwareBilinear=1，仅 SM86）"],
        )
        self.sample_combo.grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=(6, 0))

        ttk.Label(opt, text="日志级别：", font=FONT).grid(row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Combobox(
            opt, textvariable=self.loglv_var, font=FONT, state="readonly",
            values=["关闭（Level=0）", "仅错误（Level=1）", "信息（Level=2）", "调试（Level=3）", "全部（Level=4）"],
        ).grid(row=1, column=3, sticky="ew", pady=(6, 0))

        ttk.Label(opt, text="扫描档位：", font=FONT).grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.preset_combo = ttk.Combobox(
            opt, textvariable=self.preset_var, font=FONT, state="readonly",
            values=list(games.SCAN_PRESETS.keys()),
        )
        self.preset_combo.grid(row=2, column=1, sticky="ew", padx=(0, 12), pady=(6, 0))
        self.preset_combo.bind("<<ComboboxSelected>>", self._on_preset_change)

        # 上面这几项都只作用于 DLSSG 引擎，切到 OptiScaler 时要说清楚，
        # 免得看到一片置灰以为界面坏了
        self.advanced_hint = ttk.Label(opt, text="", font=FONT, foreground=C_DIM, justify="left")
        self.advanced_hint.grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # 运动矢量分辨率：OptiScaler 引擎专有。默认开着 —— 实测黑神话这类 UE5
        # 游戏不设它的话，XeFG 每帧都因 MV/深度分辨率不匹配而失败（表现为"没效果"）。
        self.hiresmv_var = tk.BooleanVar(value=False)
        self.hiresmv_check = ttk.Checkbutton(
            opt, text="运动矢量按高分辨率处理（HighResMV）",
            variable=self.hiresmv_var,
        )
        self.hiresmv_check.grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # 共存模式：DLSSG 引擎与 OptiScaler 引擎装入同一目录，此时 OptiScaler
        # 的帧生成输入改为 dlssg（取游戏自身的 DLSS 帧生成流）。
        # 两者各用各的代理入口：DLSSG 用 version.dll、OptiScaler 用 dxgi.dll。
        self.coexist_var = tk.BooleanVar(value=False)
        self.coexist_check = ttk.Checkbutton(
            opt, text="与 DLSSG 引擎共存（输入改为 DLSS 流，倍率由本引擎决定）",
            variable=self.coexist_var,
        )
        self.coexist_check.grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # 关闭 OptiScaler 自身的帧生成，只保留神经网络渲染等其它通道。
        # 与上一条的区别：上一条让本引擎继续产出帧，这一条不产出帧。
        self.nofg_var = tk.BooleanVar(value=False)
        self.nofg_check = ttk.Checkbutton(
            opt, text="关闭本引擎的帧生成（只保留神经网络渲染等其它通道）",
            variable=self.nofg_var,
        )
        self.nofg_check.grid(row=6, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # ---------------- 实验性引擎：DLSS 5 神经网络渲染 ----------------
        # 放在「高级」页而不是「游戏与安装」页：它是第三个引擎（OptiScaler 的
        # DLSS 5 构建），和 XeSS 多帧生成不是一回事，选中后直接替换引擎包。
        # 主流程那页只留两个成熟引擎，避免误选。
        self.dlss5_box = ttk.LabelFrame(
            tab_adv, text=" 实验性引擎 ", padding=10
        )
        self.dlss5_box.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.dlss5_box.columnconfigure(0, weight=1)

        self.dlss5_var = tk.BooleanVar(value=False)
        self.dlss5_check = ttk.Checkbutton(
            self.dlss5_box,
            text="DLSS 5 神经网络渲染（OptiScaler · 替换 XeSS 多帧生成）",
            variable=self.dlss5_var, command=self._on_dlss5_toggle,
        )
        self.dlss5_check.grid(row=0, column=0, sticky="w")

        # 警告块：选中这个引擎才展开
        self.dlss5_warn = ttk.Frame(self.dlss5_box)
        self.dlss5_warn.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.dlss5_warn.columnconfigure(0, weight=1)
        self.dlss5_note = ttk.Label(
            self.dlss5_warn, text=DLSS5_WARNING, font=FONT, foreground=C_WARN,
            justify="left", wraplength=700,
        )
        self.dlss5_note.grid(row=0, column=0, sticky="ew")
        self.dlss5_warn.bind("<Configure>", self._on_wrap)
        self.dlss5_warn.grid_remove()

        # ---------------- 操作按钮（属于「游戏与安装」页） ----------------
        act = ttk.Frame(tab_game)
        act.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        act.columnconfigure(4, weight=1)
        self.btn_preview = ttk.Button(act, text="预览将要做的改动", command=self.do_preview)
        self.btn_preview.grid(row=0, column=0)
        self.btn_install = ttk.Button(act, text="⚡ 一键安装", command=self.do_install)
        self.btn_install.grid(row=0, column=1, padx=(6, 0))
        self.btn_uninstall = ttk.Button(act, text="卸载并还原", command=self.do_uninstall)
        self.btn_uninstall.grid(row=0, column=2, padx=(6, 0))
        self.installed_label = ttk.Label(act, text="", font=FONT, foreground=C_DIM)
        self.installed_label.grid(row=0, column=3, padx=(12, 0), sticky="w")

        # ---------------- 日志（三页共用，切标签页也不消失） ----------------
        logbox = ttk.LabelFrame(root, text=" 运行日志 ", padding=(6, 4))
        logbox.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        logbox.columnconfigure(0, weight=1)
        logbox.rowconfigure(0, weight=1)
        self.logtext = tk.Text(
            logbox, font=FONT_M, height=7, wrap="word", bd=0, state="disabled",
            background="#1e1e24", foreground="#d6d6d6", insertbackground="#d6d6d6",
        )
        self.logtext.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(logbox, orient="vertical", command=self.logtext.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.logtext.configure(yscrollcommand=lsb.set)
        self.logtext.tag_configure("ok", foreground="#7ee787")
        self.logtext.tag_configure("warn", foreground="#ffd866")
        self.logtext.tag_configure("err", foreground="#ff7b72")
        self.logtext.tag_configure("dim", foreground="#8b949e")

        self.status = ttk.Label(root, text="就绪", font=FONT, foreground=C_DIM, anchor="w")
        self.status.grid(row=3, column=0, sticky="ew", pady=(6, 0))

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------

    def _log(self, msg: str, tag: str = "") -> None:
        self.logtext.configure(state="normal")
        for i, line in enumerate(str(msg).splitlines() or [""]):
            self.logtext.insert("end", line + "\n", tag if i == 0 else "")
        self.logtext.see("end")
        self.logtext.configure(state="disabled")

    def _set_status(self, text: str, color: str = C_DIM) -> None:
        self.status.configure(text=text, foreground=color)

    def _pump(self) -> None:
        try:
            while True:
                kind, name, payload, cb = self.q.get_nowait()
                self._set_busy(False)
                if kind == "done":
                    if cb:
                        try:
                            cb(payload)
                        except Exception:
                            self._log(traceback.format_exc(), "err")
                else:
                    exc, tb = payload
                    self._log(f"操作失败：{exc}", "err")
                    _log(tb, "error")
                    if cb:
                        cb(exc)
                    else:
                        messagebox.showerror("出错了", str(exc))
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_env, self.btn_install, self.btn_uninstall,
                  self.btn_preview, self.btn_selftest, self.btn_report):
            try:
                b.configure(state=state)
            except tk.TclError:
                pass
        self.configure(cursor="watch" if busy else "")

    def _run(self, fn, on_done=None, on_error=None, status: str = "处理中…") -> None:
        if self.busy:
            messagebox.showinfo("请稍候", "上一个操作还没结束。")
            return
        self._set_busy(True)
        self._set_status(status)
        self.task(fn, on_done, on_error)

    # ------------------------------------------------------------------
    # ① 环境
    # ------------------------------------------------------------------

    def refresh_env(self) -> None:
        def work():
            return gpu.detect()

        def done(env: gpu.EnvReport):
            self.env = env
            self._autoselect_engine()
            self._render_env()
            self._set_status("环境检测完成")
            self.scan_games()

        self._run(work, done, status="正在检测显卡与驱动…")

    def _autoselect_engine(self) -> None:
        """按显卡自动选引擎：RTX 20/30 → DLSSG，其余 → XeSS。

        为什么这么分：DLSSG 引擎走的是 NVIDIA 的 DLSS 帧生成运行库，只有
        N 卡能用；A 卡 / I 卡 / 核显只能走 XeSS（OptiScaler）。所以检测到什么
        卡就用什么引擎，省掉"选错引擎、装完没效果"这一整类问题。

        只在用户没手动改过的时候自动定；改过就不再覆盖 —— 他可能有自己的理由。
        """
        if self._engine_autoset:
            return
        env = self.env
        g = env.primary if env else None

        if g is not None and g.verdict == VERDICT_OK:
            # RTX 20/30（SM75/SM86）：DLSSG 引擎是成熟路径
            want = ENGINE_CHOICE_DLSSG
        else:
            # 其余情况一律走 XeSS：A 卡 / I 卡 / 没有 N 卡 / N 卡架构不支持本 Mod
            want = ENGINE_CHOICE_XESS

        if self.engine_var.get() != want:
            self.engine_var.set(want)
            self._sync_engine_widgets()
        self._engine_autoset = True
        self._log(f"按显卡自动选择引擎：{want}")

    def _render_env(self) -> None:
        env = self.env
        if env is None:
            return
        g = env.primary
        if g is None:
            # A 卡 / I 卡 / 核显没有 DLSS 帧生成，但有 XeSS 那条路
            bg, fg, title = C_BG_WARN, C_WARN, "没有检测到 NVIDIA 显卡"
            detail = (
                "DLSSG 引擎用不了（它依赖 NVIDIA 驱动提供的 NGX / NVAPI / CUDA 接口）。\n"
                "已经自动切到 XeSS 引擎 —— 它靠显卡的超分能力插帧，A 卡 / I 卡 / 核显也能试。\n"
                "XeSS 的实际效果很依赖具体游戏和驱动环境，装完请自己进游戏确认有没有生效。"
            )
        elif g.verdict == VERDICT_OK:
            bg, fg = C_BG_OK, C_OK
            title = f"✓ 显卡可用：{g.name}"
            detail = (
                f"架构 {g.family}　驱动 {g.driver or '未知'}　显存 {g.vram_mib} MiB\n"
                f"计算路由将使用 Router={g.router}，KernelImage=PTX"
                + ("　（笔记本版）" if g.is_laptop else "")
            )
        elif g.verdict == VERDICT_NOT_NEEDED:
            bg, fg = C_BG_WARN, C_WARN
            title = f"⚠ 不需要这个工具：{g.name}"
            detail = g.reason + "\n在游戏的画面设置里直接打开「帧生成」即可。"
        elif g.name and not g.router:
            # N 卡但不在本 Mod 覆盖范围（比 20 系更老、或更特殊）→ 退到 XeSS
            bg, fg = C_BG_WARN, C_WARN
            title = f"⚠ DLSSG 用不了：{g.name}"
            detail = (
                f"{g.family or '未知架构'}　驱动 {g.driver or '未知'}\n{g.reason}\n"
                "已经自动切到 XeSS 引擎 —— XeSS 不依赖本 Mod 的 NVIDIA 内核，"
                "该引擎不依赖本 Mod 的 NVIDIA 内核。"
            )
        else:
            bg, fg = C_BG_ERR, C_ERR
            title = f"✗ 显卡不受支持：{g.name}"
            detail = f"{g.family or '未知架构'}　驱动 {g.driver or '未知'}\n{g.reason}"

        self.env_frame.configure(bg=bg)
        for w in (self.env_title, self.env_detail):
            w.configure(bg=bg)
        self.env_title.configure(text=title, fg=fg)

        # 硬件加速 GPU 计划：帧生成的硬性前置条件，缺了它游戏会直接说"显卡不支持"
        hags_line = ""
        if env.hags is False:
            hags_line = (
                "\n\n⚠ 硬件加速 GPU 计划（HAGS）已关闭 —— 帧生成的硬性前置条件。\n"
                "     不开启的话，Mod 装好了游戏也会提示「您的显卡不支持 DLSS 帧生成技术」。\n"
                "     点右边「开启硬件加速 GPU 计划」即可（改完需重启电脑）。"
            )
            bg = C_BG_ERR if g is not None and g.verdict == VERDICT_OK else bg
            self.env_frame.configure(bg=bg)
            for w in (self.env_title, self.env_detail):
                w.configure(bg=bg)
            if g is not None and g.verdict == VERDICT_OK:
                self.env_title.configure(fg=C_WARN)
        elif env.hags is True:
            hags_line = "\n✓ 硬件加速 GPU 计划已开启（帧生成前置条件满足）"
        else:
            hags_line = "\n· 硬件加速 GPU 计划：读不到状态"

        extra = ""
        # HAGS 那条已经在上面单独高亮显示了，这里不要再重复一遍
        other_notes = [n for n in env.notes if "硬件加速" not in n]
        if other_notes:
            extra = "\n" + "；".join(other_notes)
        self.env_detail.configure(text=detail + hags_line + extra)

        try:
            self.btn_hags.configure(state="normal" if env.hags is False else "disabled")
        except tk.TclError:
            pass

        self._log(f"显卡：{g.name if g else '(无)'} → {g.family if g else '-'} / Router={env.router}")
        self._log(f"硬件加速GPU计划(HAGS)：{winenv.describe(env.hags, env.hags_raw)}",
                  "err" if env.hags is False else "")
        # 路由确定后，把代理入口/倍率的可选范围刷成对应 profile 的
        self._sync_profile_widgets()
        _prof = profiles.for_router(env.router)
        self._log(f"将使用上游 {_prof.version}：{_prof.explanation}")
        if env.router == "SM75":
            self._log("⚠ 20 系是实验性支持（上游 0.3.0 已移除 SM75 内核，本工具自动用 0.2.4）", "warn")

    # ------------------------------------------------------------------
    # ② 游戏
    # ------------------------------------------------------------------

    def scan_games(self) -> None:
        preset = self.preset_var.get()

        def work():
            libs = games.scan_libraries()
            for g in libs:
                games.inspect_game(g, preset=preset)
            return libs

        def done(libs: list[games.Game]):
            self.game_list = libs
            if self._list_only_fg():
                n_ok = sum(1 for g in libs if g.best and g.best.eligible)
                self._log(f"扫描完成（「{preset}」档）：发现 {len(libs)} 个游戏，"
                          f"其中 {n_ok} 个可以开启帧生成")
            else:
                # XeSS 引擎不看游戏自带帧生成，所以不报"几个能开"
                self._log(f"扫描完成（「{preset}」档）：发现 {len(libs)} 个游戏"
                          "（XeSS 引擎：能不能用取决于游戏有没有可钩的超分）")
            self._set_status(f"已扫描到 {len(libs)} 个游戏")
            self._apply_filter()

        self._run(work, done, status=f"正在按「{preset}」档扫描游戏库…")

    def _list_only_fg(self) -> bool:
        """游戏列表要不要按「能开帧生成」筛选/标注。

        只有 DLSSG 引擎才这么标：那个引擎的成败取决于游戏自带不带 DLSS 帧生成
        组件（nvngx_dlssg.dll）。XeSS 引擎不看这个 —— 它靠超分输入插帧，
        游戏有没有帧生成跟它能不能用是两回事。混在一起标会让人以为
        「没★ 就是不能用」，而实际上 XeSS 很可能能跑。
        """
        engine, _bundle = self._current_engine()
        return engine != state.ENGINE_OPTISCALER

    def _apply_filter(self) -> None:
        self.listbox.delete(0, "end")
        per_fg = self._list_only_fg()
        only = self.filter_var.get() and per_fg
        shown = 0
        for g in self.game_list:
            b = g.best
            if only and not (b and b.eligible):
                continue
            mark = ("★ " if (b and b.eligible) else "· ") if per_fg else ""
            self.listbox.insert("end", f"{mark}{g.name}")
            shown += 1
        if per_fg:
            self._log(f"列表显示 {shown} 个游戏" + ("（已过滤掉不支持的）" if only else ""))
        else:
            self._log(f"列表显示 {shown} 个游戏（XeSS 引擎：不看游戏自带帧生成，"
                      "全部列出）")
        self.scan_status.configure(text=f"{shown} / {len(self.game_list)}")
        if shown:
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(0)
            self._on_select()

    def _current_list(self) -> list[games.Game]:
        only = self.filter_var.get() and self._list_only_fg()
        if not only:
            return list(self.game_list)
        return [g for g in self.game_list if g.best and g.best.eligible]

    def _on_router_change(self, _evt=None) -> None:
        """路由变了 → 可用的代理入口和倍率也跟着变（0.2.4 和 0.3.0 不一样）。"""
        self._sync_profile_widgets()

    def _on_engine_change(self, _evt=None) -> None:
        # 用户自己动过下拉框 → 之后环境重检不再覆盖他的选择
        self._engine_autoset = True
        self._sync_engine_widgets()
        self._render_detail()
        self._apply_filter()

    def _on_dlss5_toggle(self) -> None:
        """实验性引擎勾选框：勾上换成 DLSS 5 引擎包，取消退回 XeSS 包。

        它和下拉框改的是同一个 engine_var，所以两边不会各说各话。
        """
        # 用户显式选过了 —— 环境检测晚一步回来时不许再覆盖他的选择。
        # 漏了这一句的话，勾选 DLSS 5 之后会被异步完成的自动选引擎改回 DLSSG。
        self._engine_autoset = True
        if bool(self.dlss5_var.get()):
            if self.engine_var.get() != ENGINE_CHOICE_DLSS5:
                self.engine_var.set(ENGINE_CHOICE_DLSS5)
        elif self.engine_var.get() == ENGINE_CHOICE_DLSS5:
            # 只在当前确实是 DLSS 5 时退回 XeSS；如果用户在下拉框里选了别的
            # 引擎（DLSSG），这里什么都不用做
            self.engine_var.set(ENGINE_CHOICE_XESS)
        self._sync_engine_widgets()
        self._render_detail()

    def _on_wrap(self, evt) -> None:
        """实验性那段警告跟着窗口宽度换行（只在宽度真的变了时才写，免得来回触发）。"""
        w = max(320, int(evt.width) - 40)
        if w != self._wrap_w:
            self._wrap_w = w
            try:
                self.dlss5_note.configure(wraplength=w)
            except tk.TclError:
                pass

    def _on_frames_change(self, _evt=None) -> None:
        """倍率变了 → 刷新说明文字。"""
        self._sync_profile_widgets()

    def _current_engine(self) -> tuple[str, str]:
        """返回 (引擎, 引擎包 key)。"""
        return self._engine_map.get(self.engine_var.get(), (state.ENGINE_DLSSG, ""))

    def _sync_engine_widgets(self) -> None:
        """按当前引擎把「率」「路由」等控件切到对应的语义。

        DLSSG 的倍率是 MaxGeneratedFrames（上限，游戏说了算）；
        OptiScaler 的倍率是我们直接下发给 XeFG 的插值帧数（我们说了算）。
        两者含义不同，所以选项列表必须分开，不能共用一套。
        """
        engine, bundle = self._current_engine()
        is_opti = engine == state.ENGINE_OPTISCALER

        # 倍率控件：两个引擎共用这一个下拉框，但取值完全不同
        try:
            if is_opti:
                opts = [f"{m}X  {lbl.split('（')[1][:-1] if '（' in lbl else ''}".strip()
                        for m, lbl in optiscaler.multiplier_options()]
                # 用简单稳定的文本，便于反查
                opts = [f"{m}X" for m, _l in optiscaler.multiplier_options()]
                self.frames_combo.configure(values=opts)
                if self.frames_var.get() not in opts:
                    self.frames_var.set("4X" if "4X" in opts else opts[0])
            else:
                prof = profiles.for_router(self._current_router())
                opts = installer.frame_options_for(prof)
                self.frames_combo.configure(values=opts)
                if self.frames_var.get() not in opts:
                    self.frames_var.set(opts[0])
        except tk.TclError:
            pass

        # OptiScaler 不区分计算路由 / 采样档 —— 那两个是 DLSSG payload 的概念
        for w in (self.router_combo, self.proxy_combo, self.sample_combo):
            try:
                w.configure(state="disabled" if is_opti else "readonly")
            except tk.TclError:
                pass
        # 「只显示能开启帧生成的」只在 DLSSG 引擎下有意义 ——
        # XeSS 引擎不看游戏自带帧生成，留着这个勾选框会让人以为过滤掉的是不能用的
        try:
            self.filter_check.configure(
                state="disabled" if is_opti else "normal")
        except tk.TclError:
            pass
        # HighResMV 是 OptiScaler 专有开关，DLSSG 引擎下无意义
        try:
            self.hiresmv_check.configure(state="normal" if is_opti else "disabled")
        except tk.TclError:
            pass
        # 共存模式同理：它描述的是 OptiScaler 引擎怎么和 DLSSG 引擎搭配
        try:
            self.coexist_check.configure(state="normal" if is_opti else "disabled")
        except tk.TclError:
            pass
        # 「只做神经网络渲染」只有 DLSS 5 包才有意义 —— 别的包关掉帧生成等于什么都不做
        try:
            self.nofg_check.configure(
                state="normal" if (is_opti and bundle == "optiscaler-dlss5") else "disabled")
        except tk.TclError:
            pass
        # 实验性区块：勾选框状态始终跟着当前引擎走，那段警告只在真的选中
        # DLSS 5 包时摆出来（平时收起来，不占地方）
        is_dlss5 = is_opti and bundle == "optiscaler-dlss5"
        try:
            self.dlss5_var.set(is_dlss5)
            if is_dlss5:
                self.dlss5_warn.grid()
            else:
                self.dlss5_warn.grid_remove()
        except tk.TclError:
            pass
        # 高级页最上面那几项只作用于 DLSSG 引擎，另一种引擎下置灰并标注原因
        try:
            self.advanced_hint.configure(
                text=("计算路由、代理入口、采样档、日志级别仅作用于 DLSSG 引擎"
                      "（当前引擎为 OptiScaler，这几项已置灰）。"
                      if is_opti else
                      "计算路由、代理入口、采样档、日志级别仅作用于 DLSSG 引擎。")
            )
        except tk.TclError:
            pass

        # 引擎说明：只陈述该引擎写入什么、由谁决定倍率
        if is_opti:
            spec = optiscaler.get_bundle(bundle)
            ok, msg = optiscaler.bundle_available(bundle)
            if spec is None:
                self.engine_hint.configure(text="该引擎包不可用", foreground=C_ERR)
            elif not ok:
                self.engine_hint.configure(text=f"引擎包不完整：{msg}", foreground=C_ERR)
            elif bundle == "optiscaler-dlss5":
                self.engine_hint.configure(
                    text="实验性引擎：DLSS 5 神经网络渲染。警告见「高级」页。",
                    foreground=C_WARN,
                )
            else:
                self.engine_hint.configure(
                    text=f"{spec.display_name}　倍率由本工具设定，不依赖游戏提供的选项。",
                    foreground=C_DIM,
                )
        else:
            self.engine_hint.configure(
                text="DLSSG：NVIDIA DLSS 帧生成。按显卡路由选择上游版本。",
                foreground=C_DIM,
            )

        self._sync_profile_widgets()

    def _current_router(self) -> str:
        rsel = self.router_var.get()
        if rsel.startswith("自动"):
            return self.env.router if self.env else "SM86"
        return "SM75" if "SM75" in rsel else "SM86"

    def _sync_profile_widgets(self) -> None:
        """按当前路由（DLSSG）或当前引擎包（OptiScaler）刷新说明与可选项。"""
        engine, bundle = self._current_engine()

        if engine == state.ENGINE_OPTISCALER:
            mult = self._current_multiplier()
            lines = [
                f"将以 {mult}X 安装。倍率由本工具下发给 XeSS 帧生成，",
                "倍率的取值由本工具决定，与游戏是否提供倍率选项无关。",
            ]
            if mult > optiscaler.TEMPLATE_MAX_MULT:
                lines.append(
                    f"⚠ {mult}X 超出该构建自带模板写明的档位（最高 "
                    f"{optiscaler.TEMPLATE_MAX_MULT}X），靠运行时补丁解锁，可能无效或画质异常。"
                )
            lines.append("注意：本引擎与 DLSSG 引擎互斥，同一游戏目录只能装一个。")
            try:
                self.frames_hint.configure(
                    text="\n".join(lines),
                    foreground=C_WARN if mult > optiscaler.TEMPLATE_MAX_MULT else C_DIM,
                )
            except tk.TclError:
                pass
            return

        router = self._current_router()
        prof = profiles.for_router(router)

        # 代理入口
        opts = ["自动选择"] + list(prof.proxy_names)
        try:
            self.proxy_combo.configure(values=opts)
            if self.proxy_var.get() not in opts:
                self.proxy_var.set("自动选择")
        except tk.TclError:
            pass

        # 倍率
        fopts = installer.frame_options_for(prof)
        try:
            self.frames_combo.configure(values=fopts)
            if self.frames_var.get() not in fopts:
                self.frames_var.set(fopts[0])
        except tk.TclError:
            pass

        # 说明文字
        hint = f"将使用上游 {prof.version}（{prof.explanation}）"
        hint += (
            "\n「最高倍率」只是允许的上限 —— 实际用几倍由游戏决定。"
            "游戏只有「开/关」没有倍率选项的话（黑神话就是），它固定按 2X 跑，改上限不会有变化。"
        )
        if router == "SM75":
            hint += "\n⚠ 20 系为实验性支持：上游 0.3.0 已移除 SM75 内核，本工具自动改用 0.2.4。"
        hint += "\n注意：本引擎与 OptiScaler（XeSS）引擎互斥，同一游戏目录只能装一个。"
        try:
            self.frames_hint.configure(
                text=hint, foreground=C_WARN if router == "SM75" else C_DIM
            )
        except tk.TclError:
            pass

    def _current_multiplier(self) -> int:
        """从倍率下拉框反查数字。"""
        raw = (self.frames_var.get() or "").strip()
        digits = "".join(ch for ch in raw if ch.isdigit())
        try:
            return optiscaler.normalize_multiplier(int(digits))
        except ValueError:
            return 4

    def _on_preset_change(self, _evt=None) -> None:
        """切换扫描档位后重扫当前游戏。"""
        if self.selected is not None:
            self.rescan_current()

    def rescan_current(self) -> None:
        g = self.selected
        if g is None:
            return
        preset = self.preset_var.get()

        def work():
            games.inspect_game(g, preset=preset)
            return g

        def done(_g):
            self._render_detail()
            self._set_status(f"已按「{preset}」档重新扫描")

        self._run(work, done, status=f"正在按「{preset}」档扫描 {g.name} …")

    def _render_prediction(self, pred) -> None:
        """把「这个游戏能不能开帧生成」的判定显示出来（DLSSG 引擎的判据）。"""
        if pred is None:
            return
        icon = {
            capability.Support.GOOD: "✓",
            capability.Support.MAYBE: "·",
            capability.Support.UNLIKELY: "⚠",
            capability.Support.NO: "✗",
        }.get(pred.level, "·")
        self.detail.insert("end", f"\n{icon} {pred.headline}\n", pred.level.color)
        for r in pred.reasons:
            self.detail.insert("end", f"　　{r}\n", "dim")
        # 说明性文字（旧字段名 advice）只作为事实补充列出，不加「建议」之类的抬头
        note = getattr(pred, "advice", "") or ""
        for ln in note.splitlines():
            if ln.strip():
                self.detail.insert("end", f"　　{ln}\n", "dim")

    def _on_select(self, _evt=None) -> None:
        sel = self.listbox.curselection()
        cur = self._current_list()
        if not sel or sel[0] >= len(cur):
            return
        self.selected = cur[sel[0]]
        self._render_detail()
        self._refresh_installed_label()

    def browse_game(self) -> None:
        path = filedialog.askdirectory(title="选择游戏文件夹（或游戏主程序所在目录）")
        if not path:
            return
        self._add_manual(Path(path))

    def browse_exe(self) -> None:
        """直接指定游戏主程序 EXE —— 自动扫描不准时的兜底手段。"""
        path = filedialog.askopenfilename(
            title="选择游戏主程序（渲染 EXE）",
            filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")],
        )
        if not path:
            return
        exe = Path(path)
        self._log(f"手动指定主程序：{exe}")

        def work():
            cand = games.analyse_one(exe)
            return cand

        def done(cand: games.ExeCandidate):
            g = games.Game(
                name=exe.parent.name or exe.stem,
                root=exe.parent,
                source="手动指定 EXE",
                appid="",
                candidates=[cand],
            )
            self.game_list.insert(0, g)
            self.filter_var.set(False)
            self._apply_filter()
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(0)
            self._on_select()

            if not cand.is_x64:
                messagebox.showwarning(
                    "这不是 64 位程序",
                    f"{exe.name} 是 {cand.api_label} 之外的架构，本 Mod 需要 64 位程序。",
                )
            elif not cand.eligible:
                messagebox.showwarning(
                    "这个主程序可能不合适",
                    f"{exe.name}\n\n{cand.api_label}\n\n"
                    + "\n".join(cand.api_counter or cand.reasons[:5])
                    + "\n\n若游戏确实支持帧生成，则所选 EXE 可能不对 —— "
                    "需要的是真正渲染画面的那个（通常在 Binaries\\Win64 之类目录里）。",
                )
            else:
                self._log(f"✓ {exe.name}：{cand.api_label}，可以用", "ok")

        self._run(work, done, status=f"正在分析 {exe.name} …")

    def _add_manual(self, path: Path) -> None:
        def work():
            g = games.Game(name=path.name, root=path, source="手动选择", appid="")
            games.inspect_game(g)
            return g

        def done(g: games.Game):
            self.game_list.insert(0, g)
            self.filter_var.set(False)
            self._apply_filter()
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(0)
            self._on_select()
            if not g.candidates:
                messagebox.showwarning(
                    "没找到游戏主程序",
                    "这个文件夹里没有找到符合条件的主程序。\n"
                    "请选择游戏 EXE 所在的目录（例如 …\\b1\\Binaries\\Win64）。",
                )

        self._run(work, done, status=f"正在分析 {path.name} …")

    def _render_detail(self) -> None:
        g = self.selected
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if g is None:
            self.detail.insert("end", "请在左边选一个游戏。\n", "dim")
            self.detail.configure(state="disabled")
            return

        self.detail.insert("end", f"{g.name}\n", "h")
        self.detail.insert("end", f"来源 {g.source}　目录 {g.root}\n", "dim")
        self.detail.insert("end", "\n")

        cands = list(g.candidates)
        self.candidates = cands
        elig = [c for c in cands if c.eligible]

        if not cands:
            self.detail.insert("end", "✗ 没有找到可执行程序\n", "err")
            self.prediction = None
        elif not elig:
            b = cands[0]
            # XeSS 引擎下不摆"这游戏有没有帧生成"的判断 —— 那是 DLSSG 引擎的判据
            self.prediction = (
                capability.predict(b.name, b.directory, b.api_level, False, g.name)
                if self._list_only_fg() else None
            )
            self._render_prediction(self.prediction)
            self.detail.insert("end", f"\n　主程序 {b.name}\n", "mono")
            for r in b.reasons[:5]:
                self.detail.insert("end", f"　　· {r}\n", "dim")
        else:
            b = max(elig, key=lambda c: c.score)
            self.chosen_exe = b.path
            self.detail.insert("end", f"✓ 已定位主程序（{b.confidence}）\n", "ok")
            self.detail.insert("end", f"　{b.path}\n", "mono")

            # 能力预判：这是评论区「装了才发现游戏没有帧生成」的对策。
            # 只对 DLSSG 引擎显示 —— XeSS 引擎不依赖游戏自带帧生成。
            self.prediction = (
                capability.predict(b.name, b.directory, b.api_level, b.eligible, g.name)
                if self._list_only_fg() else None
            )
            self._render_prediction(self.prediction)

            self.detail.insert("end", "\n判断依据：\n", "dim")
            for r in b.reasons:
                tag = "dim"
                if any(k in r for k in ("D3D12（已确认", "dlssg", "DLSS 帧生成")):
                    tag = "ok"
                elif r.startswith("（反证）") or "反证" in r:
                    tag = "dim"
                elif "仅 D3D11" in r or "Vulkan" in r:
                    tag = "warn"
                self.detail.insert("end", f"　· {r}\n", tag)
            self.detail.insert("end", f"\n安装目录：{b.directory}\n", "mono")

        # 反作弊
        self.anticheat = ac.scan(g.root)
        if self.anticheat.risky:
            self.detail.insert("end", f"\n⚠ 反作弊：{self.anticheat.summary}\n", "warn")
            self.detail.insert("end", "　带反作弊的游戏注入 DLL 有封号风险，本工具默认拒绝安装。\n", "warn")
        else:
            self.detail.insert("end", "\n✓ 未检测到反作弊组件\n", "ok")

        target_dir = Path(self.chosen_exe).parent if self.chosen_exe else None
        vr = installer.verify_any(target_dir) if target_dir else None
        if vr and vr.installed:
            rec = vr.record or {}
            which = installer.engine_of(rec)
            if vr.healthy:
                if which == state.ENGINE_OPTISCALER:
                    detail = (f"引擎 {installer.engine_label(which)}，"
                              f"倍率 {rec.get('multiplier', '—')}X，入口 {rec.get('proxy') or '—'}")
                else:
                    detail = f"Router={rec.get('router') or '—'}，入口 {rec.get('proxy') or '—'}"
                self.detail.insert("end", f"\n✓ 已安装（{detail}）\n", "ok")
            else:
                self.detail.insert("end", "\n⚠ 已安装，但文件校验不通过\n", "warn")
            for c in vr.details:
                if c.level != "ok":
                    self.detail.insert("end", f"　[{c.level}] {c.title}\n", "warn" if c.level == "warn" else "err")
        else:
            self.detail.insert("end", "\n· 当前状态：未安装\n", "dim")

        self.detail.configure(state="disabled")

        values = [str(c.path) for c in sorted(elig, key=lambda c: -c.score)] if elig else []
        self.exe_combo.configure(values=values)
        if values:
            self.exe_var.set(values[0])
        else:
            self.exe_var.set("")

    def _on_exe_change(self, _evt=None) -> None:
        p = self.exe_var.get()
        if p:
            self.chosen_exe = Path(p)

    def _refresh_installed_label(self) -> None:
        inst = installer.all_installs()
        if inst:
            n_opti = sum(1 for i in inst if installer.engine_of(i) == state.ENGINE_OPTISCALER)
            extra = f"（其中 {n_opti} 个是 XeSS 引擎）" if n_opti else ""
            self.installed_label.configure(
                text=f"本工具已为 {len(inst)} 个游戏装过{extra}（可随时卸载还原）",
                foreground=C_OK,
            )
        else:
            self.installed_label.configure(text="尚未安装过任何游戏", foreground=C_DIM)

    # ------------------------------------------------------------------
    # 参数收集
    # ------------------------------------------------------------------

    def _collect(self):
        if self.selected is None:
            messagebox.showinfo("未选择游戏", "左侧列表未选中任何游戏。")
            return None
        if not self.chosen_exe:
            messagebox.showwarning("无法安装", "没有定位到可用的游戏主程序。")
            return None
        target_dir = Path(self.chosen_exe).parent

        sel = self.proxy_var.get()
        if sel == "自动选择":
            proxy, why = installer.auto_pick_proxy(target_dir)
        else:
            proxy, why = sel, "手动指定"
        if not proxy.lower().endswith(".dll"):
            proxy += ".dll"

        rsel = self.router_var.get()
        if rsel.startswith("自动"):
            router = self.env.router if self.env else "SM86"
        else:
            router = "SM75" if "SM75" in rsel else "SM86"

        # 按路由定 profile —— SM75 只能走 0.2.4
        prof = profiles.for_router(router)

        # 倍率不能超过该 profile 的上限
        frames = installer.frame_option_to_value(self.frames_var.get(), 3)
        frames = max(1, min(prof.max_frames, frames))

        # 入口必须是该 profile 支持的
        if proxy not in prof.proxy_names:
            proxy, why = installer.auto_pick_proxy(target_dir, profile=prof)

        bilinear = 1 if self.sample_var.get().startswith("性能") else 0
        # 日志级别按括号里的数字取，不按文案匹配 ——
        # 文案改过一次就踩过坑：标签写了「信息」而解析还在找「运行」，
        # 结果选 2/3 级静默退回到 1 级，日志里什么都看不到。
        m = re.search(r"Level=(\d+)", self.loglv_var.get())
        lvl = int(m.group(1)) if m else 1
        return target_dir, proxy, why, router, bilinear, lvl, frames

    def _preflight_or_none(self, target_dir, proxy, router, quiet=False):
        acr = ac.scan(self.selected.root)
        pf = installer.preflight(
            target_dir, self.chosen_exe, proxy, router,
            env=self.env, game_name=self.selected.name, anticheat=acr,
        )
        if not quiet:
            self._log("—" * 30)
            for c in pf.checks:
                tag = {"ok": "dim", "warn": "warn", "error": "err"}.get(c.level, "")
                self._log(f"[{c.level.upper()}] {c.title}" + (f" — {c.detail.splitlines()[0]}" if c.detail else ""), tag)
        return pf

    # ------------------------------------------------------------------
    # OptiScaler 引擎（XeSS / DLSS 5）
    # ------------------------------------------------------------------

    def _opti_target(self) -> Path | None:
        if self.selected is None:
            messagebox.showinfo("未选择游戏", "左侧列表未选中任何游戏。")
            return None
        if not self.chosen_exe:
            messagebox.showwarning("无法安装", "没有定位到可用的游戏主程序。")
            return None
        return Path(self.chosen_exe).parent

    def _opti_coexist(self) -> bool:
        """是否勾了「与 DLSSG 引擎共存」。"""
        try:
            return bool(self.coexist_var.get())
        except (AttributeError, tk.TclError):
            return False

    def _opti_fg_input(self) -> str:
        """帧生成输入源。

        共存模式下自动用 dlssg —— 那时 DLSSG 引擎在位，游戏自身的 DLSS 帧生成
        通道是真的，用 dlssg 输入能绕开「运动矢量与深度分辨率不一致」。
        其余场合用 upscaler（游戏只有超分的场景）。
        """
        return "dlssg" if self._opti_coexist() else "upscaler"

    def _fg_enabled(self) -> bool:
        """本引擎（OptiScaler）自己的帧生成要不要开。

        关掉它 = 只留 DLSS 5 神经网络渲染，帧生成交给引擎一（DLSSG）。
        这是"全程只有一个帧生成器"的组合：实测两个帧生成器同时工作时，
        OptiScaler 算出来的帧进不了画面（计数器在涨、观感是原生帧率）。
        """
        try:
            return not bool(self.nofg_var.get())
        except (AttributeError, tk.TclError):
            return True

    def _opti_preflight(self, target_dir: Path, bundle: str, quiet: bool = False):
        acr = ac.scan(self.selected.root)
        checks = optiscaler.preflight(
            target_dir, bundle, self._current_multiplier(),
            running_names=[self.chosen_exe.name] if self.chosen_exe else [],
            anticheat=acr,
            fg_input=self._opti_fg_input(),
            coexist=self._opti_coexist(),
            fg_enabled=self._fg_enabled(),
        )
        if not quiet:
            self._log("—" * 30)
            for c in checks:
                tag = {"ok": "dim", "warn": "warn", "error": "err"}.get(c.level, "")
                self._log(f"[{c.level.upper()}] {c.title}"
                          + (f" — {c.detail.splitlines()[0]}" if c.detail else ""), tag)
        return checks

    def _opti_preview(self, bundle: str) -> None:
        target_dir = self._opti_target()
        if target_dir is None:
            return
        checks = self._opti_preflight(target_dir, bundle)
        plan = optiscaler.make_plan(target_dir, self.chosen_exe, bundle,
                                    self._current_multiplier(),
                                    game_name=self.selected.name,
                                    high_res_mv=bool(self.hiresmv_var.get()),
                                    fg_input=self._opti_fg_input(),
                                    fg_enabled=self._fg_enabled())
        spec = optiscaler.get_bundle(bundle)
        lines = [
            f"安装目录：{target_dir}",
            f"引擎包：{spec.display_name}",
            f"倍率：{self._current_multiplier()}X",
            f"帧生成输入源：{self._opti_fg_input()}"
            + ("（共存模式）" if self._opti_coexist() else ""),
            f"本引擎帧生成：{'开' if self._fg_enabled() else '关（只做神经网络渲染）'}",
            f"代理入口：{plan.proxy}",
            "",
        ]
        for it in plan.items:
            act = {"copy": "写入", "skip": "跳过（已一致）",
                   "remove": "删除"}.get(it.action, it.action)
            lines.append(f"[{act}] {it.rel}　{it.note}")
        for rel in plan.cleanup:
            lines.append(f"[清理] {rel}（上次装在别的入口名下）")

        self._log("—" * 30)
        self._log("预览（未做任何改动）：", "warn")
        for ln in lines:
            self._log("   " + ln)

        errors = [c for c in checks if c.level == "error"]
        if errors:
            messagebox.showerror(
                "预检未通过",
                "\n".join(f"· {c.title}\n  {c.detail}" for c in errors),
            )
        else:
            messagebox.showinfo(
                "预览完成",
                "预检全部通过。将要写入：\n\n" + "\n".join(lines[:8])
                + "\n\n点「一键安装」开始（写入前会先备份原文件）。",
            )

    def _opti_install(self, bundle: str) -> None:
        target_dir = self._opti_target()
        if target_dir is None:
            return
        checks = self._opti_preflight(target_dir, bundle)
        errors = [c for c in checks if c.level == "error"]
        if errors:
            detail = "\n".join(f"· {c.title}\n  {c.detail}" for c in errors)
            self._log("预检未通过，已中止", "err")
            if any("写入权限" in c.title for c in errors):
                if messagebox.askyesno("需要管理员权限", f"{detail}\n\n要以管理员身份重新启动本工具吗？"):
                    self._relaunch_as_admin()
                return
            messagebox.showerror("预检未通过", detail)
            return

        spec = optiscaler.get_bundle(bundle)
        mult = self._current_multiplier()
        warn = "\n".join(f"· {c.title}" for c in checks if c.level == "warn")
        msg = (
            f"游戏：{self.selected.name}\n"
            f"安装目录：{target_dir}\n"
            f"引擎包：{spec.display_name}\n"
            f"倍率：{mult}X\n"
            f"代理入口：{optiscaler.pick_proxy(target_dir, bundle)[0]}\n\n"
            "本引擎会写入多个文件（含子目录），覆盖前会先备份原文件。\n"
            "这些文件全部登记在册，点「卸载并还原」会逐个删除并还原原文件。\n\n"
            "注意：本引擎与 DLSSG 引擎互斥，装了这个就不能再装那个。\n"
        )
        if warn:
            msg += "\n提醒：\n" + warn + "\n"
        msg += "\n确认开始安装吗？"
        if not messagebox.askyesno("确认安装", msg, icon="warning"):
            self._log("安装已取消")
            return
        self._opti_install_now(target_dir, bundle)

    def _opti_install_now(self, target_dir: Path, bundle: str) -> None:
        mult = self._current_multiplier()
        hires = bool(self.hiresmv_var.get())
        game = self.selected.name

        def work():
            return optiscaler.install(target_dir, self.chosen_exe, bundle, mult,
                                      game_name=game, high_res_mv=hires,
                                      fg_input=self._opti_fg_input(),
                                      fg_enabled=self._fg_enabled())

        def done(res):
            if not res.success:
                self._log(res.message, "err")
                messagebox.showerror("安装失败（已自动回滚）", res.message
                                     + "\n\n游戏目录已恢复到安装前的状态，未做任何残留改动。")
            else:
                self._log(res.message, "ok")
                for n in res.installed:
                    self._log(f"   已写入 {target_dir / n}", "ok")
                if res.backup_dir:
                    self._log(f"   原文件备份：{res.backup_dir}", "dim")
                vr = optiscaler.verify_installed(target_dir)
                self._log("   体检：" + ("完好" if vr.healthy else "异常"),
                          "ok" if vr.healthy else "err")
                self._render_detail()
                self._refresh_installed_label()
                extra = ""
                if bundle == "optiscaler-dlss5":
                    extra = ("\n\nDLSS 5 神经网络渲染是实验性的：如果画面异常或游戏起不来，"
                             "直接点「卸载并还原」即可完全恢复。")
                messagebox.showinfo(
                    "安装成功",
                    f"{res.message}\n\n现在启动游戏，按 Insert 打开 OptiScaler 菜单，"
                    f"确认 Frame Generation 已开启（输出应为 XeFG）。{extra}",
                )
            self._set_status("就绪")

        self._run(work, done, status="正在安装（备份 → 写入 → 校验）…")

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------

    def do_preview(self) -> None:
        engine, bundle = self._current_engine()
        if engine == state.ENGINE_OPTISCALER:
            self._opti_preview(bundle)
            return
        got = self._collect()
        if not got:
            return
        target_dir, proxy, why, router, bilinear, lvl, frames = got
        pf = self._preflight_or_none(target_dir, proxy, router)
        plan = installer.make_plan(
            target_dir, self.chosen_exe, proxy, router,
            game_name=self.selected.name, hardware_bilinear=bilinear,
            max_generated_frames=frames, log_level=lvl,
        )
        lines = [
            f"安装目录：{target_dir}",
            f"代理入口：{proxy}（{why}）",
            f"计算路由：Router={router}",
            f"最高倍率：{self.frames_var.get()}",
            "",
        ]
        for it in plan.items:
            act = {installer.ACT_COPY: "写入", installer.ACT_SKIP: "跳过（已一致）",
                   installer.ACT_REMOVE: "删除"}.get(it.action, it.action)
            lines.append(f"[{act}] {it.dst.name}　{it.note}")
        lines.append("")
        lines.append("生成的 dlssg_sm86.ini：")
        lines.append(plan.ini_text)
        self._log("—" * 30)
        self._log("预览（未做任何改动）：", "warn")
        for ln in lines:
            self._log("   " + ln)
        blocker = "存在阻断问题，不能安装：\n\n" + "\n".join(f"· {c.title}\n  {c.detail}" for c in pf.errors)
        if pf.errors:
            messagebox.showerror("预检未通过", blocker)
        else:
            messagebox.showinfo(
                "预览完成",
                "预检全部通过。将要做的改动：\n\n" + "\n".join(lines[:6]) +
                "\n\n点「一键安装」开始（写入前会先备份原文件）。",
            )

    def do_install(self) -> None:
        engine, bundle = self._current_engine()
        if engine == state.ENGINE_OPTISCALER:
            self._opti_install(bundle)
            return
        got = self._collect()
        if not got:
            return
        target_dir, proxy, why, router, bilinear, lvl, frames = got
        pf = self._preflight_or_none(target_dir, proxy, router)

        # 20 系是实验性路由 —— 上游 0.3.0 起明确这么定位，必须先提醒
        if router == "SM75":
            if not messagebox.askyesno(
                "20 系显卡：实验性支持",
                "RTX 20 系列走的是 SM75 实验性路由。\n\n"
                "上游从 0.3.0 起把它标注为实验性质，且没有在实体 Turing 显卡上"
                "完成验证。\n"
                "可能的症状：画面闪烁、拖影、游戏闪退。\n\n"
                "（不过 2070 笔记本实测是正常的，所以多数情况下应该没问题）\n\n"
                "要继续吗？出问题时可以随时「卸载并还原」，不会损坏游戏。",
                icon="warning",
            ):
                self._log("已在 20 系提示处取消")
                return

        # 帧生成硬性前置条件：HAGS 关闭时游戏一定会说"显卡不支持"
        if self.env is not None and self.env.hags is False:
            if messagebox.askyesno(
                "硬件加速 GPU 计划未开启",
                "检测到「硬件加速 GPU 计划」（HAGS）处于关闭状态。\n\n"
                "这是 DLSS 帧生成的硬性系统前置条件。不开启的话，"
                "即使 Mod 正确安装，游戏也会提示\n"
                "「您的显卡不支持 DLSS 帧生成技术」。\n\n"
                "是否现在开启？（需要管理员权限，修改后需重启电脑）\n\n"
                "选「否」会继续安装，但游戏内很可能仍然无法开启帧生成。",
                icon="warning",
            ):
                self.do_enable_hags()
                return

        # 能力预判：游戏大概率没有帧生成功能时，先问一句值不值得试
        if self.prediction is not None and self.prediction.level == capability.Support.UNLIKELY:
            if not messagebox.askyesno(
                "这个游戏可能没有帧生成功能",
                f"{self.prediction.headline}\n\n"
                + "\n".join(self.prediction.reasons)
                + f"\n\n{self.prediction.advice}\n\n"
                "还要继续安装吗？",
                icon="warning",
            ):
                self._log("已在兼容性提示处取消")
                return

        if not pf.ok:
            detail = "\n".join(f"· {c.title}\n  {c.detail}" for c in pf.errors)
            self._log("预检未通过，已中止", "err")
            if any("写入权限" in c.title for c in pf.errors):
                if messagebox.askyesno(
                    "需要管理员权限",
                    f"{detail}\n\n要以管理员身份重新启动本工具吗？",
                ):
                    self._relaunch_as_admin()
                return
            if any("反作弊" in c.title for c in pf.errors):
                if messagebox.askyesno(
                    "检测到反作弊组件",
                    f"{detail}\n\n给带反作弊的游戏注入 DLL 可能导致账号被封。\n"
                    "确认继续？",
                    icon="warning", default="no",
                ):
                    self._install_now(target_dir, proxy, router, bilinear, lvl, frames, force=True)
                return
            messagebox.showerror("预检未通过", detail)
            return

        warn = "\n".join(f"· {c.title}" for c in pf.warnings)
        msg = (
            f"游戏：{self.selected.name}\n"
            f"安装目录：{target_dir}\n"
            f"代理入口：{proxy}（{why}）\n"
            f"计算路由：Router={router}\n"
            f"最高倍率：{self.frames_var.get()}\n\n"
            "本工具只会在这个目录里写入 2 个文件，覆盖前会先备份原文件。\n"
            "随时可以点「卸载并还原」恢复原状。\n"
        )
        if warn:
            msg += "\n提醒：\n" + warn
        msg += "\n确认开始安装吗？"
        if not messagebox.askyesno("确认安装", msg):
            self._log("安装已取消")
            return
        self._install_now(target_dir, proxy, router, bilinear, lvl, frames)

    def _install_now(self, target_dir, proxy, router, bilinear, lvl, frames=3, force=False):
        def work():
            plan = installer.make_plan(
                target_dir, self.chosen_exe, proxy, router,
                game_name=self.selected.name, hardware_bilinear=bilinear,
                max_generated_frames=frames, log_level=lvl,
            )
            return installer.execute_plan(plan)

        def done(res: installer.InstallResult):
            if not res.success:
                self._log(res.message, "err")
                messagebox.showerror("安装失败（已自动回滚）", res.message +
                                     "\n\n游戏目录已恢复到安装前的状态，未做任何残留改动。")
            else:
                self._log(res.message, "ok")
                for n in res.installed:
                    self._log(f"   已写入 {target_dir / n}", "ok")
                if res.backup_dir:
                    self._log(f"   原文件备份：{res.backup_dir}", "dim")
                vr = installer.verify(target_dir)
                self._log("   体检：" + ("完好" if vr.healthy else "异常"), "ok" if vr.healthy else "err")
                self._render_detail()
                self._refresh_installed_label()
                messagebox.showinfo(
                    "安装成功",
                    f"{res.message}\n\n现在启动游戏，在画面设置里打开「帧生成 / Frame Generation」。\n"
                    "如果游戏里没有这个选项，请把日志级别改成「运行诊断」重装一次，"
                    "再看游戏目录下的 dlssg_sm86\\logs。",
                )
            self._set_status("就绪")

        self._run(work, done, status="正在安装（备份 → 写入 → 校验）…")

    def do_uninstall(self) -> None:
        if self.selected is None or not self.chosen_exe:
            messagebox.showinfo("未选择游戏", "左侧列表未选中任何游戏。")
            return
        target_dir = Path(self.chosen_exe).parent
        vr = installer.verify_any(target_dir)
        if not vr.installed:
            messagebox.showinfo("没有安装记录", f"本工具没有在\n{target_dir}\n安装过东西。")
            return
        rec = vr.record or {}
        which = installer.engine_of(rec) if rec else ""
        warn = ""
        if any(c.level == "warn" and "正在运行" in c.title for c in vr.details):
            warn = "\n⚠ 游戏正在运行：文件被占用会导致写入或删除失败。\n"
        if not messagebox.askyesno(
            "确认卸载",
            f"将从下面这个目录移除本工具的文件，并还原安装前的原始文件：\n\n{target_dir}\n"
            f"引擎：{installer.engine_label(which) if which else '（未知）'}\n"
            f"{warn}\n安装时间：{rec.get('installed_at', '—')}\n\n继续吗？",
        ):
            return

        def work():
            return installer.uninstall_any(target_dir)

        def done(res):
            self._log(res.message, "ok" if res.success else "err")
            if getattr(res, "restored", None):
                self._log(f"   已还原：{'、'.join(res.restored)}", "ok")
            self._render_detail()
            self._refresh_installed_label()
            self._set_status("就绪")
            messagebox.showinfo("卸载完成", res.message)

        self._run(work, done, status="正在卸载并还原…")

    def do_enable_hags(self) -> None:
        """开启硬件加速 GPU 计划 —— 只在用户明确同意后才动这一项系统设置。"""
        enabled, raw = winenv.read_hags()
        if enabled:
            messagebox.showinfo("无需操作", "硬件加速 GPU 计划已经处于开启状态。")
            return

        if not winenv.is_admin():
            if messagebox.askyesno(
                "需要管理员权限",
                "「硬件加速 GPU 计划」是系统级设置，修改它需要管理员权限。\n\n"
                "要以管理员身份重新启动本工具吗？（重启工具后请再点一次这个按钮）",
            ):
                self._relaunch_as_admin()
            return

        if not messagebox.askyesno(
            "确认修改系统设置",
            "本操作只会修改注册表里的一个值：\n\n"
            "  HKEY_LOCAL_MACHINE\\SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers\n"
            "  HwSchMode : 1 → 2\n\n"
            "等同于「设置 → 系统 → 屏幕 → 显示卡 → 更改默认图形设置」中的\n"
            "打开「硬件加速 GPU 计划」是完全同一件事，不涉及任何其他系统设置。\n\n"
            "⚠ 改完必须重启电脑才会生效。\n\n确认修改吗？",
            icon="warning",
        ):
            self._log("系统设置修改已取消")
            return

        ok, msg = winenv.set_hags(True)
        if ok:
            self._log(msg, "ok")
            messagebox.showinfo(
                "已开启，请重启电脑",
                msg + "\n\n重启后回来点「重新检测环境」，确认这一项变成已开启。",
            )
        else:
            self._log(f"开启失败：{msg}", "err")
            messagebox.showerror("开启失败", msg)
        self.refresh_env()

    def do_selftest(self) -> None:
        def work():
            from .selftest import run

            return run(verbose=False)

        def done(r):
            self._log("—" * 30)
            self._log("内置安全自检：", "warn")
            for status, name, detail in r.rows:
                tag = "ok" if status == "PASS" else "err"
                self._log(f"   [{status}] {name}" + (f"\n            {detail}" if status == "FAIL" and detail else ""), tag)
            summary = f"{r.passed} 项通过 / {r.failed} 项失败"
            self._log(f"   自检结论：{summary}", "ok" if r.failed == 0 else "err")
            self._set_status("自检完成")
            if r.failed == 0:
                messagebox.showinfo(
                    "安全自检通过",
                    f"全部 {r.passed} 项检查通过。\n\n"
                    "验证内容包括：只写入预期的 2 个文件、覆盖前必定备份、\n"
                    "卸载后逐字节还原、写入失败必定整体回滚、\n"
                    "以及反作弊/进程运行/权限不足等风险一律拦下。",
                )
            else:
                messagebox.showerror("自检未通过", f"有 {r.failed} 项失败，详见日志区。")

        self._run(work, done, status="正在运行内置安全自检…")

    def do_report(self) -> None:
        def work():
            from . import report

            env = self.env or gpu.detect()
            return report.write_report(env, reports_root())

        def done(p: Path):
            self._log(f"环境报告已生成：{p}", "ok")
            try:
                os.startfile(str(p.parent))  # noqa: S606
            except Exception:
                pass
            messagebox.showinfo("已生成", f"环境报告已保存到：\n{p}")

        self._run(work, done, status="正在生成环境报告…")

    def _relaunch_as_admin(self) -> None:
        try:
            params = " ".join(f'"{a}"' for a in sys.argv[1:])
            if getattr(sys, "frozen", False):
                exe, args = sys.executable, params
            else:
                exe = sys.executable
                args = f'"{Path(sys.argv[0]).resolve()}" {params}'
            rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 1)
            if rc > 32:
                self.destroy()
            else:
                messagebox.showerror("提权失败", f"无法以管理员身份启动（代码 {rc}）。")
        except Exception as exc:
            messagebox.showerror("提权失败", str(exc))


def launch() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(launch())
