# GitHub 发布教程（零基础版）

从零开始把工具发到 GitHub，全程大概 15 分钟。

**先说结论：你机器上 git 已经装好了（2.53.0），网络也通，只差两步 —— 配置身份、建仓库推送。**

---

## 第 0 步：配置 git 身份（只需做一次）

git 提交时会记录"是谁提交的"，所以要先告诉它你的名字和邮箱。
**在任意地方打开 PowerShell，把下面两行改掉你的信息再回车**（邮箱用你注册 GitHub 的那个）：

```powershell
git config --global user.name "你的名字"
git config --global user.email "你的邮箱@example.com"
```

> 这两条命令**没有输出就是成功**。名字会公开显示在提交记录里，不想用真名就起个网名。

验证一下：

```powershell
git config --global user.name
git config --global user.email
```

---

## 第 1 步：在 GitHub 网页上建一个空仓库

1. 打开 https://github.com ，登录（没账号就先注册，免费）
2. 右上角 **`+`** → **New repository**
3. 填写：
   - **Repository name**：`dlssg-framegen-tool`（只能用英文、数字、`-`、`_`）
   - **Description**（可选）：`一键为 RTX 20/30 系列开启 DLSS 帧生成`
   - 选 **Public**（公开）
   - ⚠️ **下面三个勾全部不要勾**（README / .gitignore / license 都别加）
     —— 因为我们已经准备好了，勾了反而会冲突
4. 点 **Create repository**

建好后会跳到一个页面，显示一堆命令。**别管它**，只要把页面地址栏里那串记下来，长这样：

```
https://github.com/你的用户名/dlssg-framegen-tool
```

---

## 第 2 步：本地提交并推送

打开 PowerShell，**整段复制粘贴**（把最后那个网址换成你自己的）：

```powershell
cd "E:\AI project\v4.1f\dlssg-tool"

git commit -m "v1.0.1: 支持 RTX 20/30 系列一键开启 DLSS 帧生成"

git branch -M main

git remote add origin https://github.com/你的用户名/dlssg-framegen-tool.git

git push -u origin main
```

**第一次 push 会弹出一个窗口让你登录 GitHub** —— 这是 Git Credential Manager 在要授权：

1. 选 **Sign in with your browser**（用浏览器登录）
2. 浏览器会打开 GitHub 授权页面，点 **Authorize**
3. 回到 PowerShell，会看到进度条，然后显示上传完成

**这一步之后就不用再登录了**，以后推送直接成功。

### 如果不弹窗 / 报错

| 报错 | 原因 | 解决 |
|---|---|---|
| `remote origin already exists` | 重复执行过 | 先跑 `git remote remove origin` 再重试 |
| `Authentication failed` | 登录失败 | 重新跑 `git push -u origin main`，会再弹一次 |
| `could not read Username` | 弹窗被取消了 | 同上 |
| `Connection was reset` / 超时 | 网络问题 | 见文末「网络连不上 GitHub」 |
| `please tell me who you are` | 第 0 步没做 | 回到第 0 步 |

推送成功后，刷新 GitHub 页面 —— 代码就上去了。

---

## 第 3 步：发布 Release（把 exe 传上去）

**注意：39 MB 的 exe 不要直接提交进 git 仓库**，要用 Release 附件的方式发布。
（`.gitignore` 已经排除了 `dist/`，所以刚才那次提交里没有 exe，这是对的。）

1. 打开你的仓库页面
2. 右侧找到 **Releases** → 点 **Create a new release**
3. 填写：
   - **Choose a tag**：输入 `v1.0.1`，然后点 **Create new tag: v1.0.1**
   - **Release title**：`v1.0.1 — 支持 RTX 20/30 系列`
   - **Describe this release**：见下面的模板，直接抄
4. 把这两个文件**拖进**页面下方的附件框：
   - `E:\AI project\v4.1f\dlssg-tool\dist\DLSSG帧生成一键开启工具.exe`
   - `E:\AI project\v4.1f\dlssg-tool\dist\SHA256SUMS.txt`
5. 点 **Publish release**

传 39 MB 大概要几分钟，看网速。

### Release 说明模板（直接复制）

```markdown
## 一键为 RTX 20 / 30 系列开启 DLSS 帧生成

单个 exe，39 MB，**无需安装、无需 Python、无需联网**，双击即用。

### 实机验证

| 显卡 | 路由 | 结果 | 帧率 |
|---|---|---|---|
| RTX 3070 | SM86 | ✅ 成功开启 | 41 → 105 FPS（3X） |
| RTX 2070 Laptop | SM75 | ✅ 成功开启 | 40 → 65 FPS（2X） |

两台机器均未观察到画面异常。详细证据见仓库内 `docs/测试报告.md`。

### 使用前必看

**如果游戏提示「您的显卡不支持 DLSS 帧生成技术」，先检查
「硬件加速 GPU 计划（HAGS）」是否已开启** —— 这是帧生成的硬性系统前置条件，
关闭时连 RTX 40 系都用不了。工具会自动检测并在界面上提示。

### 安全说明

- 只会向游戏目录写入 2 个文件，覆盖前一律先备份，卸载可完整还原
- 不写注册表、不装驱动、不改系统设置
- 检测到反作弊组件会默认阻止安装（**联机游戏有封号风险，建议只用于单机**）
- 内置 85 项安全自检，可自行运行验证

### 文件校验

下载后可用 `certutil -hashfile "DLSSG帧生成一键开启工具.exe" SHA256` 核对，
应与 SHA256SUMS.txt 一致。

---

**致谢**：底层使用 [sdli1995/dlssg_for_sm86](https://github.com/sdli1995/dlssg_for_sm86)
（DLSSG Native 0.2.4）。本工具只是自动化封装，不修改上游二进制。
```

---

## 第 4 步（以后更新版本时）

改了代码要发新版，重复这套就行：

```powershell
cd "E:\AI project\v4.1f\dlssg-tool"
git add -A
git commit -m "说明这次改了什么"
git push
```

然后去 GitHub 网页建一个新的 Release（tag 用 `v1.0.2`）。

---

## 常见问题

### 网络连不上 GitHub

先测一下：

```powershell
Test-NetConnection github.com -Port 443 -InformationLevel Quiet
```

返回 `True` 就是通的。不通的话：

- 你机器上有个代理软件（之前检测到 `127.0.0.1:7890` 端口），**把代理打开再试**
- 如果代理开了 git 还是不通，给 git 单独指定代理：
  ```powershell
  git config --global http.proxy http://127.0.0.1:7890
  git config --global https.proxy http://127.0.0.1:7890
  ```
  （端口号按你代理软件里显示的实际值改。取消用 `git config --global --unset http.proxy`）

### 中文文件名显示成 \344\275\240 这样

已经帮你配好了，如果别的地方遇到，跑一次：

```powershell
git config --global core.quotepath false
```

### 上传后发现不该传的文件

`.gitignore` 已经排除了 `payload/`、`dist/`、`work/`、`__pycache__/`。
如果还是传上去了：

```powershell
git rm -r --cached 目录名
git commit -m "移除不该提交的文件"
git push
```

### 想改仓库名 / 删除仓库

都在 GitHub 网页上：仓库页面 → **Settings** → 最下面 **Danger Zone**。

---

## 需要人工确认的两件事

1. **第 0 步的名字和邮箱** —— 这个我替不了你，你自己填
2. **第 1 步建仓库** —— 需要你的 GitHub 账号登录

其余步骤照抄即可。卡在哪一步，把报错原文发我。
