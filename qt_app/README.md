# 移动端 App（qt_app）

> ⚠️ 这不是"第三种客户端"。它的界面**就是 `web/index.html`**——
> 本目录只是**网页 client 的原生壳（WebView）+ 本地桥**，用于补上网页拿不到的原生能力。

|              | 说明                                                                                                        |
| ------------ | ----------------------------------------------------------------------------------------------------------- |
| **运行什么** | WebView 加载服务器的网页 client（`http://127.0.0.1:50865/`，可由 `homeUrl` 换成部署域名）                   |
| **额外提供** | ① 华为 AREngine 米制位姿（真实尺度）② 华为 SLAM 稀疏点云 ③ 系统文件对话框 ④ PLY 直存手机下载目录 ⑤ 本地历史 |
| **不做**     | 不做推理（推理只在服务器）；不实现独立 UI                                                                   |

桌面端**不是**另一种客户端——它就是浏览器直接打开同一个地址；旧的 PyQt5 + VTK 实现已归档，
见 [`../archive/desktop-pyqt-vtk/README.md`](../archive/desktop-pyqt-vtk/README.md)。

---

## 目录结构

```
qt_app/
├── CMakeLists.txt                  # Qt 6.5 + Android 打包（可选 desktop 目标）
├── build_apk.ps1                   # 命令行打包 APK
├── android/                        # AndroidManifest + assets（含华为 AREngine 资产，已入库）
├── arcore-hw/                      # 华为 AREngine NDK 头文件（库走运行时 dlopen）
├── arcore/                         # ARCore NDK 资产（历史遗留，当前主线用 AREngine）
├── qml/
│   ├─ WebShell.qml                # WebView 壳（顶部工具条：热重载 / 服务器 / 浏览器打开）
│   └── ScanPage.qml                # AR 扫描覆盖层（预览 + 拍摄/录制 + 完成）
└── src/
    ├── main.cpp                    # 入口：解析 homeUrl、注册 QML 类型、启动桥
    ├── ar_bridge_server.h/.cpp     # 本地 HTTP 桥 :50687（见「本地桥」）
    ├── ar_scan_controller.h/.cpp   # AR 扫描控制器：抓帧 + 米制位姿 + 点云累积
    ├── ar_scan_preview.h/.cpp      # QQuickFramebufferObject：渲染线程抓帧
    ├── arsession_backend.h         # AR 后端抽象基类
    ├── hw_ar_engine_session.h/.cpp # 华为 AREngine 适配（dlopen 加载，避免 JNI_OnLoad 崩溃）
    └── sensor_reader.h/.cpp        # 传感器回退（无 AR 时提供旋转位姿）
```

## 本地桥（`127.0.0.1:50687`）

网页通过 `fetch` 访问，解决"网页无法调用原生能力"的问题（Qt WebView 不支持注入 JS 对象）。
**16 个接口，完整清单见 [`../docs/API.md`](../docs/API.md)**，分组：

- 健康 / 状态 / 位姿：`/ar/health`、`/ar/status`、`/ar/pose`
- 历史（App 私有目录 JSON）：`GET|POST /ar/history`
- 文件：`/ar/file/pick`（系统对话框）、`/ar/file/save?name=`（写 `Downloads/`）
- AR 扫描：`/ar/scan/{start,settings,capture,finish,stop,reset,status,data,frames/{i}}`
- 华为点云：`/ar/scan/pointcloud`（PLY）

所有响应带 CORS 头，供外部页面跨域访问。

## 依赖

| 组件               | 要求                                                                       |
| ------------------ | -------------------------------------------------------------------------- |
| Qt                 | 6.5.3（含 `android_arm64_v8a`；本机 `C:\Qt\Qt6.5.3`）                      |
| JDK                | 17                                                                         |
| Android SDK / NDK  | SDK（platforms;android-33 + build-tools）、NDK r25b                        |
| 华为 AREngine 资产 | 已入库：`android/assets/AREngine_Server.apk` + `libhuawei_arengine_ndk.so` |
| 设备               | 需支持华为 AREngine（否则 AR 功能不可用，自动回退传感器）                  |

## 构建

```powershell
cd qt_app
.\build_apk.ps1 -Project D:\PROJECT\Omni3D\qt_app -LibTarget omni3d_capture `
  -Abi arm64-v8a -ApkOut D:\PROJECT\Omni3D\qt_app\Omni3D_Capture-hw-debug.apk
adb install -r -g Omni3D_Capture-hw-debug.apk
```

也可用 Qt Creator 打开 `CMakeLists.txt`，Kit 选 **Qt 6.5.3 Android arm64-v8a**。

**入口地址**：默认 `http://127.0.0.1:50865/`（配合 `adb reverse tcp:50865 tcp:50865`）；
可用 Intent extra `homeUrl` 或写入私有目录 `home_url.txt` 持久化为部署域名。

**无需 adb 也能改地址**：App 顶部工具条有「⚙ 服务器」按钮，弹出**原生**对话框
（纯 QML + C++ `ServerConfig`，不依赖网页能否加载），校验后写入 `home_url.txt`
并立即重载 WebView。这一点很重要：**地址填错时网页根本加载不出来**，
这也是唯一的自救通道。

> **认证界面不需要在 App 里做**：App 加载的就是 `web/index.html`，
> 网页端加了登录页，移动端就自动有了；密码摘要用页面内纯 JS SHA-256 计算
> （因为 `http://127.0.0.1:50865` 之外的安全上下文不可保证）。

## 数据流

```
渲染线程 ArScanPreview ──► ArScanController
   ├─ 抓帧 JPEG（glReadPixels / AImage）
   ├─ 每帧 6DoF 米制位姿（col-major 4×4）+ 内参 K
   └─ 累积华为 SLAM 稀疏点云（世界坐标，空间去重）
网页（经桥取回）──► POST /api/tasks（multipart：帧 + intrinsics + extrinsics）
服务器 ──► Fast3R 稠密重建 ──► 稠密点云 + 稀疏点云同帧叠加
```

## 米制尺度

App 采集时每帧都会带 **col-major 4×4 cam2world 位姿（米）** 一并上传。
服务器用它把重建点云对齐到**真实尺度 + AR 世界坐标系**，因此 App 内的
测量**无需手动标定**即直接以米显示。

详见 [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) 第 6 节。

## 遗留

- `intrinsics` 目前只做校验与报告，未参与几何求解（见 [`../CONTEXT.md`](../CONTEXT.md) 第 6 节）。
- `arcore/` 目录是历史遗留（当前主线用华为 AREngine），待清理。
