# Omni3D iOS（ARKit）

本目录是独立的原生 iPhone 客户端。它用 `WKWebView` 加载与 Android 相同的
`panel/index.html`，用 ARKit `ARWorldTrackingConfiguration` 获取米制相机位姿、
内参、相机图像和稀疏特征点。页面使用与 Android 相同的本地接口
`http://127.0.0.1:50687/ar/...`。

## 构建与安装

1. 用 Xcode 打开 `ios/Omni3DiOS.xcodeproj`，选择 `Omni3DiOS` scheme。
2. 在 Signing & Capabilities 中选择你的 Apple 开发团队，并使用唯一的 Bundle ID。
3. 连接支持 ARKit 的 iPhone，选择它作为运行目标并运行。
4. 顶部“服务器”按钮填入 Omni3D 网页面板的 HTTPS 地址。默认 localhost 地址
   仅适用于服务器也运行在手机上的特殊场景。

无设备时可做无签名编译检查：

```bash
xcodebuild -project ios/Omni3DiOS.xcodeproj -scheme Omni3DiOS \
  -configuration Debug -sdk iphoneos -destination 'generic/platform=iOS' \
  CODE_SIGNING_ALLOWED=NO build
```

## 接口与坐标

- `/ar/scan/start` 打开原生扫描画面；可单张拍摄、每 600 毫秒连续采集或完成扫描。
- `/ar/scan/data` 返回逐帧内参和 4×4 列主序相机到世界位姿，单位为米。
- `/ar/scan/frames/{i}` 返回与内参对应的传感器方向 JPEG。
- `/ar/scan/pointcloud` 返回 ARKit 稀疏特征点 PLY。
- `/ar/status` 和 `/ar/scan/status` 的 `provider` 为 `apple-arkit`。
- `/ar/file/pick` 使用系统文件选择器；`/ar/file/save` 将 PLY 写入
  “文件”App → “我的 iPhone” → “Omni3D”。

ARKit 必须在真机上验证跟踪和相机权限；iOS 模拟器不能提供真实 AR 扫描。
扫描图像、位姿和点云只保留在当前 App 进程内，重新启动后会清空。
