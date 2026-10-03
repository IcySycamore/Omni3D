#pragma once

#include <QByteArray>
#include <QMutex>
#include <QVector>

/**
 * Android AR 后端接口：华为 AREngine 和 Google ARCore 共用扫描流程。
 * ⚠️ 线程纪律：initialize/update/captureJpeg/applyCameraTexture 等须在 GL 线程
 *     （渲染线程）调用；主线程访问前需检查 glOwned()。见 hw_ar_engine_session.h。
 */
class ArSessionBackend
{
public:
    virtual ~ArSessionBackend() = default;

    /** 当前帧数据（线程安全读取） */
    struct FrameData
    {
        float k[9] = {0.f, 0.f, 0.f, 0.f, 0.f, 0.f, 0.f, 0.f, 1.f}; // 3x3 行主序（display 对齐）
        float pose[16] = {0.f};                                     // 4x4 行主序（c2w，米制）
        bool tracking = false;
    };

    /** 创建并配置 AR 会话（需在 GL 线程，cameraTextureId 为 OES 纹理名） */
    virtual bool initialize(unsigned int cameraTextureId) = 0;

    /** 每帧调用：更新 AR 状态并刷新共享帧数据 */
    virtual bool update() = 0;

    /** 设置显示尺寸（渲染线程每帧调用，用于内参旋转对齐） */
    virtual void setDisplaySize(int width, int height) = 0;

    virtual bool isInitialized() const = 0;
    virtual bool isCameraOn() const = 0;
    virtual bool glOwned() const = 0;
    virtual bool applyCameraTexture(unsigned int textureId) = 0;
    virtual bool transformDisplayUv(const float *in, float *out, int count) = 0;
    virtual QByteArray captureJpeg() = 0;
    virtual void imageDimensions(int *width, int *height) const = 0;
    virtual QVector<float> acquirePointCloud() = 0;
    virtual void setPreviewResolution(int width, int height) = 0;
    virtual bool consumeResizePending() = 0;
    virtual bool applyResizeOnRenderThread() = 0;
    virtual bool preferCpuCapture() const { return false; }
    virtual bool gpuIntrinsics(float *outK9, int imageWidth, int imageHeight) const
    { Q_UNUSED(outK9); Q_UNUSED(imageWidth); Q_UNUSED(imageHeight); return false; }

    /** 读取最近一帧数据 */
    virtual FrameData frame() const = 0;

    /** 录制开关（渲染线程据此自动抽帧） */
    virtual void setRecording(bool on) = 0;
    virtual bool isRecording() const = 0;

    /** 请求截图：下一帧渲染后把屏幕读回 JPEG */
    virtual void requestCapture() = 0;

    /** GL 线程：检查并消费截图请求（每帧调用一次） */
    virtual bool consumeCaptureRequest() = 0;

    /** GL 线程：写入截图 JPEG */
    virtual void storeJpeg(const QByteArray &jpeg) = 0;

    /** 取走待处理截图（取走后清空） */
    virtual QByteArray takePendingJpeg() = 0;

    virtual void shutdown() = 0;
};
