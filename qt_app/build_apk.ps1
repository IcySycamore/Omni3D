<#
Omni3D 采集壳 — Android APK 一键构建脚本

解决的问题：
  1. androiddeployqt 调 gradle wrapper 下载卡死（services.gradle.org 不可达）
     -> 编译只出 .so；打包用本地已解压的 gradle.bat，绝不经过 wrapper 下载
  2. JAVA_HOME 尾随空格 / cmd set 传递问题
     -> 用 -Dorg.gradle.java.home 显式传给 gradle，不依赖环境变量
  3. androiddeployqt 首次生成 android-build 时 gradle 会卡
     -> 带超时保护，超时后自动改用手动 gradle 继续

用法（PowerShell）：
  powershell -ExecutionPolicy Bypass -File build_apk.ps1
  powershell -ExecutionPolicy Bypass -File build_apk.ps1 -Project qt_app\test_mini -LibTarget omni_mini -ApkOut qt_app\test_mini\OmniMini-debug.apk
  powershell -ExecutionPolicy Bypass -File build_apk.ps1 -Clean     # 全量重建
#>
param(
    [string]$Project = "D:\PROJECT\Omni3D\qt_app",
    [string]$LibTarget = "",          # qt_add_executable 目标名（默认从 CMakeLists 解析）
    [string]$BuildDir = "",
    [string]$ApkOut = "",
    [int]$DeployTimeoutSec = 150,     # androiddeployqt 超时（首次生成 android-build 时）
    [ValidateSet("arm64-v8a", "x86_64", "armeabi-v7a", "x86")]
    [string]$Abi = "arm64-v8a",      # Android ABI（模拟器用 x86_64）
    [switch]$Clean,
    [switch]$DisableCamera          # 模拟器调试：禁用相机激活（防虚拟相机崩溃）
)

$ErrorActionPreference = "Stop"

# ---------- 环境（PowerShell 赋值无尾随空格问题）----------
$env:JAVA_HOME = "D:\Android\jdk17\jdk"
$env:ANDROID_SDK_ROOT = "D:\Android\Sdk"
$env:ANDROID_NDK_ROOT = "D:\Android\Sdk\ndk\25.1.8937393"

$Cmake = "C:\msys64\clang64\bin\cmake.exe"
$Ninja = "C:\msys64\clang64\bin\ninja.exe"
$Gradle = "C:\Users\12847\.gradle\wrapper\dists\gradle-8.0-bin\cf74e924e60763a5b9e65370c5c82e61\gradle-8.0\bin\gradle.bat"

# Qt 目标套件与 ABI 一一对应（真机 arm64 / 模拟器 x86_64）
$QtTarget = switch ($Abi) {
    "arm64-v8a" { "android_arm64_v8a" }
    "x86_64" { "android_x86_64" }
    "armeabi-v7a" { "android_armv7" }
    "x86" { "android_x86" }
}
$Qt = "C:/Qt/Qt6.5.3/6.5.3/$QtTarget"
$QtHost = "C:/Qt/Qt6.5.3/6.5.3/mingw_64"

if (-not $BuildDir) { $BuildDir = Join-Path $Project "build-android" }
if (-not $ApkOut) { $ApkOut = Join-Path $Project "Omni3D_Capture-debug.apk" }
$AndroidBuild = Join-Path $BuildDir "android-build"

# ---------- 解析库目标名 ----------
if (-not $LibTarget) {
    $cmakeList = Join-Path $Project "CMakeLists.txt"
    Get-Content $cmakeList | ForEach-Object {
        if ($_ -match 'qt_add_executable\(\s*([A-Za-z0-9_]+)') { $LibTarget = $Matches[1] }
    }
}
if (-not $LibTarget) { throw "无法解析 qt_add_executable 目标名，请用 -LibTarget 指定" }
$soName = "lib${LibTarget}_${Abi}.so"
Write-Host "==> 工程: $Project | 库目标: $LibTarget"

# ---------- 1. configure ----------
if (-not (Test-Path (Join-Path $BuildDir "build.ninja")) -or $Clean) {
    if ($Clean -and (Test-Path $BuildDir)) { Remove-Item $BuildDir -Recurse -Force }
    Write-Host "==> cmake configure"
    $extraDefs = @()
    if ($DisableCamera) { $extraDefs += "-DOMNI3D_DISABLE_CAMERA=ON" }
    & $Cmake -S $Project -B $BuildDir -G Ninja -DCMAKE_BUILD_TYPE=Release `
        "-DCMAKE_TOOLCHAIN_FILE=$Qt/lib/cmake/Qt6/qt.toolchain.cmake" `
        "-DCMAKE_PREFIX_PATH=$Qt" "-DQT_HOST_PATH=$QtHost" `
        "-DANDROID_SDK_ROOT=$env:ANDROID_SDK_ROOT" `
        "-DANDROID_NDK_ROOT=$env:ANDROID_NDK_ROOT" `
        "-DANDROID_ABI=$Abi" "-DANDROID_PLATFORM=android-33" `
        "-DCMAKE_MAKE_PROGRAM=$Ninja" @extraDefs
    if ($LASTEXITCODE -ne 0) { throw "cmake configure 失败" }
}

# ---------- 2. 编译 .so（只编库 target，不触发 androiddeployqt/gradle）----------
Write-Host "==> 编译 $LibTarget"
& $Cmake --build $BuildDir --target $LibTarget -j 4
if ($LASTEXITCODE -ne 0) { throw "编译失败" }
$soPath = Join-Path $BuildDir $soName
if (-not (Test-Path $soPath)) { throw "未找到 $soPath" }

# ---------- 3. 先把 .so 放入 android-build（androiddeployqt 需要它存在）----------
$destLibDir = Join-Path $AndroidBuild "libs\$Abi"
New-Item -ItemType Directory -Force -Path $destLibDir | Out-Null
$destLib = Join-Path $destLibDir $soName
Copy-Item $soPath $destLib -Force
Write-Host "==> 已放入 $soName"

# ---------- 4. 首次生成 android-build（androiddeployqt + 超时保护）----------
if (-not (Test-Path (Join-Path $AndroidBuild "AndroidManifest.xml"))) {
    # androiddeployqt.exe 在宿主工具链（mingw_64），不在 android_arm64_v8a 下
    $deploy = Join-Path $QtHost "bin/androiddeployqt.exe"
    $settings = Join-Path $BuildDir "android-$LibTarget-deployment-settings.json"
    if (-not (Test-Path $settings)) { throw "缺少部署配置 $settings" }
    Write-Host "==> 生成 android-build（超时 $DeployTimeoutSec s，gradle 卡住会跳过）"
    $p = Start-Process -FilePath $deploy -ArgumentList `
        "--input `"$settings`" --output `"$AndroidBuild`" --deployment bundled --gradle" `
        -NoNewWindow -PassThru
    if (-not $p.WaitForExit($DeployTimeoutSec * 1000)) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        Write-Host "   androiddeployqt 超时（预期：wrapper 下载卡死），android-build 已生成，继续手动 gradle"
    }
    if (-not (Test-Path (Join-Path $AndroidBuild "AndroidManifest.xml"))) { throw "android-build 生成失败" }
}

# ---------- 5. 确保最新 .so 在 android-build ----------
Copy-Item $soPath $destLib -Force
Write-Host "==> 已更新 $soName"

# ---------- 5. 手动 gradle 打包（本地 gradle，不经 wrapper 下载）----------
Write-Host "==> gradle assembleDebug"
& $Gradle "-Dorg.gradle.java.home=$env:JAVA_HOME" -p $AndroidBuild assembleDebug --no-daemon
if ($LASTEXITCODE -ne 0) { throw "gradle 打包失败" }

# ---------- 6. 复制 APK ----------
$apkSrc = Get-ChildItem (Join-Path $AndroidBuild "build\outputs\apk\debug") -Filter *.apk -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $apkSrc) { throw "未找到 APK 产物" }
Copy-Item $apkSrc.FullName $ApkOut -Force
Write-Host "==> ✅ APK: $ApkOut ($([math]::Round($apkSrc.Length/1MB,1)) MB)"
