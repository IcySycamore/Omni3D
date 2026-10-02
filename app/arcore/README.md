# ARCore NDK SDK 放置说明

从 https://github.com/google-ar/arcore-android-sdk 下载 zip，解压后把以下文件放到本目录：

```
arcore/
├── arcore_c_api.h              ← 来自 sdk 的 libs/arcore_c_api.h
└── lib/
    └── arm64-v8a/
        └── libarcore_sdk_c.so  ← 来自 sdk 的 libs/arm64-v8a/libarcore_sdk_c.so
```

> CMake 的 include 路径 = 本目录；链接路径 = `lib/${ANDROID_ABI}/libarcore_sdk_c.so`
