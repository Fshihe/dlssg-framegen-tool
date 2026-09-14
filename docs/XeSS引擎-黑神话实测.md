# 黑神话基准测试 · XeSS 引擎实测记录

测试日期：2026-09-15
测试对象：《黑神话：悟空 性能测试工具》
目录：`D:\steam\steamapps\common\Black Myth Wukong Benchmark Tool\b1\Binaries\Win64`
机器：RTX 3070 / 驱动 32.0.16.1088 (610.88) / Win11 26200 / HAGS 已开

## 结论（先看这个）

**XeSS 帧生成在黑神话上不可用。** 引擎能加载、能解锁、能建立交换链，
但**每一帧插值都失败**，实际生成的帧数是 0。

这不是设置问题 —— OptiScaler 自己的界面就写着原因：

> **Frame Generation (XeFG) — Requires disabling dilated motion vectors**

黑神话的 DLSS 集成用的是 dilated motion vectors，XeFG 不接受这种输入。

## 1. 引擎本身是正常工作的

`OptiScaler.log`：

```
OptiScaler v10.0.0-dev (unknown) (20260911_094730) loaded
FrameGen.Enabled: true
FrameGen.FGInput: upscaler
FrameGen.FGOutput: xefg
XeFG.InterpolationCount: 3
XeFG.UnlockMFG: true
```

MFG 解锁 5/5 全部命中，provider 上报上限从 2X 抬到 4X：

```
XeFGUnlock::Apply XeFG unlock: recognised provider build 0x69cb0f4d
  U1/frame-count-fallback / U2/model-downgrade / U3/default-ceiling
  U4/override-clamp      / U5/reported-maximum    —— 5 of 5 patches applied
XeFGUnlock::Apply XeFG unlock: 5 of 5 patches applied (0 skipped), MFG enabled up to 4X
XeFGProxy::HookXeFG LoadResult: true
XeLLProxy::InitXeLLProper Loaded from ...\libxell.dll
XeFG_Dx12::CreateSwapchainContext XeFG context created
XeFG_Dx12::CreateSwapchain1 Max supported interpolations: 3
XeFG_Dx12::CreateSwapchain1 XeFG swapchain created
```

所以：**配置写对了、解锁生效了、上下文建起来了。** 问题不在这几层。

## 2. 真正失败的地方

XeFG 被激活之后（`XeFG_Dx12::Activate SetEnabled: true`），每一帧都报同一个错：

```
[E] XeFG Log: XeFG: Invalid argument.
    motion vector and depth resource resolutions must match.
```

一次真实运行（约 40 秒）里的报错数：**6781 次**。

时间线（同一份日志）：

| 时刻 | 事件 |
|---|---|
| 01:15:53 | 游戏启动，读到我们的配置 |
| 01:15:57 | XeFG 上下文 + 交换链建立 |
| 01:16:36 | `Activate` —— 帧生成真正开启 |
| 01:16:36 ~ 01:17:15 | **6781 次分辨率不匹配** |
| 01:17:15 | `Deactivate` |

界面上的「XeLL (inactive)」也印证了这一点：低延迟层要在帧生成真正出帧之后
才有东西可管，FG 没出帧，它自然是 inactive。

## 3. 一度误判：HighResMV

**这里要更正一个我先前的错误结论。**

我曾在试验里看到 `HighResMV=true` 时报错数为 0，据此把它设成了默认值。
后来复查试验日志才发现，那两次试验里 **XeFG 从未激活**：

```
trial-baseline.log          Activate 3 次  → 报错 3694   （真实运行）
trial-HighResMV=true.log    Activate 0 次  → 报错 0      （假通过）
```

「0 报错」的真实含义是「什么都没发生」，不是「修好了」。
试验脚本当时按"日志超过 200KB"判定，而报错本身会把日志刷大 ——
于是**报错的运行很快被读到，没报错的运行其实只是没启动**，判据完全反了。

修正后的结论：`HighResMV` 无论 auto 还是 true 都失败
（auto 3694 次 / true 6781 次），**没有任何证据表明它有用**。
因此工具不再覆盖这个值，改回 `auto`（不替用户改上游默认）。

试验脚本已重写为 `tools/probe_bmw_opti.py`，判据改成
**必须出现 `XeFG_Dx12::Activate` 才下结论**，没激活就报"无结论"。

## 4. 环境信息（供参考）

- 渲染分辨率 1708x964 → 显示 2560x1440
- DLSS 初始化标志：`JitteredMV: false`、`LowResMV: false`、`DepthInverted: true`
- `libxess.dll not found` —— XeSS **超分**库缺失。本路径不需要它
  （`FGInput=upscaler` 用游戏自带超分，`FGOutput=xefg` 只需 `libxess_fg`）。
  这也是工具刻意不带那个 77 MB 文件的原因。
- `Hudless resource is nullptr` —— 游戏没提供无 HUD 缓冲，UI 可能被一起插值。
- 游戏目录里的 `D3D12\D3D12Core.dll` 是游戏自带的，与本工具的
  `D3D12_Optiscaler\` 不冲突（目录名不同，安装前已核对）。

## 5. 还没试过的方向

`FGInput=dlssg` —— 改走游戏自身的 Streamline 通道取输入，
而不是从超分那里取。理论上 DLSSG 的输入是引擎按帧生成要求生成的、
尺寸应当对齐，可能绕开这个不匹配。

已留在工具里（`--fg-input dlssg`），但**未经验证**，需要实际跑一次才知道。

## 6. 清理

工具装的 11 个文件 + 生成的两个空目录 + 运行产生的 `OptiScaler.log`
都会被 `uninstall` 一次清掉并复核；游戏自带文件一个不动。
安装前的目录快照存在 `work/bmw-snapshot-before.txt`，可用于比对。
