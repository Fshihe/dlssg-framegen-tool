# 黑神话基准测试 · XeSS 引擎实测记录

测试日期：2026-09-15
测试对象：《黑神话：悟空 性能测试工具》
目录：`D:\steam\steamapps\common\Black Myth Wukong Benchmark Tool\b1\Binaries\Win64`
机器：RTX 3070 / 驱动 32.0.16.1088 (610.88) / Win11 26200 / HAGS 已开

## 结论

**可用。** XeSS 帧生成在 RTX 3070 上正常建立并运行，倍率由工具下发到 4X。

但有一个**必须开的开关**，不开的话完全不生效 —— 见下面第 3 节。

## 1. 引擎加载确认

`OptiScaler.log` 里的关键行：

```
OptiScaler v10.0.0-dev (unknown) (20260911_094730) loaded
FrameGen.Enabled: true
FrameGen.FGInput: upscaler
FrameGen.FGOutput: xefg
XeFG.InterpolationCount: 3
XeFG.UnlockMFG: true
XeFG.MaxInterpolatedFrames: 3
```

读到的配置与我们生成的一致 → 配置文件定位正确、键值写对了节。

## 2. MFG 解锁生效

```
XeFGUnlock::Apply XeFG unlock: recognised provider build 0x69cb0f4d
XeFGUnlock::Apply XeFG unlock: U1/frame-count-fallback patched at 0x20da4f -> E9 CD 00 00 00 90
XeFGUnlock::Apply XeFG unlock: U2/model-downgrade patched at 0x1a5de4 -> EB 06
XeFGUnlock::Apply XeFG unlock: U3/default-ceiling patched at 0x1a517d -> BB 03 00 00 00
XeFGUnlock::Apply XeFG unlock: U4/override-clamp patched at 0x1a45c2 -> C7 87 6C 01 00 00 03 00 00 00
XeFGUnlock::Apply XeFG unlock: U5/reported-maximum patched at 0x20973b -> B8 03 00 00 00
XeFGUnlock::Apply XeFG unlock: 5 of 5 patches applied (0 skipped), MFG enabled up to 4X
XeFGProxy::HookXeFG LoadResult: true
XeLLProxy::InitXeLLProper Loaded from ...\libxell.dll
XeFG_Dx12::CreateSwapchainContext XeFG context created
XeFG_Dx12::CreateSwapchain1 Max supported interpolations: 3
XeFG_Dx12::CreateSwapchain1 XeFG swapchain created
```

5 个补丁全部命中，provider 上报上限从 2X 抬到 4X，XeFG 上下文与交换链都建起来了。
RTX 3070 是 Ampere（CC 8.6），原本被 Ada 门控挡住，这段解锁就是绕过它的。

## 3. 必须开 HighResMV（这是本次最重要的发现）

不开的话，日志里每一帧都刷同一行错误：

```
[E] XeFG Log: XeFG: Invalid argument. motion vector and depth resource resolutions must match.
```

原因：XeFG 要求运动矢量与深度缓冲**分辨率一致**。
本游戏渲染分辨率 1708x964、显示分辨率 2560x1440，而 DLSS 的初始化标志里
`LowResMV: false` —— MV 按显示分辨率给，深度按渲染分辨率给，两者对不上。

对照实验（同一台机器、同一个包、只改这一个键）：

| 配置 | XeFG 报错数 | 结果 |
|---|---|---|
| `HighResMV=auto`（上游默认） | **3694** | 帧生成完全不生效 |
| `HighResMV=true` | **0** | 正常出帧 |
| `HighResMV=true` + `MakeDepthCopy=true` | 0 | 与上一行等价，无需叠加 |

所以工具里把 `XeFG.HighResMV` 默认设成 `true`。

注意这个值**没有通吃的答案**，取决于游戏把 MV 放在哪个分辨率。
遇到别的游戏不正常时，把工具里那个勾选框关掉再试。

## 4. 其它观察（不影响可用性）

- `libxess.dll not found` —— XeSS **超分**库缺失。本路径不需要它
  （`FGInput=upscaler` 用的是游戏自带超分，`FGOutput=xefg` 只需要 `libxess_fg`）。
  这也是工具刻意不带那个 77 MB 文件的原因。
- `Hudless resource is nullptr` —— 游戏没提供无 HUD 缓冲。UI 元素可能被一起插值，
  这是 OptiScaler 这类通用方案的固有限制，不是配置错误。
- `amd_fidelityfx_*` 一系列 not found —— FSR 输出路径的 provider，本路径用不到，
  刻意没打包。
- 游戏目录里的 `D3D12\D3D12Core.dll` 是**游戏自带的**，与本工具的
  `D3D12_Optiscaler\` 不冲突（目录名不同，安装前已核对）。

## 5. 清理

工具装的 11 个文件 + 生成的两个空目录 + 运行产生的 `OptiScaler.log`
都会被 `uninstall` 一次清掉并复核；游戏自带文件一个不动。
安装前的目录快照存在 `work/bmw-snapshot-before.txt`，可用于比对。
