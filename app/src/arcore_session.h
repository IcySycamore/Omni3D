#pragma once

#include "arsession_backend.h"

#include <QMutex>
#include <QMutexLocker>
#include <atomic>
#include <utility>

/// Google ARCore NDK backend for ordinary Android devices.
class ArCoreSession final : public ArSessionBackend
{
public:
    static ArCoreSession *instance();
    static bool serviceReady();
    static bool requestInstall();

    bool initialize(unsigned int cameraTextureId) override;
    bool update() override;
    void setDisplaySize(int width, int height) override;
    bool isInitialized() const override { return m_session != nullptr; }
    bool isCameraOn() const override { return m_cameraOn; }
    bool glOwned() const override { return m_cameraOn; }
    bool applyCameraTexture(unsigned int textureId) override;
    bool transformDisplayUv(const float *in, float *out, int count) override;
    QByteArray captureJpeg() override;
    void imageDimensions(int *width, int *height) const override;
    QVector<float> acquirePointCloud() override;
    void setPreviewResolution(int width, int height) override;
    bool consumeResizePending() override { return false; }
    bool applyResizeOnRenderThread() override { return false; }
    bool preferCpuCapture() const override { return true; }
    bool gpuIntrinsics(float *outK9, int imageWidth, int imageHeight) const override;
    FrameData frame() const override;
    void setRecording(bool on) override { m_recording = on; }
    bool isRecording() const override { return m_recording; }
    void requestCapture() override { m_captureRequested = true; }
    bool consumeCaptureRequest() override { return m_captureRequested.exchange(false); }
    void storeJpeg(const QByteArray &jpeg) override { m_pendingJpeg = jpeg; }
    QByteArray takePendingJpeg() override { return std::exchange(m_pendingJpeg, QByteArray()); }
    void shutdown() override;

private:
    ArCoreSession() = default;
    ~ArCoreSession() override { shutdown(); }

    void *m_session = nullptr;
    void *m_frame = nullptr;
    void *m_pose = nullptr;
    void *m_intrinsics = nullptr;
    void *m_textureIntrinsics = nullptr;
    unsigned int m_textureId = 0;
    int m_width = 0, m_height = 0, m_imageWidth = 0, m_imageHeight = 0;
    int m_textureWidth = 0, m_textureHeight = 0;
    float m_textureK[9] = {0.f, 0.f, 0.f, 0.f, 0.f, 0.f, 0.f, 0.f, 1.f};
    bool m_cameraOn = false;
    bool m_recording = false;
    std::atomic<bool> m_captureRequested{false};
    QByteArray m_pendingJpeg;
    mutable QMutex m_mutex;
    FrameData m_data;
};
