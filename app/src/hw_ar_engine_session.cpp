#include "hw_ar_engine_session.h"

#include <QJniObject>
#include <QJniEnvironment>
#include <QFile>
#include <QFileInfo>
#include <QStandardPaths>
#include <QDir>
#include <QBuffer>
#include <QtMath>
#include <QDateTime>

// 诊断：写文件日志（logcat 会被华为 SLAM 刷屏淹没，改用私有目录文件）
static void logFile(const char *msg)
{
#ifdef Q_OS_ANDROID
    QFile f(QStandardPaths::writableLocation(QStandardPaths::AppDataLocation)
            + QStringLiteral("/hw_ar.log"));
    if (f.open(QIODevice::Append | QIODevice::WriteOnly | QIODevice::Text)) {
        f.write(QDateTime::currentDateTime().toString("HH:mm:ss.zzz ").toUtf8());
        f.write(msg);
        f.write("\n");
        f.close();
    }
#endif
}

#ifdef Q_OS_ANDROID
#include <QtCore/qcoreapplication.h>
#include <QtCore/qnativeinterface.h>
#include <android/log.h>
#include <dlfcn.h>
#include <cstdio>
#include <media/NdkImage.h>
#define HLOG(m) __android_log_print(ANDROID_LOG_INFO, "HwArEngine", "%s", m)
#define HLOGE(m) __android_log_print(ANDROID_LOG_ERROR, "HwArEngine", "%s", m)
#else
#include <cstdio>
#define HLOG(m) std::fprintf(stderr, "HwArEngine: %s\n", m)
#define HLOGE(m) std::fprintf(stderr, "HwArEngine ERR: %s\n", m)
#endif

// ============================================================
// 华为 AR Engine NDK 动态加载（dlopen）
//  ⚠️ 华为库不能放 APK 的 lib/ 目录：Qt 启动会 System.load 触发
//     JNI_OnLoad 崩溃（已踩坑）。改放 assets/jni/arm64-v8a/，
//     运行时解压到私有目录后 dlopen（dlopen 不触发 JNI_OnLoad）。
// ============================================================

#define HW_FN(ret, name, ...) \
    typedef ret (*name##_fn)(__VA_ARGS__); \
    static name##_fn name = nullptr;

// ---- Session ----
HW_FN(int, HwArSession_create, void*, void*, void**)
HW_FN(void, HwArSession_destroy, void*)
HW_FN(int, HwArSession_configure, void*, const void*)
HW_FN(int, HwArSession_resume, void*)
HW_FN(int, HwArSession_pause, void*)
HW_FN(void, HwArSession_setCameraTextureName, void*, unsigned int)
HW_FN(void, HwArSession_setDisplayGeometry, void*, int32_t, int32_t, int32_t)
HW_FN(int, HwArSession_update, void*, void*)
// ---- Config ----
HW_FN(void, HwArConfig_create, const void*, void**)
HW_FN(void, HwArConfig_destroy, void*)
HW_FN(void, HwArConfig_setUpdateMode, const void*, void*, int)
HW_FN(void, HwArConfig_setPreviewSize, const void*, void*, int32_t, int32_t)
// ---- Frame ----
HW_FN(void, HwArFrame_create, const void*, void**)
HW_FN(void, HwArFrame_destroy, void*)
HW_FN(int, HwArFrame_acquireCamera, const void*, const void*, void**)
HW_FN(int, HwArFrame_acquireCameraImage, const void*, const void*, void**)
HW_FN(void, HwArFrame_transformDisplayUvCoords, const void*, const void*, int32_t, const float*, float*)
// 华为与 ARCore 同构：ArImage_getNdkImage(ArImage*, AImage**) 为 2 参数
HW_FN(void, HwArImage_getNdkImage, const void*, void**)
HW_FN(void, HwArImage_release, void*)
// ---- PointCloud（SLAM 稀疏点云，世界坐标；与 ARCore 同构）----
HW_FN(int, HwArFrame_acquirePointCloud, const void*, const void*, void**)
HW_FN(void, HwArPointCloud_getNumberOfPoints, const void*, const void*, int32_t*)
HW_FN(void, HwArPointCloud_getData, const void*, const void*, const float**)
HW_FN(void, HwArPointCloud_getTimestamp, const void*, const void*, int64_t*)
HW_FN(void, HwArPointCloud_release, void*)
// ---- Camera ----
HW_FN(void, HwArCamera_getTrackingState, const void*, const void*, int*)
HW_FN(void, HwArCamera_getImageIntrinsics, const void*, const void*, void*)
HW_FN(void, HwArCamera_getDisplayOrientedPose, const void*, const void*, void*)
HW_FN(void, HwArCamera_release, void*)
// ---- Pose ----
HW_FN(void, HwArPose_create, const void*, const float*, void**)
HW_FN(void, HwArPose_destroy, void*)
HW_FN(void, HwArPose_getMatrix, const void*, const void*, float*)
// ---- Intrinsics ----
HW_FN(void, HwArCameraIntrinsics_create, const void*, void**)
HW_FN(void, HwArCameraIntrinsics_getFocalLength, const void*, const void*, float*, float*)
HW_FN(void, HwArCameraIntrinsics_getPrincipalPoint, const void*, const void*, float*, float*)
HW_FN(void, HwArCameraIntrinsics_getImageDimensions, const void*, const void*, int32_t*, int32_t*)
HW_FN(void, HwArCameraIntrinsics_destroy, void*)
// ---- EnginesApk ----
HW_FN(int, HwArEnginesApk_isAREngineApkReady, void*, void*)
HW_FN(void, HwArEnginesApk_checkAvailability, void*, void*, int*)
HW_FN(void, HwArEnginesApk_requestInstall, void*, void*, int, int*)

#undef HW_FN

static bool s_dlopen = false;
static void *s_handle = nullptr;

// 从 assets 解压华为 NDK 库到私有目录并 dlopen（线程安全：只做一次）
static bool ensureDlopen()
{
    if (s_dlopen)
        return s_handle != nullptr;
    s_dlopen = true;

#ifdef Q_OS_ANDROID
    const QString srcPath = QStringLiteral("assets:/jni/arm64-v8a/libhuawei_arengine_ndk.so");
    const QString destDir = QStandardPaths::writableLocation(QStandardPaths::AppDataLocation);
    QDir().mkpath(destDir);
    const QString dest = destDir + QStringLiteral("/libhuawei_arengine_ndk.so");

    QFile src(srcPath);
    if (!src.open(QIODevice::ReadOnly)) {
        HLOGE("dlopen: 无法读取 assets 库");
        return false;
    }
    QFile out(dest);
    if (!out.open(QIODevice::WriteOnly)) {
        HLOGE("dlopen: 无法写入私有目录");
        return false;
    }
    out.write(src.readAll());
    out.close();

    s_handle = dlopen(dest.toUtf8().constData(), RTLD_NOW | RTLD_GLOBAL);
    if (!s_handle) {
        HLOGE("dlopen: 打开华为库失败");
        return false;
    }

#define LOAD(name) name = (name##_fn)dlsym(s_handle, #name); \
    if (!name) { HLOGE("dlsym 失败: " #name); return false; }
    LOAD(HwArSession_create) LOAD(HwArSession_destroy)
    LOAD(HwArSession_configure) LOAD(HwArSession_resume) LOAD(HwArSession_pause)
    LOAD(HwArSession_setCameraTextureName) LOAD(HwArSession_setDisplayGeometry)
    LOAD(HwArSession_update)
    LOAD(HwArConfig_create) LOAD(HwArConfig_destroy) LOAD(HwArConfig_setUpdateMode)
    LOAD(HwArConfig_setPreviewSize)
    LOAD(HwArFrame_create) LOAD(HwArFrame_destroy) LOAD(HwArFrame_acquireCamera)
    LOAD(HwArFrame_acquireCameraImage) LOAD(HwArFrame_transformDisplayUvCoords)
    LOAD(HwArImage_getNdkImage) LOAD(HwArImage_release)
    LOAD(HwArFrame_acquirePointCloud)
    LOAD(HwArPointCloud_getNumberOfPoints) LOAD(HwArPointCloud_getData)
    LOAD(HwArPointCloud_getTimestamp) LOAD(HwArPointCloud_release)
    LOAD(HwArCamera_getTrackingState) LOAD(HwArCamera_getImageIntrinsics)
    LOAD(HwArCamera_getDisplayOrientedPose) LOAD(HwArCamera_release)
    LOAD(HwArPose_create) LOAD(HwArPose_destroy) LOAD(HwArPose_getMatrix)
    LOAD(HwArCameraIntrinsics_create) LOAD(HwArCameraIntrinsics_getFocalLength)
    LOAD(HwArCameraIntrinsics_getPrincipalPoint) LOAD(HwArCameraIntrinsics_getImageDimensions)
    LOAD(HwArCameraIntrinsics_destroy)
    LOAD(HwArEnginesApk_isAREngineApkReady)
    LOAD(HwArEnginesApk_checkAvailability) LOAD(HwArEnginesApk_requestInstall)
#undef LOAD

    HLOG("dlopen: 华为 NDK 库加载成功");
    return true;
#else
    return false;
#endif
}

// 从 WindowManager 读取显示旋转角
static int getDisplayRotationDeg()
{
#ifdef Q_OS_ANDROID
    QJniObject ctx = QNativeInterface::QAndroidApplication::context();
    if (!ctx.isValid())
        return 0;
    QJniObject wm = ctx.callObjectMethod(
        "getSystemService", "(Ljava/lang/String;)Ljava/lang/Object;",
        QJniObject::fromString("window").object());
    if (!wm.isValid())
        return 0;
    QJniObject def = wm.callObjectMethod("getDefaultDisplay", "()Landroid/view/Display;");
    if (!def.isValid())
        return 0;
    const int rot = def.callMethod<jint>("getRotation", "()I");
    switch (rot) {
    case 1: return 90;
    case 2: return 180;
    case 3: return 270;
    default: return 0;
    }
#else
    return 0;
#endif
}

HwArEngineSession *HwArEngineSession::instance()
{
    static HwArEngineSession s;
    return &s;
}

HwArEngineSession::~HwArEngineSession()
{
    shutdown();
}

bool HwArEngineSession::serverReady()
{
    if (!ensureDlopen())
        return false;
#ifdef Q_OS_ANDROID
    QJniObject ctx = QNativeInterface::QAndroidApplication::context();
    if (!ctx.isValid())
        return false;
    QJniEnvironment env;
    const int ready = HwArEnginesApk_isAREngineApkReady(env.jniEnv(), ctx.object());
    HLOG(ready ? "serverReady: YES" : "serverReady: NO");

    // 附带 checkAvailability 枚举日志（0/1/2/100/201/202/203），便于排查
    int avail = -1;
    HwArEnginesApk_checkAvailability(env.jniEnv(), ctx.object(), &avail);
    const char *name = "?";
    switch (avail) {
    case 0: name = "UNKNOWN_ERROR"; break;
    case 1: name = "UNKNOWN_CHECKING"; break;
    case 2: name = "UNKNOWN_TIMED_OUT"; break;
    case 100: name = "UNSUPPORTED_DEVICE_NOT_CAPABLE"; break;
    case 201: name = "SUPPORTED_NOT_INSTALLED"; break;
    case 202: name = "SUPPORTED_APK_TOO_OLD"; break;
    case 203: name = "SUPPORTED_INSTALLED"; break;
    }
    {
        char buf[96];
        std::snprintf(buf, sizeof(buf), "checkAvailability: %d (%s)", avail, name);
        HLOG(buf);
    }

    return ready == 1;
#else
    return false;
#endif
}

QString HwArEngineSession::extractServerApk()
{
    const QString destDir = QStandardPaths::writableLocation(QStandardPaths::AppDataLocation);
    QDir().mkpath(destDir);
    const QString dest = destDir + QStringLiteral("/AREngine_Server.apk");
    QFile src(QStringLiteral("assets:/AREngine_Server.apk"));
    if (!src.open(QIODevice::ReadOnly)) {
        HLOGE("extractServerApk: 找不到 Server APK");
        return {};
    }
    QFile out(dest);
    if (!out.open(QIODevice::WriteOnly)) {
        HLOGE("extractServerApk: 无法写入");
        return {};
    }
    out.write(src.readAll());
    out.close();
    HLOG("extractServerApk: done");
    return dest;
}

bool HwArEngineSession::installServer()
{
#ifdef Q_OS_ANDROID
    const QString apk = extractServerApk();
    if (apk.isEmpty())
        return false;
    QJniObject ctx = QNativeInterface::QAndroidApplication::context();
    if (!ctx.isValid())
        return false;
    QJniObject intent("android/content/Intent", "(Ljava/lang/String;)V",
                      QJniObject::fromString("android.intent.action.VIEW").object());
    QJniObject uri = QJniObject::callStaticObjectMethod(
        "android/net/Uri", "parse", "(Ljava/lang/String;)Landroid/net/Uri;",
        QJniObject::fromString("file://" + apk).object());
    intent.callObjectMethod("setDataAndType",
                            "(Landroid/net/Uri;Ljava/lang/String;)Landroid/content/Intent;",
                            uri.object(), QJniObject::fromString("application/vnd.android.package-archive").object());
    intent.callObjectMethod("addFlags", "(I)Landroid/content/Intent;", 0x00000001);
    ctx.callObjectMethod("startActivity", "(Landroid/content/Intent;)V", intent.object());
    HLOG("installServer: 已发起安装 Intent");
    return true;
#else
    return false;
#endif
}

bool HwArEngineSession::initialize(unsigned int cameraTextureId)
{
    if (!ensureDlopen())
        return false;
#ifdef Q_OS_ANDROID
    QJniObject ctx = QNativeInterface::QAndroidApplication::context();
    if (!ctx.isValid())
        return false;
    QJniEnvironment env;

    void *session = nullptr;
    if (HwArSession_create(env.jniEnv(), ctx.object(), &session) != 0 || !session) {
        HLOGE("initialize: HwArSession_create failed");
        return false;
    }
    m_session = session;

    void *config = nullptr;
    HwArConfig_create(session, &config);
    if (config) {
        HwArConfig_setUpdateMode(session, config, 0); // HWAR_UPDATE_MODE_BLOCKING
        // 分辨率设置（设置页可选）：0=默认（AREngine 自动，通常 640×480）
        if (m_previewW > 0 && m_previewH > 0) {
            char buf[96];
            std::snprintf(buf, sizeof(buf), "initialize: setPreviewSize %dx%d",
                          m_previewW, m_previewH);
            HLOG(buf);
            HwArConfig_setPreviewSize(session, config, m_previewW, m_previewH);
        }
        HwArSession_configure(session, config);
        HwArConfig_destroy(config);
    }

    HwArSession_setCameraTextureName(session, cameraTextureId);

    void *frame = nullptr;
    HwArFrame_create(session, &frame);
    m_frame = frame;
    m_intrinsicsObj = nullptr;
    m_poseObj = nullptr;

    m_displayRotationDeg = getDisplayRotationDeg();
    // 相机真正启动（setCameraTextureName + resume）由渲染线程在 GL 上下文
    // current 时执行（applyCameraTexture）。此处 texture 0 不 resume：
    // 主线程无 eglContext 时 resume 会失败，且可能留下坏状态。
    if (cameraTextureId != 0)
        HwArSession_resume(session);
    m_cameraOn = (cameraTextureId != 0);
    HLOG("initialize: OK");
    return true;
#else
    Q_UNUSED(cameraTextureId);
    return false;
#endif
}

// 应用渲染线程创建的 OES 相机纹理并启动相机（AR 扫描预览用）
bool HwArEngineSession::applyCameraTexture(unsigned int texId)
{
    if (!m_session)
        return false;
#ifdef Q_OS_ANDROID
    // 华为 AREngine 相机需要先有正确的 display geometry（rotation/宽高），
    // 否则 resume 后相机无输出（黑屏 + 0 帧）。ScanPage 尺寸可能晚于
    // 本函数执行 → 未就绪时返回 false，由渲染线程每帧重试直到就绪。
    if (m_displayW <= 0 || m_displayH <= 0) {
        HLOG("applyCameraTexture: display size not ready yet, defer");
        logFile("applyCameraTexture: display size not ready, defer");
        return false;
    }
    m_texId = texId;
    HwArSession_setCameraTextureName(m_session, texId);
    HwArSession_setDisplayGeometry(m_session, m_displayRotationDeg, m_displayW, m_displayH);
    // 分辨率设置（扫描前改过）：启动相机前重新 configure（含 setPreviewSize）
    if (m_resizePending.exchange(false)) {
        void *config = nullptr;
        HwArConfig_create(m_session, &config);
        if (config) {
            HwArConfig_setUpdateMode(m_session, config, 0);
            HwArConfig_setPreviewSize(m_session, config, m_previewW, m_previewH);
            const int rc = HwArSession_configure(m_session, config);
            HwArConfig_destroy(config);
            char buf[96];
            std::snprintf(buf, sizeof(buf), "applyCameraTexture: configure %dx%d rc=%d",
                          m_previewW, m_previewH, rc);
            HLOG(buf);
            logFile(buf);
        }
    }
    const int rc = HwArSession_resume(m_session);
    m_cameraOn = (rc == 0);
    m_glOwned = m_cameraOn; // 相机启动后由渲染线程接管 update/取帧（需 GL 上下文）
    {
        char buf[128];
        std::snprintf(buf, sizeof(buf), "applyCameraTexture: %dx%d rot=%d rc=%d",
                      m_displayW, m_displayH, m_displayRotationDeg, rc);
        HLOG(buf);
        logFile(buf);
    }
    return m_cameraOn;
#else
    Q_UNUSED(texId);
    return false;
#endif
}

bool HwArEngineSession::update()
{
    if (!m_session || !m_frame)
        return false;
#ifdef Q_OS_ANDROID
    // 对齐华为官方示例（onDrawFrame）：每次 update 前在 GL 线程重设相机纹理
    // 与 display geometry（旋转后纹理绑定/几何可能失效，必须每帧刷新）
    if (m_texId > 0)
        HwArSession_setCameraTextureName(m_session, m_texId);
    if (m_displayW > 0 && m_displayH > 0)
        HwArSession_setDisplayGeometry(m_session, m_displayRotationDeg, m_displayW, m_displayH);
#endif
    if (HwArSession_update(m_session, m_frame) != 0)
        return false;

    void *camera = nullptr;
    if (HwArFrame_acquireCamera(m_session, m_frame, &camera) != 0 || !camera) {
        logFile("update: acquireCamera FAILED");
        return false;
    }

    if (!m_poseObj) {
        void *pose = nullptr;
        HwArPose_create(m_session, nullptr, &pose);
        m_poseObj = pose;
    }
    void *pose = m_poseObj;
    HwArCamera_getDisplayOrientedPose(m_session, camera, pose);

    float mat[16] = {0.f};
    HwArPose_getMatrix(m_session, pose, mat);

    int tstate = 0;
    HwArCamera_getTrackingState(m_session, camera, &tstate);
    // 诊断：打印实际 tracking state 值（已确认华为枚举与 ARCore 同构）
    {
        static int sDiag = 0;
        if (++sDiag % 30 == 0) {
            char buf[96];
            std::snprintf(buf, sizeof(buf), "update: tstate=%d rot=%d size=%dx%d",
                          tstate, m_displayRotationDeg, m_displayW, m_displayH);
            HLOG(buf);
            logFile(buf);
        }
    }
    // 华为 AR Engine tracking state 枚举（与 ARCore 同构，真机已确认）：
    //   0 = HWAR_TRACKING_STATE_TRACKING（跟踪中）
    //   1 = HWAR_TRACKING_STATE_PAUSED（暂停/初始化）
    //   2 = HWAR_TRACKING_STATE_STOPPED
    const bool tracking = (tstate == 0);

    // 内参（预分配 + 读取焦距/主点）
    if (!m_intrinsicsObj) {
        void *intr = nullptr;
        HwArCameraIntrinsics_create(m_session, &intr);
        m_intrinsicsObj = intr;
    }
    if (m_intrinsicsObj) {
        void *intr = m_intrinsicsObj;
        HwArCamera_getImageIntrinsics(m_session, camera, intr);
        float fx = 0.f, fy = 0.f, cx = 0.f, cy = 0.f;
        HwArCameraIntrinsics_getFocalLength(m_session, intr, &fx, &fy);
        HwArCameraIntrinsics_getPrincipalPoint(m_session, intr, &cx, &cy);
        int32_t iw = 0, ih = 0;
        HwArCameraIntrinsics_getImageDimensions(m_session, intr, &iw, &ih);
        if (iw > 0 && ih > 0) {
            m_imgW = iw;
            m_imgH = ih;
        }
        QMutexLocker lk(&m_mutex);
        m_data.k[0] = fx; m_data.k[1] = 0.f; m_data.k[2] = cx;
        m_data.k[3] = 0.f; m_data.k[4] = fy; m_data.k[5] = cy;
        m_data.k[6] = 0.f; m_data.k[7] = 0.f; m_data.k[8] = 1.f;
    }

    HwArCamera_release(camera);

    {
        QMutexLocker lk(&m_mutex);
        for (int i = 0; i < 16; ++i)
            m_data.pose[i] = mat[i];
        m_data.tracking = tracking;
    }
    return true;
}

void HwArEngineSession::setDisplaySize(int width, int height)
{
    m_displayW = width;
    m_displayH = height;
    // geometry 由 applyCameraTexture（渲染线程 GL 上下文）统一设置；
    // 当前 ScanPage 尺寸固定，无需在此立即调用 setDisplayGeometry
}

// 设置相机采集分辨率（主线程/桥调用）。
// 相机未启动时标记待应用（applyCameraTexture 启动时消费）；
// 相机已运行时由渲染线程 render() 暂停→重配→恢复。
void HwArEngineSession::setPreviewResolution(int width, int height)
{
    if (width <= 0 || height <= 0)
        return;
    m_previewW = width;
    m_previewH = height;
    if (m_session)
        m_resizePending.store(true);
    {
        char buf[96];
        std::snprintf(buf, sizeof(buf), "setPreviewResolution: %dx%d (glOwned=%d)",
                      m_previewW, m_previewH, m_glOwned ? 1 : 0);
        HLOG(buf);
        logFile(buf);
    }
}

bool HwArEngineSession::consumeResizePending()
{
    return m_resizePending.exchange(false);
}

// 渲染线程：暂停 → 按新预览分辨率重配 → 恢复（相机运行时改分辨率）
bool HwArEngineSession::applyResizeOnRenderThread()
{
    if (!m_session || !m_previewW || !m_previewH)
        return false;
#ifdef Q_OS_ANDROID
    HLOG("applyResize: pause -> reconfigure -> resume");
    if (HwArSession_pause(m_session) != 0) {
        HLOGE("applyResize: pause failed");
        return false;
    }
    void *config = nullptr;
    HwArConfig_create(m_session, &config);
    if (config) {
        HwArConfig_setUpdateMode(m_session, config, 0);
        HwArConfig_setPreviewSize(m_session, config, m_previewW, m_previewH);
        const int rc = HwArSession_configure(m_session, config);
        HwArConfig_destroy(config);
        char buf[96];
        std::snprintf(buf, sizeof(buf), "applyResize: configure %dx%d rc=%d",
                      m_previewW, m_previewH, rc);
        HLOG(buf);
        logFile(buf);
    }
    HwArSession_setCameraTextureName(m_session, m_texId);
    if (m_displayW > 0 && m_displayH > 0)
        HwArSession_setDisplayGeometry(m_session, m_displayRotationDeg, m_displayW, m_displayH);
    const int rc2 = HwArSession_resume(m_session);
    m_cameraOn = (rc2 == 0);
    logFile(rc2 == 0 ? "applyResize: resume OK" : "applyResize: resume FAILED");
    return rc2 == 0;
#else
    return false;
#endif
}

// 取当前帧 SLAM 稀疏点云（世界坐标；每点 xyz + 置信度 → 只保留 xyz）
QVector<float> HwArEngineSession::acquirePointCloud()
{
    QVector<float> out;
    if (!m_session || !m_frame)
        return out;
#ifdef Q_OS_ANDROID
    void *cloud = nullptr;
    if (HwArFrame_acquirePointCloud(m_session, m_frame, &cloud) != 0 || !cloud)
        return out;
    int32_t n = 0;
    HwArPointCloud_getNumberOfPoints(m_session, cloud, &n);
    const float *data = nullptr;
    HwArPointCloud_getData(m_session, cloud, &data);
    if (n > 0 && data) {
        out.reserve(n * 3);
        for (int32_t i = 0; i < n; ++i) {
            const float x = data[i * 4], y = data[i * 4 + 1], z = data[i * 4 + 2];
            const float conf = data[i * 4 + 3];
            if (conf < 0.5f)   // 过滤低置信度杂点
                continue;
            if (qIsFinite(x) && qIsFinite(y) && qIsFinite(z))
                out.append(x), out.append(y), out.append(z);
        }
    }
    HwArPointCloud_release(cloud);
#else
    Q_UNUSED(m_session);
    Q_UNUSED(m_frame);
#endif
    return out;
}

// 更新一帧 + 抓取相机 JPEG（AREngine 内部持有相机，无需第二个 Camera2 会话）
QByteArray HwArEngineSession::captureJpeg()
{
    if (!m_session || !m_frame)
        return {};
    if (!update()) // 先刷新 m_data（位姿/内参/跟踪）并推进 frame
        return {};

#ifdef Q_OS_ANDROID
    void *hwImg = nullptr;
    if (HwArFrame_acquireCameraImage(m_session, m_frame, &hwImg) != 0 || !hwImg) {
        logFile("captureJpeg: acquireCameraImage FAILED");
        return {};
    }
    QByteArray out;
    void *ndk = nullptr;
    HwArImage_getNdkImage(hwImg, &ndk);   // 华为与 ARCore 同构：2 参数 (image, &out)
    if (!ndk) {
        logFile("captureJpeg: getNdkImage returned null");
        HwArImage_release(hwImg);
        return {};
    }
    {
        AImage *img = static_cast<AImage *>(ndk);
        int w = 0, h = 0, fmt = 0, planes = 0;
        AImage_getWidth(img, &w);
        AImage_getHeight(img, &h);
        AImage_getFormat(img, &fmt);
        AImage_getNumberOfPlanes(img, &planes);
        char buf[96];
        std::snprintf(buf, sizeof(buf), "captureJpeg: img %dx%d fmt=%d planes=%d", w, h, fmt, planes);
        logFile(buf);
    }
    if (ndk) {
        AImage *img = static_cast<AImage *>(ndk);
        int w = 0, h = 0, fmt = 0, planes = 0;
        AImage_getWidth(img, &w);
        AImage_getHeight(img, &h);
        AImage_getFormat(img, &fmt);
        AImage_getNumberOfPlanes(img, &planes);
        if (w > 0 && h > 0 && planes >= 3) {
            uint8_t *yp = nullptr, *up = nullptr, *vp = nullptr;
            int yRow = 0, uRow = 0, vRow = 0, yPix = 1, uPix = 1, vPix = 1;
            int yLen = 0, uLen = 0, vLen = 0;
            AImage_getPlaneData(img, 0, &yp, &yLen);
            AImage_getPlaneRowStride(img, 0, &yRow);
            AImage_getPlanePixelStride(img, 0, &yPix);
            AImage_getPlaneData(img, 1, &up, &uLen);
            AImage_getPlaneRowStride(img, 1, &uRow);
            AImage_getPlanePixelStride(img, 1, &uPix);
            AImage_getPlaneData(img, 2, &vp, &vLen);
            AImage_getPlaneRowStride(img, 2, &vRow);
            AImage_getPlanePixelStride(img, 2, &vPix);
            QImage rgb(w, h, QImage::Format_RGB888);
            for (int row = 0; row < h; ++row) {
                const uint8_t *yrow = yp + row * yRow;
                const uint8_t *urow = up + (row / 2) * uRow;
                const uint8_t *vrow = vp + (row / 2) * vRow;
                uchar *dst = rgb.scanLine(row);
                for (int col = 0; col < w; ++col) {
                    const int y = yrow[col * yPix];
                    const int u = urow[(col / 2) * uPix] - 128;
                    const int v = vrow[(col / 2) * vPix] - 128;
                    dst[col * 3 + 0] = uchar(qBound(0, y + int(1.402f * v), 255));
                    dst[col * 3 + 1] = uchar(qBound(0, y - int(0.344136f * u) - int(0.714136f * v), 255));
                    dst[col * 3 + 2] = uchar(qBound(0, y + int(1.772f * u), 255));
                }
            }
            QBuffer buf(&out);
            buf.open(QIODevice::WriteOnly);
            rgb.save(&buf, "JPG", 85);
        }
    }
    HwArImage_release(hwImg);
    return out;
#else
    return {};
#endif
}

ArSessionBackend::FrameData HwArEngineSession::frame() const
{
    QMutexLocker lk(&m_mutex);
    return m_data;
}

// 内参图像尺寸（显示方向；供 GPU 高清帧 K 变换用）
void HwArEngineSession::imageDimensions(int *w, int *h) const
{
    if (w)
        *w = m_imgW;
    if (h)
        *h = m_imgH;
}

// 按 display rotation 变换相机纹理 UV（校准预览方向；须在 GL 线程/当前帧有效时调用）
bool HwArEngineSession::transformDisplayUv(const float *in, float *out, int num)
{
    if (!m_session || !m_frame)
        return false;
    HwArFrame_transformDisplayUvCoords(m_session, m_frame, num, in, out);
    return true;
}

void HwArEngineSession::setRecording(bool on)
{
    QMutexLocker lk(&m_mutex);
    m_recording = on;
}

bool HwArEngineSession::isRecording() const
{
    QMutexLocker lk(&m_mutex);
    return m_recording;
}

void HwArEngineSession::requestCapture()
{
    QMutexLocker lk(&m_mutex);
    m_requestCapture = true;
}

bool HwArEngineSession::consumeCaptureRequest()
{
    QMutexLocker lk(&m_mutex);
    const bool r = m_requestCapture;
    m_requestCapture = false;
    return r;
}

void HwArEngineSession::storeJpeg(const QByteArray &jpeg)
{
    QMutexLocker lk(&m_mutex);
    m_pendingJpeg = jpeg;
}

QByteArray HwArEngineSession::takePendingJpeg()
{
    QMutexLocker lk(&m_mutex);
    QByteArray j = m_pendingJpeg;
    m_pendingJpeg.clear();
    return j;
}

void HwArEngineSession::shutdown()
{
    if (m_session) {
#ifdef Q_OS_ANDROID
        HwArSession_pause(m_session);
#endif
        if (m_poseObj)
            HwArPose_destroy(m_poseObj);
        if (m_frame)
            HwArFrame_destroy(m_frame);
        HwArSession_destroy(m_session);
        m_session = nullptr;
        m_frame = nullptr;
        m_poseObj = nullptr;
        m_intrinsicsObj = nullptr;
    }
}
