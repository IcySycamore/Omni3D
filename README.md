# Omni3D · 手机 3D 重建

从手机视频 / 图片重建 **真实尺度的 3D 点云**——网页采集、云端推理、AR 加持。

```
网页采集 ──► FastAPI 队列 ──► Fast3R 稠密重建 ──► 3D 点云查看 / 测量 / 下载
                    ▲
       Qt App 壳（华为 AREngine）提供真实米制位姿 + 稀疏点云
```

- **网页是产品主体**：采集（录制 / 拍摄 / 本地文件 / AR 扫描）、提交、3D 查看、两点测量、标尺校准、历史记录、设置，全部在浏览器完成。
- **Qt App 是可选壳**：WebView 加载网页，额外提供网页拿不到的两样能力——① 华为 AREngine 真实尺度位姿（VIO 米制）② 华为 SLAM 稀疏点云（与服务器稠密点云同帧融合）。
- **服务器**：FastAPI 单机推理服务（Fast3R 稠密重建），支持 frp / 反向代理 / 公网多种暴露方式。

---

## ✨ 功能

- 🎥 **多路采集**：网页摄像头录制 / 连拍 / 本地视频图片；App 内「AR 扫描」带真实尺度自动取帧
- 🧠 **稠密重建**：Fast3R（ViT-Large）多视图稠密点云，服务器 GPU 推理
- 📏 **真实尺度**：AR 扫描提供米制位姿 → 测量直接显示 `m`；纯网页可用标尺校准
- ◈ **华为点云融合**：AR 扫描时累积华为 SLAM 稀疏点云，与服务器稠密点云同帧叠加（可开关、可合并下载）
- 📊 **查看 / 测量**：three.js 点云渲染、两点测距、标尺校准、重置视角
- 📦 **下载**：PLY 导出（App 内直写手机下载目录，PC 浏览器直接下载）
- 🕘 **历史记录**：任务列表 + 状态 + 删除

---

## 🚀 快速开始（本地跑服务器）

前置：Python 3.9+、CUDA GPU（建议 ≥12GB 显存）、[HuggingFace](https://huggingface.co/) 权重。

```bash
git clone https://github.com/IcySycamore/Omni3D.git
cd Omni3D

# 1) 安装依赖
pip install fastapi uvicorn numpy opencv-python pillow torch
#    （fast3r 模型依赖已 vendored 在 fast3r/requirements.txt，一并安装）

# 2) 放置模型权重
#    从 HuggingFace 下载 jedyang97/Fast3R_ViT_Large_512 到：
#    jedyang97/Fast3R_ViT_Large_512/   （权重未入库，需手动放置）

# 3) 启动服务器（默认 127.0.0.1:50865）
python web/server.py
#    HOST=0.0.0.0 PORT=8000 python web/server.py   # 局域网 / 自定义端口
```

浏览器打开 <http://127.0.0.1:50865/>（模型首次加载需数分钟，`/health` 可查就绪状态）。

### 采集与重建

1. **采集**：录制视频 / 连拍照片 / 选择本地文件；绕物体缓慢移动 10~20s，覆盖各角度
2. **提交**：`提交重建` → 服务器排队 → 抽帧 → 推理 → 生成点云（进度条实时显示）
3. **查看**：点云渲染 + 两点测量 + 标尺校准 + PLY 下载

### 手机 App（可选，需华为设备）

- 构建：见 [构建 App](#-构建-app-qt-app) 与 [部署指南](docs/DEPLOYMENT.md)
- App 内 WebView 加载网页，自动获得：
  - AR 扫描（真实尺度采集 + 华为点云）
  - 本地文件选择（系统对话框）与 PLY 保存到下载目录

---

## 🏗 架构

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

- **采集端**：网页（浏览器）/ Qt App（AR 扫描）三路互斥，统一 multipart 契约（`is_video/frame_count/resolution/intrinsics/extrinsics/ar_pose`）。
- **真实尺度**：AR 扫描的帧携带 AREngine 米制位姿（VIO），服务器据此重建，测量直接为米制。
- **华为点云融合**：AR 扫描时 AREngine 累积稀疏 SLAM 点云（世界坐标），经桥 `/ar/scan/pointcloud` 取回，与服务器稠密点云同帧叠加（网页开关 + 下载合并）。
- **点云回传**：服务器 `result.ply`（base64/URL）+ `points[:20000]` → 网页渲染；App 内下载走桥 `/ar/file/save` 写 `/sdcard/Download`。

---

## 🔧 构建 App（qt_app）

前置：Qt 6.5.3、Android SDK/NDK、JDK 17、华为 AREngine 资产（已入库）。

```powershell
cd qt_app
.\build_apk.ps1 -Project D:\PROJECT\Omni3D\qt_app -LibTarget omni3d_capture `
  -Abi arm64-v8a -ApkOut D:\PROJECT\Omni3D\qt_app\Omni3D_Capture-hw-debug.apk
adb install -r -g Omni3D_Capture-hw-debug.apk
```

- App 入口 `homeUrl`：默认 `http://127.0.0.1:50865/`（adb reverse）；可持久化为 `https://你的域名/`（frp 场景，脱离 adb）。
- 华为 AREngine Server 由 App 自集成安装（资产在 `qt_app/android/assets/`）。

---

## 📁 目录结构

```
Omni3D/
├── web/                  # FastAPI 服务 + 单文件前端
│   ├── server.py         # 路由 / 编排 / 监听（127.0.0.1:50865）
│   ├── index.html        # 前端主体（采集/查看/测量/历史/设置）
│   ├── task_queue.py     # 单 worker 任务队列
│   └── WEB_REQUIREMENTS.md  # 前端需求契约
├── app/                  # 桌面应用收敛包（与 web 共享核心）
│   ├── core/pipeline.py  # 重建管线（加载→推理→对齐）
│   ├── core/config.py    # 权重路径 / 设备 / 参数
│   ├── workers/          # QThread 加载/推理
│   └── ui/               # 桌面卡片 UI（旧产品线）
├── qt_app/               # Qt App 壳（Android）
│   ├── qml/WebShell.qml  # WebView 壳
│   ├── qml/ScanPage.qml  # AR 扫描覆盖层
│   └── src/              # 桥 / 扫描 / AREngine / 预览
├── fast3r/               # vendored 模型仓库（训练/推理）
├── docs/
│   ├── DEPLOYMENT.md     # 部署指南（本仓库）
│   └── frp/              # frp 隧道配置文档
├── configs/  scripts/  notebooks/  demo_examples/   # 模型实验
└── jedyang97/            # 模型权重（.gitignore，需手动放置）
```

---

## 📚 文档

- [部署指南](docs/DEPLOYMENT.md) —— 本地 / 局域网 / frp 公网 / 云 GPU 四种部署
- [frp 隧道配置](docs/frp/README.md) —— 公网暴露与 App 入口
- [网页端需求契约](web/WEB_REQUIREMENTS.md)

## 🤝 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## ⚖️ License

本项目当前仓库未包含 LICENSE 文件（见 [PR #9 讨论](https://github.com/IcySycamore/Omni3D/pull/9)）；内含 vendored 的 [Meta Fast3R](https://github.com/facebookresearch/fast3r) / [DUSt3R](https://github.com/naver/dust3r) 代码，遵循其各自许可证。
