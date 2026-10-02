#include "ar_scan_controller.h"
#include "hw_ar_engine_session.h"

#include <QHash>
#include <QtMath>
#include <QMutexLocker>

#ifdef Q_OS_ANDROID
#include <android/log.h>
#define SLOG(m) __android_log_print(ANDROID_LOG_INFO, "ArScan", "%s", m)
#else
#include <cstdio>
#define SLOG(m) std::fprintf(stderr, "ArScan: %s\n", m)
#endif

ArScanController *ArScanController::instance()
{
    static ArScanController s;
    return &s;
}

ArScanController::ArScanController()
{
    m_timer.setInterval(600); // 抓帧间隔（毫秒）
    connect(&m_timer, &QTimer::timeout, this, &ArScanController::onCaptureTick);
}

void ArScanController::setAvailable(bool ok)
{
    if (m_available == ok)
        return;
    m_available = ok;
    emit availableChanged();
    if (!ok) {
        m_timer.stop();
        m_scanning = false;
        emit scanningChanged();
    }
}

void ArScanController::setPreviewSize(int w, int h)
{
    if (w <= 0 || h <= 0)
        return;
    HwArEngineSession::instance()->setDisplaySize(w, h);
}

void ArScanController::setResolution(int w, int h)
{
    if (w <= 0 || h <= 0)
        return;
    HwArEngineSession::instance()->setPreviewResolution(w, h);
}

bool ArScanController::startScan()
{
    if (!m_available || !HwArEngineSession::instance()->isCameraOn()) {
        SLOG("startScan rejected (camera not on yet)");
        return false;
    }
    m_frames.clear();
    emit frameCountChanged();
    m_finished = false;
    emit finishedChanged();
    m_scanning = true;
    emit scanningChanged();
    m_timer.start();
    SLOG("scan started (recording)");
    return true;
}

void ArScanController::stopScan()
{
    m_timer.stop();
    m_scanning = false;
    emit scanningChanged();
    SLOG("scan stopped");
}

// 拍一张：单帧采集（不启动录制）
void ArScanController::captureOne()
{
    if (!m_available || !HwArEngineSession::instance()->isCameraOn()) {
        SLOG("captureOne rejected (camera not on yet)");
        return;
    }
    requestCapture();
    SLOG("captureOne requested");
}

// 完成扫描：停止采集并标记 finished（网页轮询到 finished && 有帧 → 自动收数据）
void ArScanController::finish()
{
    m_timer.stop();
    m_scanning = false;
    m_finished = true;
    emit scanningChanged();
    emit finishedChanged();
    SLOG("scan finished");
}

// 抓帧请求（主线程设置标志，渲染线程在 render() 中消费执行）
void ArScanController::requestCapture()
{
    m_captureRequested.store(true);
}

bool ArScanController::consumeCaptureRequest()
{
    return m_captureRequested.exchange(false);
}

// 渲染线程执行完抓帧后调用（线程安全）
void ArScanController::storeCaptureResult(const QByteArray &jpeg, const QVector<float> &pose16,
                                          const QVector<float> &k9, bool tracking)
{
    if (jpeg.isEmpty())
        return;
    Frame fr;
    fr.jpeg = jpeg;
    fr.pose = pose16;
    fr.intrinsics = k9;
    fr.tracking = tracking;
    if (tracking != m_tracking) {
        m_tracking = tracking;
        emit trackingChanged();
    }
    {
        QMutexLocker lk(&m_mutex);
        m_frames.append(fr);
    }
    emit frameCountChanged();
}

// 渲染线程累积华为 SLAM 稀疏点云（世界坐标；5mm 空间网格去重）
void ArScanController::storePointCloudFrame(const QVector<float> &xyz)
{
    if (xyz.size() < 3)
        return;
    QMutexLocker lk(&m_mutex);
    const float cell = 0.005f; // 5mm
    QHash<qint64, bool> seen;
    for (int i = 0; i + 2 < xyz.size(); i += 3) {
        const float x = xyz[i], y = xyz[i + 1], z = xyz[i + 2];
        const qint64 key = (qint64(qFloor(x / cell)) * 73856093)
                           ^ (qint64(qFloor(y / cell)) * 19349663)
                           ^ (qint64(qFloor(z / cell)) * 83492791);
        if (seen.contains(key))
            continue;
        seen.insert(key, true);
        m_pointCloud.append(x);
        m_pointCloud.append(y);
        m_pointCloud.append(z);
    }
}

void ArScanController::onCaptureTick()
{
    requestCapture(); // 实际抓帧由渲染线程在 render() 中执行
}

void ArScanController::reset()
{
    QMutexLocker lk(&m_mutex);
    m_frames.clear();
    m_pointCloud.clear();
    m_finished = false;
    emit frameCountChanged();
    emit finishedChanged();
}

QVector<float> ArScanController::pointCloudPoints() const
{
    QMutexLocker lk(&m_mutex);
    return m_pointCloud;
}

// 生成 PLY（ASCII 头 + 二进制 float 顶点），供 /ar/scan/pointcloud 返回
QByteArray ArScanController::pointCloudPly() const
{
    QMutexLocker lk(&m_mutex);
    const int n = m_pointCloud.size() / 3;
    if (n <= 0)
        return {};
    QByteArray out;
    out += "ply\n";
    out += "format binary_little_endian 1.0\n";
    out += "element vertex " + QByteArray::number(n) + "\n";
    out += "property float x\n";
    out += "property float y\n";
    out += "property float z\n";
    out += "end_header\n";
    out.append(reinterpret_cast<const char *>(m_pointCloud.constData()),
               m_pointCloud.size() * sizeof(float));
    return out;
}

QByteArray ArScanController::jpegAt(int i) const
{
    QMutexLocker lk(&m_mutex);
    if (i < 0 || i >= m_frames.size())
        return {};
    return m_frames[i].jpeg;
}

QVector<float> ArScanController::poseAt(int i) const
{
    QMutexLocker lk(&m_mutex);
    if (i < 0 || i >= m_frames.size())
        return {};
    return m_frames[i].pose;
}

QVector<float> ArScanController::intrinsicsAt(int i) const
{
    QMutexLocker lk(&m_mutex);
    if (i < 0 || i >= m_frames.size())
        return {};
    return m_frames[i].intrinsics;
}

QVector<int> ArScanController::frameSizes() const
{
    QMutexLocker lk(&m_mutex);
    QVector<int> out;
    out.reserve(m_frames.size());
    for (const auto &f : m_frames)
        out.append(f.jpeg.size());
    return out;
}
