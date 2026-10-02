#pragma once

#include <QMutex>
#include <QImage>
#include <QString>
#include <QVector>
#include <atomic>

#include "arsession_backend.h"

/**
 * 华为 AR Engine 会话封装（HMS NDK C API）。
 * 接口与 ArCoreSession 完全一致，供鸿蒙/华为设备使用。
 */
class HwArEngineSession : public ArSessionBackend
{
public:
    HwArEngineSession() = default;
    ~HwArEngineSession() override;

    HwArEngineSession(const HwArEngineSession &) = delete;
    HwArEngineSession &operator=(const HwArEngineSession &) = delete;

    static HwArEngineSession *instance();
    // 检查 AR Engine Service 是否就绪（未安装/版本不匹配返回 false）
    static bool serverReady();
    // 把 assets 里的 AREngine Server APK 释放到私有目录（返回完整路径，失败返回空）
    static QString extractServerApk();    // 自集成安装 AREngine Server APK（HarmonyOS 4.0+ 机制），成功触发安装返回 true
    static bool installServer();
    bool initialize(unsigned int cameraTextureId) override;
    bool update() override;
    // 按 display rotation 变换相机纹理 UV（校准预览方向；须在 GL 线程调用）
    bool transformDisplayUv(const float *in, float *out, int num);
    // 更新一帧并抓取相机 JPEG（YUV_420_888 → QImage → JPG）；同时刷新 m_data 位姿
    QByteArray captureJpeg();
    // 内参图像尺寸（显示方向，HwArCameraIntrinsics_getImageDimensions）
    void imageDimensions(int *w, int *h) const;
    // 取当前帧的 SLAM 稀疏点云（世界坐标系，每点 xyz；须在 GL 线程 update 后调用）
    QVector<float> acquirePointCloud();
    // 应用相机纹理并启动相机（AR 扫描时由渲染线程创建的 OES 纹理触发）
    bool applyCameraTexture(unsigned int texId);
    // AREngine 会话是否已创建（渲染线程据此等待后再应用纹理）
    bool isInitialized() const { return m_session != nullptr; }
    // 相机是否已启动（AREngine 持有相机，用于判定扫描可用性）
    bool isCameraOn() const { return m_cameraOn; }
    // AREngine 是否由渲染线程接管（update/取帧需在 GL 上下文线程执行）
    bool glOwned() const { return m_glOwned; }
    void setDisplaySize(int width, int height) override;
    // 设置相机采集分辨率（设置页 → 扫描前调用；相机运行时在渲染线程重配）
    void setPreviewResolution(int width, int height);
    // 渲染线程：是否有待应用的分辨率变更
    bool consumeResizePending();
    // 渲染线程：暂停→按新分辨率重配→恢复（相机运行时用；返回是否成功）
    bool applyResizeOnRenderThread();
    FrameData frame() const override;
    void setRecording(bool on) override;
    bool isRecording() const override;
    void requestCapture() override;
    bool consumeCaptureRequest() override;
    void storeJpeg(const QByteArray &jpeg) override;
    QByteArray takePendingJpeg() override;
    void shutdown() override;

private:
    void *m_session = nullptr;       // HwArSession_*
    void *m_frame = nullptr;         // HwArFrame_*（预分配复用）
    void *m_intrinsicsObj = nullptr; // HwArCameraIntrinsics_*（预分配复用）
    void *m_poseObj = nullptr;       // HwArPose_*（预分配复用）
    unsigned int m_texId = 0;        // 绑定的 OES 相机纹理（每帧 update 前重设，官方写法）
    int m_displayRotationDeg = 0;    // WindowManager 旋转角（0/90/180/270）
    int m_displayW = 0;
    int m_displayH = 0;
    int m_previewW = 0;              // 相机采集分辨率（0=AREngine 默认）
    int m_previewH = 0;
    int m_imgW = 0;                  // 内参图像尺寸（显示方向，来自 getImageDimensions）
    int m_imgH = 0;
    std::atomic<bool> m_resizePending{false}; // 分辨率变更待渲染线程应用
    bool m_cameraOn = false;         // AREngine 相机是否已启动（有有效纹理）
    bool m_glOwned = false;          // 渲染线程接管（update 需 GL 上下文）
    mutable QMutex m_mutex;
    FrameData m_data;
    bool m_requestCapture = false;
    bool m_recording = false;
    QByteArray m_pendingJpeg;
};
