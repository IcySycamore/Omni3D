# Omni3D 精确采集壳（Qt C++）

跨平台采集壳：**Qt C++（QML UI）+ ARCore NDK C API**。
设备端不做推理（推理在服务器），只负责精确采集：相机预览 + 精确内参（`imageIntrinsics`）+ 6DoF 米制位姿 + 拍照/录制 + multipart 上传。

技术栈与桌面端 Qt 迁移计划统一（一套 C++ 核心，QML 跨 Android/iOS）。

## 目录结构

```
qt_app/
├── CMakeLists.txt              # Qt 6.5 + Android 打包配置
├── android/AndroidManifest.xml # CAMERA/INTERNET 权限 + AR 特性 + ARCore meta-data
├── arcore/                     # ARCore NDK SDK（见下方「准备 ARCore」）
├── qml/Main.qml                # 暗色 UI：预览 + 拍照/录制/上传
└── src/
    ├── main.cpp                # 入口 + QML 类型注册
    ├── arsession.h/.cpp        # ARCore 会话（NDK C API）：内参/位姿/截图
    ├── camera_item.h/.cpp      # QQuickFramebufferObject 相机预览 + GL 渲染 + 截屏
    ├── capture_controller.h/.cpp # 采集控制器（QML 单例）
    └── uploader.h/.cpp         # QNetworkAccessManager multipart 上传
```

## 依赖

| 组件           | 要求                                                                | 本机状态           |
| -------------- | ------------------------------------------------------------------- | ------------------ |
| Qt             | 6.5.3（含 `android_arm64_v8a`）                                     | ✅ `C:\Qt\Qt6.5.3` |
| JDK            | 17                                                                  | ⏳ 下载中          |
| Android SDK    | cmdline-tools + platform-tools + platforms;android-33 + build-tools | ⏳                 |
| Android NDK    | r25b（25.1.8937393）                                                | ⏳ 下载中          |
| ARCore NDK SDK | `arcore_c_api.h` + `libarcore_sdk_c.so`                             | ⏳ 下载中          |

## 准备 ARCore

下载 `google-ar/arcore-android-sdk`（GitHub），解压后放入：

```
qt_app/arcore/
├── arcore_c_api.h                  # 来自 sdk/libs/arcore_c_api.h
└── lib/
    └── arm64-v8a/
        └── libarcore_sdk_c.so      # 来自 sdk/libs/arm64-v8a/
```

## 构建（Qt Creator）

1. 打开 `qt_app/CMakeLists.txt`（Qt Creator）
2. Kit 选 **Qt 6.5.3 Android arm64-v8a**（`C:\Qt\Qt6.5.3\6.5.3\android_arm64_v8a`）
3. Qt Creator → 工具 → 选项 → 设备/Android：配置 SDK 路径（`D:\Android\Sdk`）、NDK（r25b）、JDK（17）
4. 构建 → 部署到真机（**必须支持 ARCore**）

## 命令行构建（备选）

```powershell
$env:ANDROID_SDK_ROOT='D:\Android\Sdk'
$env:ANDROID_NDK_ROOT='D:\Android\Sdk\ndk\25.1.8937393'
$env:JAVA_HOME='D:\Android\jdk17'
# 用 Qt 的 Android 工具链 + ninja 配置后 androiddeployqt 打包
```

## 数据流

```
原生 GL 线程 (CameraRenderer)
   ├─ ArSession.frame()   ← 精确 K（display 对齐）+ 6DoF 米制位姿（4x4 行主序）
   └─ ArSession.takePendingJpeg() ← glReadPixels 截图
QML (Main.qml) → CaptureController
   ├─ 拍照/录制 → 内存列表（图 + K + pose）
   └─ 上传 → http://frp-oil.com:50865/reconstruct（multipart）
服务器 (web/server.py)
   └─ Fast3R 重建（intrinsics/extrinsics 已接收；尺度缩放逻辑待接入）
```

## 后续路线

- [ ] 服务端按米制位姿缩放点云到真实尺度（闭环「尺度问题」）
- [ ] iOS ARKit 桥（同一套 QML UI）
- [ ] 录制模式轨迹可视化
