# XeSS 引擎实测：黑神话 / 幻兽帕鲁

测试日期：2026-09-15
机器：RTX 3070 / 驱动 32.0.16.1088 (610.88) / Win11 26200 / HAGS 已开

---

## 结论（先看这个）

**之前那个"黑神话不兼容"的结论是错的。** 真正的原因是**少了一步配置**。

OptiScaler 官方的 XeFG 说明里写着：

> 对虚幻引擎游戏，用 Upscaler 输入配 DLSS 时，需要**关闭 dilated motion vectors**。
> 编辑游戏的 `Engine.ini` 加入：
> ```ini
> [SystemSettings]
> r.NGX.DLSS.DilateMotionVectors=0
> ```

不设这一条，XeFG 每一帧都会失败：

```
[E] XeFG Log: XeFG: Invalid argument.
    motion vector and depth resource resolutions must match.
```

表现是"装上了、菜单显示 4X、但帧数一点没变" —— 极易误判成游戏不支持。

工具现在会**自动写这一行**，卸载时精确撤销。

---

## 一、我犯的两个错

### 错一：把"文档里写明的配置步骤"当成了"游戏不兼容"

黑神话和帕鲁**都**报同一个错（6781 次 / 263 次），两个游戏同一个错，
本该立刻想到"是我这边缺了什么"，而不是"这两个游戏都不支持"。

OptiScaler 的界面里其实已经提示了 —— 那行红字
**"Requires disabling dilated motion vectors"** 就是"要去关掉这个"，
我却把它当成了"这游戏没救"的说明。

### 错二：假通过

早期试验里看到 `HighResMV=true` 时报错 0 次，据此下了"修好了"的结论。
复查日志才发现那两次 **XeFG 从未激活**（没有 `Activate` 行）——
"0 报错"的真实含义是"什么都没发生"。

试验脚本的判据反了：它按"日志超过 200KB"判定，而报错恰恰会把日志刷大，
于是**报错的运行很快被读到、没报错的运行其实只是没启动**。

已重写为 `tools/probe_bmw_opti.py`：**必须出现 `XeFG_Dx12::Activate` 才下结论**。

---

## 二、闪退那次（也是我造成的）

试 `FGInput=dlssg` 时游戏打开就闪退。日志里：

```
[W] streamlineLogCallback Ignoring plugin 'sl.dlss_g'
      since it is was not requested by the host
```

游戏没开启 DLSS 帧生成，那条 Streamline 通道从未被调用。
我把输入源指向了它，OptiScaler 停在"配置要求了但输入不存在"的状态，硬崩。

时间线吻合：01:15 用 upscaler 时正常；01:25 我改成 dlssg；01:32 闪退。

**工具侧已加拦截**：选 `dlssg` 时预检检查游戏有没有 DLSSG 组件，
没有直接报错拦住；有则警告"必须先在游戏里打开帧生成"。

---

## 三、正确配置（工具已自动完成）

| 项 | 值 | 为什么 |
|---|---|---|
| `FrameGen.FGInput` | `upscaler` | 不要求游戏自带帧生成 |
| `FrameGen.FGOutput` | `xefg` | 用 XeSS 帧生成 |
| `XeFG.InterpolationCount` | 3 | 3 插值 = 4X |
| `XeFG.UnlockMFG` | true | 解锁 provider 上限 |
| `XeFG.ForceBorderless` | true | **XeFG 在独占全屏下不工作** |
| 游戏 `Engine.ini` | `r.NGX.DLSS.DilateMotionVectors=0` | **必需**，否则每帧失败 |

`ForceBorderless` 也是官方说明里的一条：XeFG 不支持独占全屏。
这是"帧数没变"的另一个常见原因。

---

## 四、怎么判断到底有没有生效

官方给的判据（比看帧数可靠）：

1. **Debug View** —— 开了会看到**粉色竖线**，说明 XeFG 在工作
2. **帧时间** —— 生效后帧时间会**变厚（约一倍）但应该平坦**，
   这是 Intel 版的 Flip Metering
3. 另外：**XeFG 有时需要先激活一次、再重启游戏**，设置才会完全生效

工具里的 `tools/check_bmw_log.py` 会直接读日志给结论，不用自己翻。

---

## 五、已知限制（这些是真的）

- **XeFG 不支持 Vulkan**
- **黑神话的 DLSSG 输入需要 OptiPatcher**（一个第三方 ASI 插件，本工具未包含）
- 帕鲁里 Streamline 自己就拒绝了 DLSS-G：
  `Disabling DLSS-G since it is not supported on current hardware`
  （RTX 3070 不是 Ada）—— 所以帕鲁只能用 `upscaler` 输入
- `libxess.dll not found` 是无害的：本路径只需 `libxess_fg`，
  不需要 XeSS **超分**库，这也是工具刻意不带那个 77 MB 文件的原因

---

## 六、Engine.ini 改动的安全性

这一步动的是用户的游戏配置文件，所以做得比较严：

- 改前备份到工具目录
- 只加 `[SystemSettings]` + 标记行 + 那一行键值
- **卸载时按当初的实际情况精确撤销**：
  - 原本有这把键、值是别的 → 改回原值（不删）
  - 原本有 `[SystemSettings]` 节 → 只删键和标记，节头留着
  - 原本没这个节 → 连节头一起删
  - 文件是本工具新建的 → 直接删掉
- 换行风格（CRLF/LF）与结尾换行都按原样保留

自检里覆盖了 8 种输入（含 CRLF、空文件、无换行结尾、带注释）的
**逐字节往返比对**，全都通过。

---

## 七、清理

`uninstall` 会删掉工具装的全部文件、清掉空目录与运行日志、
并把 `Engine.ini` 逐字节还原。游戏自带文件一个不动。
