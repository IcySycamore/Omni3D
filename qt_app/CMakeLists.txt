cmake_minimum_required(VERSION 3.21)
project(Omni3DCapture VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

# WebView 壳 + 华为 AREngine（真实尺度位姿）
find_package(Qt6 6.5 REQUIRED COMPONENTS Quick QuickControls2 WebView Network Widgets)

# 华为 AR Engine NDK 头文件（库本身走 dlopen，避免 Qt 自动 System.load 触发 JNI_OnLoad 崩溃）
set(ARCORE_HW_DIR ${CMAKE_CURRENT_SOURCE_DIR}/arcore-hw)
include_directories(${ARCORE_HW_DIR}/include ${CMAKE_CURRENT_SOURCE_DIR}/src)

qt_add_executable(omni3d_capture
    src/main.cpp
    src/sensor_reader.h
    src/sensor_reader.cpp
    src/ar_bridge_server.h
    src/ar_bridge_server.cpp
    src/arsession_backend.h
    src/hw_ar_engine_session.h
    src/hw_ar_engine_session.cpp
    src/ar_scan_controller.h
    src/ar_scan_controller.cpp
    src/ar_scan_preview.h
    src/ar_scan_preview.cpp
)

qt_add_qml_module(omni3d_capture
    URI Omni3D
    VERSION 1.0
    QML_FILES
        qml/WebShell.qml
        qml/ScanPage.qml
)

target_link_libraries(omni3d_capture PRIVATE
    Qt6::Quick
    Qt6::QuickControls2
    Qt6::WebView
    Qt6::Network
    Qt6::Widgets
)

if(ANDROID)
    target_link_libraries(omni3d_capture PRIVATE mediandk) # AImage (YUV 取帧 → JPEG)
    set_target_properties(omni3d_capture PROPERTIES
        QT_ANDROID_PACKAGE_SOURCE_DIR ${CMAKE_CURRENT_SOURCE_DIR}/android
        QT_ANDROID_MIN_SDK_VERSION 24
        QT_ANDROID_TARGET_SDK_VERSION 33
    )
    # 华为 NDK 库放到 assets/jni/arm64-v8a/（Qt 不自动 System.load，运行时可 dlopen）
    set(ANDROID_EXTRA_ASSETS
        ${CMAKE_CURRENT_SOURCE_DIR}/android/assets
    )
endif()
