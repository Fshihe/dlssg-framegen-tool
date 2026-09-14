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

## 5. 闪退：`FGInput=dlssg` 把游戏弄挂了

在 XeFG 失败之后，我换了个思路试 `FGInput=dlssg`（改走游戏自身的 Streamline
通道取输入）。结果**游戏打开就闪退**。

崩溃日志（硬崩，最后一行之后直接断掉，没有任何 `[E]`）：

```
[01:32:36] [W] hkD3D12CreateDevice D3D12Device created with non-primary GPU   ×3
[01:32:38] [W] streamlineLogCallback Ignoring plugin 'sl.dlss_g'
               since it is was not requested by the host
[01:32:40] [W] FGHooks::hkResizeBuffers SwapChainFlags changed from 842 to 802
[01:32:40] [W] FGHooks::hkResizeBuffers Preventing flag change for XeFG!
[01:32:44] [I] DLSSFeatureDx12::InitDLSS _CreateFeature result: NVSDK_NGX_Result_Success
[01:32:45] [W] OptiInput::ValidateWindowSubclassLocked subclass lost to another WndProc
[01:32:47] [W] OptiInput::LogInputHealthSnapshotLatch ...
<日志到此为止>
```

**根因（我造成的）**：那行 `Ignoring plugin 'sl.dlss_g' since it was not requested
by the host` 说明游戏**没有开启 DLSS 帧生成**，那条 Streamline 通道从未被调用。
我把输入源指向了它，OptiScaler 就停在"配置要求了但输入不存在"的状态。

时间线也对得上：

| 时刻 | 配置 | 结果 |
|---|---|---|
| 01:15 | `FGInput=upscaler` | 游戏正常启动（只是 FG 不出帧） |
| 01:25 | 我改成 `FGInput=dlssg` | — |
| 01:32 | 同上 | **闪退** |

两个次要加强因素：
- `D3D12Device created with non-primary GPU` —— 这台机器装了两个虚拟显示器
  适配器（GameViewer、OrayIddDriver），OptiScaler 挑适配器时可能没挑到独显。
- `Preventing flag change for XeFG!` —— OptiScaler 在强行改交换链标志位。

**已卸载还原，游戏恢复正常**（实测启动后稳定运行 40 秒以上，7 个游戏自带文件
与最早快照逐字节一致，0 处改动）。

**工具侧已加防护**：选 `--fg-input dlssg` 时预检会检查游戏有没有 DLSSG 组件，
没有就**直接报错拦住**；有则警告"必须先在游戏里打开帧生成"。
另外整个引擎都加了一条"可能导致游戏起不来"的前置提醒。

## 6. 还没试过的方向（已降级为不建议）

`FGInput=dlssg` 的正确测试方式需要一个我做不到的前置条件：**先在游戏画面设置里
打开「帧生成」**，那条 Streamline 通道才会被调用。

但考虑到：
- 即使输入对齐，`Requires disabling dilated motion vectors` 这道坎还在
- 这条路径已经实机闪退过一次
- 每次测试都要完整跑一遍基准测试

**不建议继续试。** 黑神话这边用回引擎一（DLSSG）即可。

## 7. 清理

工具装的 11 个文件 + 生成的两个空目录 + 运行产生的 `OptiScaler.log`
都会被 `uninstall` 一次清掉并复核；游戏自带文件一个不动。
安装前的目录快照存在 `work/bmw-snapshot-before.txt`，可用于比对。
