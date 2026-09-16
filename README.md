# Omni3D

从视频 / 图片重建 **真实尺度的 3D 点云**——网页采集、云端推理、AR 加持。

```
网页采集 ──► FastAPI 队列 ──► Fast3R 稠密重建 ──► 3D 点云查看 / 测量 / 下载
                    ▲
       Qt App 壳（华为 AREngine）提供真实米制位姿 + 稀疏点云
```

- **server**：FastAPI 单机推理（Fast3R 稠密重建）+ 账号 / 会话层 / 历史。
- **web client**：产品主体。采集（录制 / 拍摄 / 本地文件 / AR 扫描）、提交、3D 查看、两点测量、标尺校准、历史记录、设置，全部在浏览器完成。
- **desktop client**：`desktop/`（PyQt5 + VTK）。登录后历史按用户名保存，VTK 视图中测距 / 标尺校准。
- **Mobile APP(Qt webview+web client)**:① 华为 AREngine 真实尺度、位姿（VIO 米制）② 华为 SLAM 稀疏点云；系统文件支持与 PLY 直存手机。

---

## ✨ 功能

- **多路采集**：网页摄像头录制 / 连拍 / 本地视频图片；App 内「AR 扫描」带真实尺度自动取帧
- **稠密重建**：Fast3R（ViT-Large）多视图稠密点云，服务器 GPU 推理
- **真实尺度**：**AR 位姿驱动** —— 服务器用帧携带的米制位姿把点云直接对齐到真实尺度与 AR 世界系（`scale` 随结果返回并入库）；纯网页用「标尺校准」兜底
- **华为点云融合**：AR 扫描时累积华为 SLAM 稀疏点云，与服务器稠密点云同帧叠加（可开关、可合并下载）
- **查看 / 测量**：three.js 点云渲染、两点测距、标尺校准、重置视角
- **下载**：PLY 导出（App 内直写手机下载目录，PC 浏览器直接下载）
- **历史记录**：任务列表 + 状态 + 删除
- **账号**：注册 / 登录

---

## 🚀 运行

**只有 1 个 server，2 种 client；移动端 App 是「网页 client 的原生壳」。**

```
                      ┌────────────────────┐
   浏览器 ───────────► │      server       │
   （web client）     │  web/server.py     │ ──►  Fast3R（GPU 稠密重建）
                      │      :50865        │
   桌面客户端 ───────► │                    │
   （desktop client） │                    │
                      └────────────────────┘
                                 ▲
   移动端 App（qt_app/）──────┘  加载的**就是网页 client**，
   + 本地桥 :50687                额外提供 AR 米制位姿 / 华为点云 / 系统文件
```

### 0. 准备

> ⚠️ **必须用带依赖的 Python 解释器**。本项目的依赖（torch / fastapi / PyQt5 / vtk）
> 装在 conda 环境里，**全局 `python` 通常没有**——直接 `python web/server.py` 会报
> `ModuleNotFoundError: No module named 'fastapi'`。
> 下面统一用 `run.ps1`：它会自动挑解释器，并在缺依赖时给出可操作的提示。

```powershell
git clone https://github.com/IcySycamore/Omni3D.git
cd Omni3D

# 依赖（server + 桌面客户端）
<你的环境>\python.exe -m pip install -r requirements-app.txt

# 模型权重：从 HuggingFace 下载 jedyang97/Fast3R_ViT_Large_512 到
#   jedyang97/Fast3R_ViT_Large_512/     （权重未入库，需手动放置）
```

前置：Python 3.9+、CUDA GPU（建议 ≥12GB 显存）。
解释器不在默认位置时设一次即可：`$env:OMNI3D_PY = 'C:\path\to\envs\Omni3D\python.exe'`

### 1. 启动服务器

```powershell
.\run.ps1 server                                          # 默认 127.0.0.1:50865
$env:HOST='0.0.0.0'; $env:PORT='8000'; .\run.ps1 server    # 局域网 / 自定义端口
```

模型首次加载需数分钟，用 <http://127.0.0.1:50865/health> 查就绪状态（`ready: true`）。

### 2.使用 网页 client

浏览器打开 <http://127.0.0.1:50865/> 即可：采集（录制 / 连拍 / 本地文件）→ 提交重建 →
3D 查看 → 两点测距 → 标尺校准 → 历史记录。

**无需登录**：首次访问会生成一个随机 `client_id` 存在浏览器里，历史记在本浏览器名下。
登录后历史归属到账号；若本机已有匿名记录，账号页会提示「是否并入」（**需手动确认**，
以免多人共用设备时误并）。会话有效期 **30 分钟（滑动）**，持续操作不会掉线。

> 网页由服务器**同源**托管，所以页面里没有也不需要「服务器地址」输入框；
> 移动端 App 的服务器地址在 App 顶部工具条 ⚙ 里配置。

### 3. 使用 桌面 client

```powershell
.\run.ps1 desktop
```

首次点「注册」建号 → 登录 → 左侧选 `demo_examples` 示例或本地视频 → 「开始重建」→
VTK 视图中查看 / 测距 / 标尺校准。**服务器地址在登录窗右上角 ⚙ 里配置**。
详见 [desktop/README.md](desktop/README.md)。

### 4. 使用 移动端 App

构建见 [构建移动端 App](#-构建移动端-app)。App 内 WebView 加载的**就是第 2 步的网页 client**，
所以网页端功能全部可用；此外多出两样网页拿不到的：

- **AR 扫描**：AREngine 米制位姿（真实尺度）+ 华为 SLAM 稀疏点云融合
- **系统文件对话框**选择本地媒体；PLY 直接保存到手机 `Downloads/`

### 采集与重建要点

绕物体缓慢移动 10~20s，覆盖各角度；`提交重建` → 排队 → 抽帧 → 推理 → 出点云（进度条实时显示）。

**点云与 PLY**：页面上实时渲染的是**抽样后**的点（默认 6 万，环境变量 `OMNI3D_MAX_RENDER_POINTS`
可调），并带**真实颜色**；「⬇ 下载 PLY」拿到的是**全量**点云（二进制小端，含 RGB），
可以直接拖进 MeshLab / CloudCompare / Blender 看。

> 重建前会按模型置信度**过滤掉一批低置信点**（`app/core/config.py` 的 `VIS_CONF_PERCENTILE`），
> 所以 `num_points` 会略小于「像素总数」。

> ⚠️ `224` 模式为了贴合训练设定会**按短边裁成 224×224 正方形**
> （横幅丢左右、竖幅丢上下）；想要完整画幅请用 `512`。

### 两个 client 的功能对照

| 功能                          | 网页 client | 桌面 client | 备注                                       |
| ----------------------------- | :---------: | :---------: | ------------------------------------------ |
| 采集：本地视频 / 图片         |     ✅      |     ✅      |                                            |
| 采集：摄像头录制 / 连拍       |     ✅      |     ❌      | 桌面端用本地文件代替                       |
| 采集：**AR 扫描**（米制位姿） |     ✅      |     ❌      | 仅移动端有 AREngine，桌面无法提供          |
| 提交重建 + 实时进度           |     ✅      |     ✅      | 同一套 `/api/tasks`                        |
| 3D 点云查看                   | ✅ three.js |   ✅ VTK    |                                            |
| 两点测距                      |     ✅      |     ✅      |                                            |
| 真实尺度                      |     ✅      |     ✅      | 服务器对齐后自动采用；无 AR 时手动标尺校准 |
| PLY 下载                      |     ✅      |     ✅      | `GET /api/history/{id}/ply`                |
| 历史：列表 / 加载 / 删除      |     ✅      |     ✅      |                                            |
| 抽帧数 / 分辨率(512、224)     |     ✅      |     ✅      |                                            |
| **账号：注册 / 登录 / 登出**  |     ✅      |     ✅      | 同一套挑战-应答；页面内登录页 / 原生登录窗 |
| 账号：匿名记录并入账号        |     ✅      |     ✅      | 均需手动确认（`/api/auth/claim`）          |
| 帮助页                        |     ✅      |     ❌      |                                            |

> **共同点**：测量、尺度换算、历史归属、账号、认证协议**全在 server**，两个 client 行为一致；
> 差异只在**采集方式**与**呈现**。AR 只有移动端能做。
>
> **移动端 App 的账号能力来自网页**：App 加载的就是 `web/index.html`，
> 所以网页端一加登录，浏览器与 App 就同时有了，不需要在 App 里再写一套登录界面。

---

## 构

```
┌─ 客户端 ─────────────────────────────────────────────┐
│  网页 web/index.html（采集/查看/测量/历史/设置）         │
│   └─ fetch /api/*           → 服务器                   │
│   └─ fetch 127.0.0.1:50687  → Qt App 本地 HTTP 桥      │
│                                                       │
│  Qt App 壳（qt_app/）                                 │
│   ├─ WebView 加载网页（混合内容/临时证书已自动放行）      │
│   ├─ ar_bridge_server（本地桥 :50687）                 │
│   ├─ ArScanController + HwArEngineSession（AR 扫描）    │
│   └─ ArScanPreview（渲染线程抓帧/位姿/点云）             │
└──────────────────────────────────────────────────────┘
        │  multipart 上传          │  AR 帧/位姿/点云（桥内取）
        ▼                          ▼
┌─ 服务器 web/server.py :50865 ────────────────────────┐
│  FastAPI 路由（/api/tasks 队列 /reconstruct 同步）     │
│  task_queue（单 worker + 内存缓存 TTL 30min）          │
│  app/core/pipeline.py（Fast3R 加载→推理→对齐）         │
│  fast3r/（vendored 模型仓库 + 权重路径 config）         │
└──────────────────────────────────────────────────────┘
```

**关键协作点**

- **采集端**：网页（浏览器）/ Qt App（AR 扫描）三路互斥，统一 multipart 契约（`is_video/frame_count/resolution/intrinsics/extrinsics` + `client_id`）。
- **真实尺度**：AR 扫描的帧携带 AREngine 米制位姿（VIO），服务器据此重建，测量直接为米制。
- **华为点云融合**：AR 扫描时 AREngine 累积稀疏 SLAM 点云（世界坐标），经桥 `/ar/scan/pointcloud` 取回，与服务器稠密点云同帧叠加（网页开关 + 下载合并）。
- **点云回传**：服务器 `result.ply`（base64/URL）+ `points[:20000]` → 网页渲染；App 内下载走桥 `/ar/file/save` 写 `/sdcard/Download`。

### 对外服务

| 服务                         | 地址              | 接口数 | 分组                                                                       |
| ---------------------------- | ----------------- | ------ | -------------------------------------------------------------------------- |
| **重建服务器**               | `127.0.0.1:50865` | 19     | 页面/健康(2) · 认证(7) · 重建任务(5) · 历史(4) · 尺度反推(1)               |
| **App 本地桥**（仅 Android） | `127.0.0.1:50687` | 16     | 健康/状态/位姿(3) · 历史(1) · 文件选择与保存(2) · AR 扫描(9) · 华为点云(1) |

完整接口、请求/响应示例、状态码与 curl 示例见 **[服务清单 `docs/API.md`](docs/API.md)**。

---

## 🔧 构建移动端 App

前置：Qt 6.5.3、Android SDK/NDK、JDK 17、华为 AREngine SDK。

```powershell
cd qt_app
.\build_apk.ps1 -Project D:\PROJECT\Omni3D\qt_app -LibTarget omni3d_capture `
  -Abi arm64-v8a -ApkOut D:\PROJECT\Omni3D\qt_app\Omni3D_Capture-hw-debug.apk
adb install -r -g Omni3D_Capture-hw-debug.apk
```

- App 入口 `homeUrl`：默认 `http://127.0.0.1:50865/`（adb reverse）；可持久化为部署域名（脱离 adb）。
- 华为 AREngine Server 由 App 自集成安装（资产在 `qt_app/android/assets/`）。

---

## 📁 目录结构

```
Omni3D/
├── web/                  # server（FastAPI）+ 前端
│   ├── server.py         # 路由 / 编排 / 监听（127.0.0.1:50865）
│   ├── auth_store.py     # 用户表：salt + sha256(salt+pwd)（明文不落库）
│   ├── session_manager.py# 会话管理：token → username
│   ├── session_store.py  # 历史持久化（SQLite，按 owner/username 隔离）
│   ├── task_queue.py     # 单 worker 任务队列
│   ├── index.html        # 前端主体（采集/查看/测量/历史/设置）
│   └── WEB_REQUIREMENTS.md  # 前端需求契约
├── desktop/              # 桌面客户端（PyQt5 + VTK）
│   ├── main.py           # 入口：登录窗 ↔ 主窗
│   ├── login_window.py   # 无边框登录窗（⚙ 服务器设置 / — / ✕）
│   ├── main_window.py    # 重建 + 历史 + VTK 视图
│   ├── vtk_view.py       # VTK 点云渲染 + 两点测距
│   └── api_client.py     # HTTP + 认证协议（verifier/proof）
├── app/core/             # 共享核心（server 使用）
│   ├── pipeline.py       # 重建管线（加载→推理→对齐）
│   ├── scale.py          # 真实尺度反推（纯函数）
│   └── config.py         # 权重路径 / 设备 / 参数
├── qt_app/               # 移动端 App 壳（WebView 加载网页 client + 本地桥）
│   ├── qml/WebShell.qml  # WebView 壳
│   ├── qml/ScanPage.qml  # AR 扫描覆盖层
│   └── src/              # 桥 / 扫描 / AREngine / 预览（Android）
├── fast3r/               # vendored 模型仓库（训练/推理）
├── docs/                 # 架构说明 / 审计文档
├── configs/  scripts/  notebooks/  demo_examples/   # 模型实验
└── jedyang97/            # 模型权重（.gitignore，需手动放置）
```

---

## 📚 文档

- [领域词汇与分层约定](CONTEXT.md) —— 术语表 + server/client 硬约束
- [服务清单（API）](docs/API.md) —— 全部接口、认证协议、状态码、curl 示例
- [速度与质量（实测）](docs/PERFORMANCE.md) —— 分辨率/帧数对耗时与自洽性的影响
- [架构说明](docs/ARCHITECTURE.md) —— server/client 模块地图、会话层、真实尺度
- [网页端需求契约](web/WEB_REQUIREMENTS.md)

> 📦 **部署**：部署指南、frp 隧道、容器（Dockerfile）与打包配置在开发阶段已移除，
> 将于**发布（release）阶段**重建。详见 `docs/ARCHITECTURE.md`。

> 🖥 **桌面客户端**：`desktop/`（PyQt5 + VTK）。登录后可用 `demo_examples` 验证重建效果，
> 并在 VTK 视图中测距 / 标尺校准；见 [其 README](desktop/README.md)。

> 📦 **依赖**：应用侧（server + 桌面端）见 [requirements-app.txt](requirements-app.txt)；
> vendored 模型仓库依赖见 `requirements.txt`。

## 🤝 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## ⚖️ License
