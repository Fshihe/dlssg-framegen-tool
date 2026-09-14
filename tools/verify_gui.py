"""GUI 冒烟测试：真的把窗口建起来，选中游戏，检查详情面板渲染不崩。

GUI 代码改过之后光靠 py_compile 不够 —— 这里会真正实例化 Tk 窗口、
走一遍 _render_detail / _render_prediction，把渲染出来的文本打出来看。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAIL
    if not cond:
        FAIL += 1
    print(f"  [{'OK ' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail and not cond else ""))


def main() -> int:
    try:
        import tkinter as tk
    except ImportError:
        print("没有 tkinter，跳过 GUI 测试")
        return 0

    from dgcore import games
    from dgcore.gui import App

    print("=" * 74)
    print("1. 实例化主窗口")
    print("=" * 74)
    app = None
    try:
        app = App()
        app.withdraw()  # 不显示出来，只做逻辑测试
        check("窗口创建成功", True)
    except Exception as exc:
        import traceback

        traceback.print_exc()
        check("窗口创建成功", False, str(exc))
        return 1

    try:
        print()
        print("=" * 74)
        print("2. 检查新控件是否存在")
        print("=" * 74)
        for attr, name in (
            ("frames_combo", "倍率下拉框"),
            ("preset_combo", "扫描档位下拉框"),
            ("btn_hags", "HAGS 一键开启按钮"),
            ("exe_combo", "主程序下拉框"),
        ):
            check(f"{name} 存在", hasattr(app, attr), attr)

        check("倍率选项已填充", len(app.frames_combo.cget("values")) >= 3,
              str(app.frames_combo.cget("values")))
        check("扫描档位已填充", len(app.preset_combo.cget("values")) == 3,
              str(app.preset_combo.cget("values")))

        print()
        print("=" * 74)
        print("3. 用一个真实游戏走详情渲染")
        print("=" * 74)
        root = Path(r"D:\steam\steamapps\common\Black Myth Wukong Benchmark Tool")
        if not root.is_dir():
            print("  (本机没有这个游戏，改用合成的假游戏)")
            import tempfile

            tmp = Path(tempfile.mkdtemp())
            exe_dir = tmp / "Game" / "Binaries" / "Win64"
            exe_dir.mkdir(parents=True)
            from dgcore.selftest import synth_pe

            (exe_dir / "Fake-Win64-Shipping.exe").write_bytes(
                synth_pe(("KERNEL32.dll", "d3d12.dll", "dxgi.dll"))
            )
            (exe_dir / "nvngx_dlssg.dll").write_bytes(b"x")
            root = tmp / "Game"

        g = games.Game(name="GUI 测试游戏", root=root, source="测试")
        games.inspect_game(g, preset="快速")
        check("扫描出候选", len(g.candidates) > 0, f"{len(g.candidates)}")

        app.game_list = [g]
        app.filter_var.set(False)
        app._apply_filter()

        # 选中第一个
        app.listbox.selection_clear(0, "end")
        app.listbox.selection_set(0)
        app._on_select()

        text = app.detail.get("1.0", "end")
        check("详情面板有内容", len(text.strip()) > 20, f"{len(text)} 字符")
        check("详情里出现了判定结论",
              any(k in text for k in ("D3D12", "不支持", "可以试", "大概率")),
              text[:300])
        check("详情里有安装目录", "安装目录" in text or "目录" in text, text[:300])
        check("prediction 已生成", app.prediction is not None)

        print()
        print("---- 详情面板实际渲染内容 ----")
        for line in text.splitlines():
            if line.strip():
                print(f"    {line}")
        print("---- 结束 ----")

        print()
        print("=" * 74)
        print("4. 参数收集（含新增的倍率）")
        print("=" * 74)
        got = app._collect()
        check("_collect 返回 7 个值", got is not None and len(got) == 7,
              f"{len(got) if got else None}")
        if got:
            target_dir, proxy, why, router, bilinear, lvl, frames = got
            # SM86 走 0.3.0（上限 6X → 5），SM75 走 0.2.4（上限 4X → 3）
            want = 5 if router == "SM86" else 3
            check(f"倍率默认符合 profile（{router} → {want}）", frames == want, str(frames))
            check("路由是 SM86 或 SM75", router in ("SM86", "SM75"), router)
            app.frames_var.set("2X")
            got2 = app._collect()
            check("切换成 2X 后 frames=1", got2 is not None and got2[6] == 1,
                  str(got2[6]) if got2 else "None")

        print()
        print("=" * 74)
        print("5. 预判渲染的各种分支都不崩")
        print("=" * 74)
        from dgcore import capability

        for lv in capability.Support:
            try:
                pred = capability.Prediction(level=lv, headline=f"测试 {lv.value}",
                                             reasons=["r1"], advice="a1")
                app.detail.configure(state="normal")
                app.detail.delete("1.0", "end")
                app._render_prediction(pred)
                app.detail.configure(state="disabled")
                check(f"渲染 {lv.value} 分支", True)
            except Exception as exc:
                check(f"渲染 {lv.value} 分支", False, str(exc))

        print()
        print("=" * 74)
        print("6. 引擎切换（DLSSG ↔ OptiScaler）")
        print("=" * 74)
        from dgcore import state as _state

        check("引擎下拉框存在", hasattr(app, "engine_combo"), "engine_combo")
        check("引擎下拉框有 3 个选项", len(app.engine_combo.cget("values")) == 3,
              str(app.engine_combo.cget("values")))

        # 默认走 DLSSG
        check("默认引擎是 DLSSG", app._current_engine()[0] == _state.ENGINE_DLSSG,
              str(app._current_engine()))

        # 切到 XeSS
        app.engine_var.set("OptiScaler · XeSS 多帧生成")
        app._on_engine_change()
        eng, bundle = app._current_engine()
        check("切到 XeSS 后引擎正确", eng == _state.ENGINE_OPTISCALER and bundle == "optiscaler-xess",
              f"{eng}/{bundle}")
        vals = list(app.frames_combo.cget("values"))
        check("倍率选项换成 2X..6X", vals == ["2X", "3X", "4X", "5X", "6X"], str(vals))
        check("路由框已禁用", str(app.router_combo.cget("state")) == "disabled",
              str(app.router_combo.cget("state")))
        check("采样档已禁用", str(app.sample_combo.cget("state")) == "disabled",
              str(app.sample_combo.cget("state")))

        app.frames_var.set("6X")
        app._on_frames_change()
        check("倍率反查 6", app._current_multiplier() == 6, str(app._current_multiplier()))
        hint = app.frames_hint.cget("text")
        check("6X 提示里点明是实验性", "实验" in hint or "超出" in hint, hint[:120])

        # 切到 DLSS 5
        app.engine_var.set("OptiScaler · DLSS 5 神经网络渲染 + XeSS")
        app._on_engine_change()
        eng2, bundle2 = app._current_engine()
        check("切到 DLSS 5 后 bundle 正确", bundle2 == "optiscaler-dlss5", str(bundle2))
        check("DLSS 5 提示里点明未签名", "未签名" in app.engine_hint.cget("text"),
              app.engine_hint.cget("text")[:120])

        # 切回 DLSSG：路由/采样必须恢复可用
        app.engine_var.set("DLSSG（NVIDIA DLSS 帧生成）")
        app._on_engine_change()
        check("切回 DLSSG 后路由框恢复", str(app.router_combo.cget("state")) == "readonly",
              str(app.router_combo.cget("state")))
        check("切回 DLSSG 后采样档恢复", str(app.sample_combo.cget("state")) == "readonly",
              str(app.sample_combo.cget("state")))
        check("切回 DLSSG 后倍率选项是 DLSSG 的",
              "上限" in " ".join(app.frames_combo.cget("values")),
              str(app.frames_combo.cget("values")))

        # OptiScaler 的预检不依赖 _collect（它不看路由/代理）
        app.engine_var.set("OptiScaler · XeSS 多帧生成")
        app._on_engine_change()
        try:
            tgt = Path(app.chosen_exe).parent if app.chosen_exe else None
            if tgt:
                app._opti_preflight(tgt, "optiscaler-xess", quiet=True)
                check("OptiScaler 预检可执行", True)
            else:
                check("OptiScaler 预检可执行", True, "(没有选定 EXE，跳过)")
        except Exception as exc:
            check("OptiScaler 预检可执行", False, str(exc))

        app.engine_var.set("DLSSG（NVIDIA DLSS 帧生成）")
        app._on_engine_change()

    finally:
        try:
            app.destroy()
        except Exception:
            pass

    print()
    print("=" * 74)
    print("结果：" + (f"{FAIL} 项失败" if FAIL else "全部通过"))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
