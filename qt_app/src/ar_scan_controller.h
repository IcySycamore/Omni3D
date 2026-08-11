#pragma once

#include <QObject>
#include <QByteArray>
#include <QVector>
#include <QTimer>
#include <QMutex>
#include <atomic>

/**
 * AR 扫描控制器（真实尺度采集）
 *
 * 流程：网页通过桥触发 → 预览纹理就绪后开启 AREngine 相机 → 定时抓帧
 * （captureJpeg：update + acquireCameraImage → JPEG）并记录每帧的米制位姿 →
 * 结束后网页经桥取回帧+位姿提交重建。
 */
class ArScanController : public QObject
{
    Q_OBJECT
    Q_PROPERTY(bool scanning READ scanning NOTIFY scanningChanged)
    Q_PROPERTY(int frameCount READ frameCount NOTIFY frameCountChanged)
    Q_PROPERTY(bool available READ available NOTIFY availableChanged)
    Q_PROPERTY(bool tracking READ tracking NOTIFY trackingChanged)
    Q_PROPERTY(bool finished READ finished NOTIFY finishedChanged)

public:
    static ArScanController *instance();

    bool available() const { return m_available; }
    bool scanning() const { return m_scanning; }
    bool tracking() const { return m_tracking; }
    bool finished() const { return m_finished; }
    int frameCount() const { return m_frames.size(); }

    // 设置/清除扫描可用性（AREngine init 成功后调用）
    void setAvailable(bool ok);

    // 预览尺寸 → AREngine setDisplayGeometry
    Q_INVOKABLE void setPreviewSize(int w, int h);
    // 相机采集分辨率（设置页；→ AREngine setPreviewSize）
    Q_INVOKABLE void setResolution(int w, int h);

    // ---- 扫描控制（桥/QML 调用）----
    Q_INVOKABLE bool startScan();     // 开始连续采集（录制；清空旧帧）
    Q_INVOKABLE void stopScan();      // 停止连续采集（相机保持预览）
    Q_INVOKABLE void captureOne();    // 拍一张（单帧采集，不启动录制）
    Q_INVOKABLE void finish();        // 完成扫描（停止 + 标记 finished → 网页收数据）
    Q_INVOKABLE void reset();         // 清空已抓帧/点云/完成标记

    // 抓帧请求（主线程定时器/桥设置，渲染线程消费执行）
    void requestCapture();
    bool consumeCaptureRequest();
    // 渲染线程执行抓帧后存回结果（线程安全）
    void storeCaptureResult(const QByteArray &jpeg, const QVector<float> &pose16,
                            const QVector<float> &k9, bool tracking);
    // 渲染线程累积华为 SLAM 稀疏点云（世界坐标，空间去重）
    void storePointCloudFrame(const QVector<float> &xyz);

    // ---- 桥取数据 ----
    QByteArray jpegAt(int i) const;
    QVector<float> poseAt(int i) const;        // 16 元素 col-major 4x4
    QVector<float> intrinsicsAt(int i) const;  // 9 元素 K（fx 0 cx / 0 fy cy / 0 0 1）
    QVector<int> frameSizes() const;           // 每帧 jpeg 字节数
    float scale() const { return 1.0f; }       // AREngine VIO 平移为米制
    // 累积的华为点云（世界坐标，xyz 平铺）
    QVector<float> pointCloudPoints() const;
    int pointCloudCount() const { return m_pointCloud.size() / 3; }
    // 生成 PLY（ASCII 二进制头 + 顶点），供桥 /ar/scan/pointcloud 返回
    QByteArray pointCloudPly() const;

signals:
    void scanningChanged();
    void frameCountChanged();
    void availableChanged();
    void trackingChanged();
    void finishedChanged();
    void errorOccurred(const QString &msg);

private slots:
    void onCaptureTick();

private:
    ArScanController();
    ArScanController(const ArScanController &) = delete;
    ArScanController &operator=(const ArScanController &) = delete;

    struct Frame
    {
        QByteArray jpeg;
        QVector<float> pose;       // 16
        QVector<float> intrinsics; // 9
        bool tracking = false;
    };

    std::atomic<bool> m_captureRequested{false};
    bool m_available = false;
    bool m_scanning = false;
    bool m_tracking = false;
    bool m_finished = false;
    QTimer m_timer;
    QVector<Frame> m_frames;
    QVector<float> m_pointCloud; // 华为 SLAM 稀疏点云累积（世界坐标 xyz）
    mutable QMutex m_mutex;
};
