# Omni3D

从视频 / 图片重建 **真实尺度的 3D 点云**——网页采集、云端推理、AR 加持，**并且已经是一个能收费的服务**。

> **状态：`v1.0.0-mvp`（Python 参考实现，已冻结）**
>
> 功能完整、316 项测试全绿、三服务可部署；但后续开发将换成 **C# / .NET 重写**，本仓归档为参考实现。
> 接手重构先读 **[`docs/HANDOVER.md`](docs/HANDOVER.md)**（冻结的行为契约、精度基线、旧→新接口地图）；
> **新仓库开工包**（行业调研 + 线协议 + 术语表 + ADR，panel 先做）见 **[`docs/handover/`](docs/handover/README.md)**。

```
网页采集 ──► FastAPI 队列 ──► Fast3R 稠密重建 ──► 3D 点云查看 / 测量 / 下载
                    ▲                    ▲
       Qt App 壳（华为 AREngine）        │
       提供真实米制位姿 + 稀疏点云        官网（portal）：账号 / 计费 / 发 API Key
```

三个可独立部署的服务（详见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)）：

| 服务            | 端口  | 职责                                             |
| --------------- | ----- | ------------------------------------------------ |
| **面板 pages**  | 50866 | 只交付页面（无模型依赖）                         |
| **官网 portal** | 50867 | 账号 / 计费 / API Key / 流水（**控制面**）       |
| **服务商 api**  | 50865 | 重建任务 / 会话历史 / 测量（**数据面**，需 GPU） |

- **web client**：**唯一的客户端实现**（`web/index.html`）。采集（录制 / 拍摄 / 本地文件 / AR 扫描）、
  提交、3D 查看、测量、标尺校准、历史、设置，全部在浏览器完成。
- **移动端 App**：不是第二种 client，而是 web client 的 **WebView 壳 + 本地桥 `:50687`**
  （额外提供 AR 米制位姿、华为 SLAM 稀疏点云、系统文件对话框）。
- **计费**：按用量（点云 / 体素 / 网格）、按计划（Personal / Professional，月度重置）、
  用量包（预付、不过期）与余额；扣减顺序 = 计划包 → 用量包 → 余额。
  **验证阶段（`BETA_FREE`）全部免费**，但用量照记。
- 旧的 PyQt5 + VTK 桌面客户端已**归档**（`archive/desktop-pyqt-vtk/`），不再维护。

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

**服务分三个角色（可同机可异机，共享 `data/`）；客户端只有一份实现。**

```
                      ┌────────────────────┐
   浏览器 ───────────► │  面板 :50866       │  页面（无模型依赖）
  （web client）      └────────────────────┘
        │             ┌────────────────────┐
        ├───────────► │ 服务商 :50865      │ ──► Fast3R（GPU 稠密重建）
        │             └────────────────────┘
        └───────────► ┌────────────────────┐
                      │  官网 :50867       │  账号 / 计费 / API Key
                      └────────────────────┘

  移动端 App = 同一个 web client + 本地桥 :50687
  桌面端 = 浏览器直接打开面板地址，无需安装任何客户端
```

### 0. 准备

> ⚠️ **必须用带依赖的 Python 解释器**。本项目依赖（torch / fastapi 等）装在 conda 环境里，
> **全局 `python` 通常没有**——直接 `python web/server.py` 会报 `ModuleNotFoundError`。
> 下面统一用 `run.ps1`：它会自动挑解释器，并在缺依赖时给出可操作的提示。

```powershell
git clone https://github.com/IcySycamore/Omni3D.git
cd Omni3D

# 依赖（应用）
<你的环境>\python.exe -m pip install -r requirements-app.txt

# 模型权重：从 HuggingFace 下载 jedyang97/Fast3R_ViT_Large_512
# 放到任意目录，然后指向它（默认读仓库内被 ignore 的 jedyang97/）
$env:OMNI3D_CHECKPOINT_DIR = 'D:\models\Fast3R_ViT_Large_512'
```

前置：Python 3.10~3.12、CUDA GPU（建议 ≥12GB 显存）。
解释器不在默认位置时设一次即可：`$env:OMNI3D_PY = 'C:\path\to\envs\Omni3D\python.exe'`

### 1. 启动服务

```powershell
.\run.ps1 server     # 50865 服务商 API（首次启动加载模型，数十秒）
.\run.ps1 pages      # 50866 面板页面（手机/别的机器只需连它 + 一个 API 地址）
.\run.ps1 portal     # 50867 官网（账号 / 计费 / API Key）
```

$
`SERVE_PAGE=1`（默认）时服务商 API 顺便也托管页面，所以 `http://127.0.0.1:50865/` 一样能打开面板。
用 <http://127.0.0.1:50865/health> 查就绪（`ready: true`）。

### 2. 使用网页 client（唯一客户端）

浏览器打开 <http://127.0.0.1:50865/> 即可：采集（录制 / 连拍 / 本地文件）→ 提交重建 →
3D 查看 → 两点测距 → 标尺校准 → 历史记录。

**无需登录**：首次访问会生成一个随机 `client_id` 存在浏览器里，历史记在本浏览器名下。
登录后历史归属到账号；若本机已有匿名记录，账号页会提示「是否并入」（**需手动确认**，
以免多人共用设备时误并）。会话有效期 **30 分钟（滑动）**，持续操作不会掉线。

> 网页由服务器**同源**托管，所以页面里没有也不需要「服务器地址」输入框；
> 移动端 App 的服务器地址在 App 顶部工具条 ⚙ 里配置。

### 3. 使用 移动端 App（可选）

构建见 [构建移动端 App](#-构建移动端-app)。App 内 WebView 加载的**就是第 2 步的网页 client**，
所以网页端功能全部可用；此外多出两样网页拿不到的：

- **AR 扫描**：AREngine 米制位姿（真实尺度）+ 华为 SLAM 稀疏点云融合
- **系统文件对话框**选择本地媒体；PLY 直接保存到手机 `Downloads/`

### 采集与重建要点

绕物体缓慢移动 10~20s，覆盖各角度；`提交重建` → 排队 → 抽帧 → 推理 → 出点云（进度条实时显示）。

**点云与 PLY**：页面上实时渲染的是**抽样后**的点（默认 6 万，环境变量 `OMNI3D_MAX_RENDER_POINTS`
可调），并带**真实颜色**；「⬇ 下载 PLY」拿到的是**全量**点云（二进制小端，含 RGB），
可以直接拖进 MeshLab / CloudCompare / Blender 看。

> 重建前会按模型置信度**过滤掉一批低置信点**（`OMNI3D_CONF_PERCENTILE`，默认 10，`0` = 不过滤），
> 再做一次**几何离群点剔除**（SOR，`OMNI3D_SOR_K` / `OMNI3D_SOR_STD`，任一为 `0` 关闭），
> 所以 `num_points` 会小于「像素总数」。

> ⚠️ `224` **仅用于快速预览，勿用于测量**：它为了贴合训练设定会**按短边裁成
> 224×224 正方形**（横幅丢左右、竖幅丢上下），不是等比缩放，测量会直接带上裁切误差。
> 测量请用 `512`（默认）。该行为属 vendored 代码（`fast3r/dust3r/utils/image.py`），不能修改。

### 功能一览（一个 client，三种运行环境）

| 功能                                         | 浏览器 | 移动端 App | 备注                                                     |
| -------------------------------------------- | :----: | :--------: | -------------------------------------------------------- |
| 本地视频 / 图片采集                          |   ✅   |     ✅     |                                                          |
| 摄像头录制 / 连拍                            |   ✅   |     ❌     | App 内 WebView 通常拿不到 `getUserMedia`，用 AR 扫描代替 |
| **AR 扫描**（米制位姿 + 华为 SLAM 稀疏点云） |   ❌   |     ✅     | 需要 AREngine，只有 App 有                               |
| 提交重建 + 实时进度                          |   ✅   |     ✅     | `/api/tasks`                                             |
| 3D 点云查看 / 两点测距 / 标尺校准            |   ✅   |     ✅     | three.js                                                 |
| 真实尺度                                     |   ✅   |     ✅     | AR 位姿自动对齐；无 AR 时手动标尺校准                    |
| PLY 下载                                     |   ✅   |     ✅     | App 内直写手机下载目录                                   |
| 历史：列表 / 加载 / 删除                     |   ✅   |     ✅     | `/api/history`，按 `owner` 隔离                          |
| 账号：注册 / 登录 / 登出、匿名记录并入       |   ✅   |     ✅     | 同一套挑战-应答                                          |
| 帮助页                                       |   ✅   |     ✅     |                                                          |

> **只有一份客户端实现**：`web/index.html`。浏览器与 App 共用它，因此网页侧的改动
> 两个环境同时生效，不存在「两套客户端行为不一致」的问题；差异只在**采集能力**
> （AR 只有 App 有）与**原生外壳**（App 多一个本地桥 `:50687`）。

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
├── archive/              # 已废弃的实现（保留供查阅）
│   └── desktop-pyqt-vtk/ # 旧的 PyQt5 + VTK 桌面客户端，见其 README
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

> 🖥 **客户端只有一个**：`web/index.html`。桌面端就是浏览器直接打开
> <http://127.0.0.1:50865/>（无需安装）；移动端 App 加载的也是它。
> 旧的 PyQt5 + VTK 桌面客户端已归档到
> [`archive/desktop-pyqt-vtk/`](archive/desktop-pyqt-vtk/README.md)。

> 📦 **依赖**：应用侧（server）见 [requirements-app.txt](requirements-app.txt)；
> vendored 模型仓库依赖见 `requirements.txt`。

## 🤝 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## ⚖️ License
