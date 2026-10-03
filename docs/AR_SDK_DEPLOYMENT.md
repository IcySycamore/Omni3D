# AR SDK 平台部署与验收

| 平台 | SDK | 客户端 | SDK 选择 |
| --- | --- | --- | --- |
| iPhone | Apple ARKit | `ios/Omni3DiOS.xcodeproj` | ARWorldTrackingConfiguration |
| 普通 Android | Google ARCore 1.56.0 | `app/` | 华为服务未就绪时使用 ARCore |
| 华为 Android / HarmonyOS 兼容设备 | Huawei AR Engine | `app/` | 服务就绪时优先使用华为 SDK |

Android 的 AR 能力是可选的：应用仍能打开网页；缺少 AR 服务时点击扫描会触发
相应安装流程。华为 Server APK 与 NDK 库已在 `app/android/assets/`，Google
ARCore AAR 已在 `app/android/libs/`。需使用支持对应 SDK 的设备，且授予相机权限。

## Android 打包

项目现有的 `app/build_apk.ps1` 适用于已配置 Qt 6.5.3、JDK 17、Android SDK 33
和 NDK r25b 的 Windows 构建机。第一次切换到这次的 SDK 配置时请加 `-Clean`，
确保 AndroidManifest 重新生成。也可用 Qt Creator 打开 `app/CMakeLists.txt`，
选择 Qt Android arm64-v8a Kit 构建 APK。`android/libs/*.aar` 由 Qt 默认 Gradle
模板自动打包。安装后，用应用顶部“服务器”设置面板地址；本机开发服务可通过
`adb reverse tcp:50865 tcp:50865` 访问。

## iOS 打包

见 [`../ios/README.md`](../ios/README.md)。使用 Xcode 和 Apple 开发团队签名，
在 ARKit 兼容 iPhone 上运行。页面服务地址建议使用 HTTPS。

## 真机验收

1. 安装客户端，授予相机权限；打开面板并开始 AR 扫描。
2. 缓慢移动设备至跟踪状态为真，拍摄至少两帧，完成扫描。
3. 检查 `/ar/status` 的 `provider` 分别为 `apple-arkit`、`google-arcore`
   或 `huawei-ar-engine`；`/ar/scan/data` 中每帧均有 16 个位姿值和 9 个内参值。
4. 检查 `/ar/scan/frames/0` 是 JPEG，`/ar/scan/pointcloud` 有 PLY 数据，
   面板能够上传扫描帧并显示重建结果。

当前环境没有连接任何手机，因此真机权限、跟踪质量、服务安装和签名安装仍需
按上述步骤在实际设备上验收。无签名 iOS 编译只能证明源码与工程配置可构建。
