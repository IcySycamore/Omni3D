# Google ARCore NDK

`arcore_c_api.h` 是 Google ARCore NDK 的 C API 声明。运行库和 Java 类来自
`../android/libs/arcore-1.56.0.aar`（Google Maven `com.google.ar:core:1.56.0`）。
Qt Android 的默认 Gradle 模板会打包 `android/libs/*.aar`，因此无需手动复制 `.so`。
ARCore 按 `optional` 集成，缺少 Google Play Services for AR 时可由用户触发安装。

ARCore SDK 的使用受 [Google ARCore Additional Terms of Service](https://developers.google.com/ar/develop/terms) 约束。
