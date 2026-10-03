#include "arcore_session.h"

#ifdef Q_OS_ANDROID

#include "arcore_c_api.h"

#include <QBuffer>
#include <QImage>
#include <QJniEnvironment>
#include <QJniObject>
#include <QMutexLocker>
#include <QtCore/qnativeinterface.h>
#include <android/log.h>
#include <dlfcn.h>
#include <cmath>

#define ALOG(m) __android_log_print(ANDROID_LOG_INFO, "OmniARCore", "%s", m)
#define AR_FN(name) static decltype(&::name) p_##name = nullptr;
AR_FN(ArCoreApk_checkAvailability)
AR_FN(ArCoreApk_requestInstall)
AR_FN(ArSession_create)
AR_FN(ArSession_destroy)
AR_FN(ArSession_configure)
AR_FN(ArSession_resume)
AR_FN(ArSession_pause)
AR_FN(ArSession_setCameraTextureName)
AR_FN(ArSession_setDisplayGeometry)
AR_FN(ArSession_update)
AR_FN(ArConfig_create)
AR_FN(ArConfig_destroy)
AR_FN(ArConfig_setUpdateMode)
AR_FN(ArFrame_create)
AR_FN(ArFrame_destroy)
AR_FN(ArFrame_acquireCamera)
AR_FN(ArFrame_acquireCameraImage)
AR_FN(ArFrame_acquirePointCloud)
AR_FN(ArFrame_transformDisplayUvCoords)
AR_FN(ArCamera_getTrackingState)
AR_FN(ArCamera_getImageIntrinsics)
AR_FN(ArCamera_getTextureIntrinsics)
AR_FN(ArCamera_getPose)
AR_FN(ArCamera_release)
AR_FN(ArCameraIntrinsics_create)
AR_FN(ArCameraIntrinsics_destroy)
AR_FN(ArCameraIntrinsics_getFocalLength)
AR_FN(ArCameraIntrinsics_getPrincipalPoint)
AR_FN(ArCameraIntrinsics_getImageDimensions)
AR_FN(ArPose_create)
AR_FN(ArPose_destroy)
AR_FN(ArPose_getMatrix)
AR_FN(ArPointCloud_getNumberOfPoints)
AR_FN(ArPointCloud_getData)
AR_FN(ArPointCloud_release)
AR_FN(ArImage_getWidth)
AR_FN(ArImage_getHeight)
AR_FN(ArImage_getFormat)
AR_FN(ArImage_getNumberOfPlanes)
AR_FN(ArImage_getPlanePixelStride)
AR_FN(ArImage_getPlaneRowStride)
AR_FN(ArImage_getPlaneData)
AR_FN(ArImage_release)
#undef AR_FN

static bool loadApi()
{
    static bool attempted = false;
    static bool loaded = false;
    if (attempted)
        return loaded;
    attempted = true;
    void *handle = dlopen("libarcore_sdk_c.so", RTLD_NOW | RTLD_LOCAL);
    if (!handle) {
        ALOG(dlerror());
        return false;
    }
#define AR_LOAD(name) p_##name = reinterpret_cast<decltype(p_##name)>(dlsym(handle, #name)); \
                      if (!p_##name) { ALOG("missing " #name); return false; }
    AR_LOAD(ArCoreApk_checkAvailability)
    AR_LOAD(ArCoreApk_requestInstall)
    AR_LOAD(ArSession_create)
    AR_LOAD(ArSession_destroy)
    AR_LOAD(ArSession_configure)
    AR_LOAD(ArSession_resume)
    AR_LOAD(ArSession_pause)
    AR_LOAD(ArSession_setCameraTextureName)
    AR_LOAD(ArSession_setDisplayGeometry)
    AR_LOAD(ArSession_update)
    AR_LOAD(ArConfig_create)
    AR_LOAD(ArConfig_destroy)
    AR_LOAD(ArConfig_setUpdateMode)
    AR_LOAD(ArFrame_create)
    AR_LOAD(ArFrame_destroy)
    AR_LOAD(ArFrame_acquireCamera)
    AR_LOAD(ArFrame_acquireCameraImage)
    AR_LOAD(ArFrame_acquirePointCloud)
    AR_LOAD(ArFrame_transformDisplayUvCoords)
    AR_LOAD(ArCamera_getTrackingState)
    AR_LOAD(ArCamera_getImageIntrinsics)
    AR_LOAD(ArCamera_getTextureIntrinsics)
    AR_LOAD(ArCamera_getPose)
    AR_LOAD(ArCamera_release)
    AR_LOAD(ArCameraIntrinsics_create)
    AR_LOAD(ArCameraIntrinsics_destroy)
    AR_LOAD(ArCameraIntrinsics_getFocalLength)
    AR_LOAD(ArCameraIntrinsics_getPrincipalPoint)
    AR_LOAD(ArCameraIntrinsics_getImageDimensions)
    AR_LOAD(ArPose_create)
    AR_LOAD(ArPose_destroy)
    AR_LOAD(ArPose_getMatrix)
    AR_LOAD(ArPointCloud_getNumberOfPoints)
    AR_LOAD(ArPointCloud_getData)
    AR_LOAD(ArPointCloud_release)
    AR_LOAD(ArImage_getWidth)
    AR_LOAD(ArImage_getHeight)
    AR_LOAD(ArImage_getFormat)
    AR_LOAD(ArImage_getNumberOfPlanes)
    AR_LOAD(ArImage_getPlanePixelStride)
    AR_LOAD(ArImage_getPlaneRowStride)
    AR_LOAD(ArImage_getPlaneData)
    AR_LOAD(ArImage_release)
#undef AR_LOAD
    loaded = true;
    return true;
}

static ArSession *sessionOf(void *ptr) { return static_cast<ArSession *>(ptr); }
static ArFrame *frameOf(void *ptr) { return static_cast<ArFrame *>(ptr); }

static int displayRotation()
{
    QJniObject context = QNativeInterface::QAndroidApplication::context();
    if (!context.isValid()) return 0;
    QJniObject wm = context.callObjectMethod("getSystemService", "(Ljava/lang/String;)Ljava/lang/Object;",
                                             QJniObject::fromString("window").object());
    QJniObject display = wm.callObjectMethod("getDefaultDisplay", "()Landroid/view/Display;");
    return display.isValid() ? display.callMethod<jint>("getRotation", "()I") : 0;
}

#endif

ArCoreSession *ArCoreSession::instance()
{
    static ArCoreSession session;
    return &session;
}

bool ArCoreSession::serviceReady()
{
#ifdef Q_OS_ANDROID
    if (!loadApi()) return false;
    QJniObject context = QNativeInterface::QAndroidApplication::context();
    if (!context.isValid()) return false;
    QJniEnvironment env;
    ArAvailability availability = AR_AVAILABILITY_UNKNOWN_ERROR;
    p_ArCoreApk_checkAvailability(env.jniEnv(), context.object(), &availability);
    if (availability != AR_AVAILABILITY_SUPPORTED_INSTALLED)
        return false;
    // ARCore requires the install check even when its service APK is present:
    // device profile data may still need an update before ArSession_create.
    ArInstallStatus install = AR_INSTALL_STATUS_INSTALL_REQUESTED;
    return p_ArCoreApk_requestInstall(env.jniEnv(), context.object(), 0, &install) == AR_SUCCESS
           && install == AR_INSTALL_STATUS_INSTALLED;
#else
    return false;
#endif
}

bool ArCoreSession::requestInstall()
{
#ifdef Q_OS_ANDROID
    if (!loadApi()) return false;
    QJniObject activity = QNativeInterface::QAndroidApplication::context();
    if (!activity.isValid()) return false;
    QJniEnvironment env;
    ArAvailability availability = AR_AVAILABILITY_UNKNOWN_ERROR;
    p_ArCoreApk_checkAvailability(env.jniEnv(), activity.object(), &availability);
    if (availability != AR_AVAILABILITY_SUPPORTED_NOT_INSTALLED &&
        availability != AR_AVAILABILITY_SUPPORTED_APK_TOO_OLD &&
        availability != AR_AVAILABILITY_SUPPORTED_INSTALLED)
        return false;
    ArInstallStatus install = AR_INSTALL_STATUS_INSTALL_REQUESTED;
    return p_ArCoreApk_requestInstall(env.jniEnv(), activity.object(), 1, &install) == AR_SUCCESS;
#else
    return false;
#endif
}

bool ArCoreSession::initialize(unsigned int textureId)
{
#ifdef Q_OS_ANDROID
    if (!serviceReady()) return false;
    QJniObject context = QNativeInterface::QAndroidApplication::context();
    QJniEnvironment env;
    ArSession *session = nullptr;
    if (p_ArSession_create(env.jniEnv(), context.object(), &session) != AR_SUCCESS || !session)
        return false;
    m_session = session;
    ArConfig *config = nullptr;
    p_ArConfig_create(session, &config);
    if (config) {
        p_ArConfig_setUpdateMode(session, config, AR_UPDATE_MODE_BLOCKING);
        if (p_ArSession_configure(session, config) != AR_SUCCESS) {
            p_ArConfig_destroy(config);
            shutdown();
            return false;
        }
        p_ArConfig_destroy(config);
    }
    ArFrame *frame = nullptr;
    p_ArFrame_create(session, &frame);
    m_frame = frame;
    ArPose *pose = nullptr;
    p_ArPose_create(session, nullptr, &pose);
    m_pose = pose;
    ArCameraIntrinsics *intrinsics = nullptr;
    p_ArCameraIntrinsics_create(session, &intrinsics);
    m_intrinsics = intrinsics;
    ArCameraIntrinsics *textureIntrinsics = nullptr;
    p_ArCameraIntrinsics_create(session, &textureIntrinsics);
    m_textureIntrinsics = textureIntrinsics;
    if (!frame || !pose || !intrinsics || !textureIntrinsics) { shutdown(); return false; }
    if (textureId) return applyCameraTexture(textureId);
    ALOG("ARCore session initialized");
    return true;
#else
    Q_UNUSED(textureId);
    return false;
#endif
}

bool ArCoreSession::applyCameraTexture(unsigned int textureId)
{
#ifdef Q_OS_ANDROID
    if (!m_session || !textureId || m_width <= 0 || m_height <= 0) return false;
    m_textureId = textureId;
    auto *session = sessionOf(m_session);
    p_ArSession_setCameraTextureName(session, textureId);
    p_ArSession_setDisplayGeometry(session, displayRotation(), m_width, m_height);
    m_cameraOn = p_ArSession_resume(session) == AR_SUCCESS;
    return m_cameraOn;
#else
    Q_UNUSED(textureId);
    return false;
#endif
}

bool ArCoreSession::update()
{
#ifdef Q_OS_ANDROID
    if (!m_session || !m_frame || !m_cameraOn) return false;
    auto *session = sessionOf(m_session);
    auto *frame = frameOf(m_frame);
    p_ArSession_setCameraTextureName(session, m_textureId);
    p_ArSession_setDisplayGeometry(session, displayRotation(), m_width, m_height);
    if (p_ArSession_update(session, frame) != AR_SUCCESS) return false;
    ArCamera *camera = nullptr;
    p_ArFrame_acquireCamera(session, frame, &camera);
    if (!camera) return false;
    ArTrackingState state = AR_TRACKING_STATE_PAUSED;
    p_ArCamera_getTrackingState(session, camera, &state);
    p_ArCamera_getPose(session, camera, static_cast<ArPose *>(m_pose));
    float pose[16] = {};
    p_ArPose_getMatrix(session, static_cast<ArPose *>(m_pose), pose);
    p_ArCamera_getImageIntrinsics(session, camera, static_cast<ArCameraIntrinsics *>(m_intrinsics));
    float fx = 0, fy = 0, cx = 0, cy = 0;
    p_ArCameraIntrinsics_getFocalLength(session, static_cast<ArCameraIntrinsics *>(m_intrinsics), &fx, &fy);
    p_ArCameraIntrinsics_getPrincipalPoint(session, static_cast<ArCameraIntrinsics *>(m_intrinsics), &cx, &cy);
    p_ArCameraIntrinsics_getImageDimensions(session, static_cast<ArCameraIntrinsics *>(m_intrinsics),
                                            &m_imageWidth, &m_imageHeight);
    p_ArCamera_getTextureIntrinsics(session, camera,
                                    static_cast<ArCameraIntrinsics *>(m_textureIntrinsics));
    float tfx = 0, tfy = 0, tcx = 0, tcy = 0;
    p_ArCameraIntrinsics_getFocalLength(session, static_cast<ArCameraIntrinsics *>(m_textureIntrinsics), &tfx, &tfy);
    p_ArCameraIntrinsics_getPrincipalPoint(session, static_cast<ArCameraIntrinsics *>(m_textureIntrinsics), &tcx, &tcy);
    p_ArCameraIntrinsics_getImageDimensions(session, static_cast<ArCameraIntrinsics *>(m_textureIntrinsics),
                                            &m_textureWidth, &m_textureHeight);
    const float textureK[9] = {tfx, 0, tcx, 0, tfy, tcy, 0, 0, 1};
    for (int i = 0; i < 9; ++i) m_textureK[i] = textureK[i];
    p_ArCamera_release(camera);
    QMutexLocker lock(&m_mutex);
    for (int i = 0; i < 16; ++i) m_data.pose[i] = pose[i];
    const float k[9] = {fx, 0, cx, 0, fy, cy, 0, 0, 1};
    for (int i = 0; i < 9; ++i) m_data.k[i] = k[i];
    m_data.tracking = state == AR_TRACKING_STATE_TRACKING;
    return true;
#else
    return false;
#endif
}

void ArCoreSession::setDisplaySize(int width, int height)
{
    m_width = width;
    m_height = height;
}

void ArCoreSession::setPreviewResolution(int width, int height)
{
    Q_UNUSED(width);
    Q_UNUSED(height);
    // ARCore camera configuration is selected by device capabilities; no arbitrary size request.
}

bool ArCoreSession::transformDisplayUv(const float *in, float *out, int count)
{
#ifdef Q_OS_ANDROID
    if (!m_session || !m_frame) return false;
    p_ArFrame_transformDisplayUvCoords(sessionOf(m_session), frameOf(m_frame), count, in, out);
    return true;
#else
    Q_UNUSED(in); Q_UNUSED(out); Q_UNUSED(count);
    return false;
#endif
}

void ArCoreSession::imageDimensions(int *width, int *height) const
{
    if (width) *width = m_imageWidth;
    if (height) *height = m_imageHeight;
}

bool ArCoreSession::gpuIntrinsics(float *outK9, int width, int height) const
{
    if (!outK9 || width <= 0 || height <= 0 || m_textureWidth <= 0 || m_textureHeight <= 0)
        return false;
    const float sx = float(width) / float(m_textureWidth);
    const float sy = float(height) / float(m_textureHeight);
    for (int i = 0; i < 9; ++i) outK9[i] = m_textureK[i];
    outK9[0] *= sx; outK9[2] *= sx;
    outK9[4] *= sy; outK9[5] *= sy;
    return true;
}

QVector<float> ArCoreSession::acquirePointCloud()
{
    QVector<float> out;
#ifdef Q_OS_ANDROID
    if (!m_session || !m_frame) return out;
    auto *session = sessionOf(m_session);
    ArPointCloud *cloud = nullptr;
    if (p_ArFrame_acquirePointCloud(session, frameOf(m_frame), &cloud) != AR_SUCCESS || !cloud)
        return out;
    int32_t count = 0;
    const float *data = nullptr;
    p_ArPointCloud_getNumberOfPoints(session, cloud, &count);
    p_ArPointCloud_getData(session, cloud, &data);
    if (data) {
        out.reserve(count * 3);
        for (int32_t i = 0; i < count; ++i) {
            const float *p = data + i * 4;
            if (p[3] >= 0.5f && std::isfinite(p[0]) && std::isfinite(p[1]) && std::isfinite(p[2]))
                out.append(p[0]), out.append(p[1]), out.append(p[2]);
        }
    }
    p_ArPointCloud_release(cloud);
#endif
    return out;
}

QByteArray ArCoreSession::captureJpeg()
{
#ifdef Q_OS_ANDROID
    if (!m_session || !m_frame) return {};
    auto *session = sessionOf(m_session);
    ArImage *image = nullptr;
    if (p_ArFrame_acquireCameraImage(session, frameOf(m_frame), &image) != AR_SUCCESS || !image)
        return {};
    int32_t width = 0, height = 0, count = 0;
    ArImageFormat format = AR_IMAGE_FORMAT_INVALID;
    p_ArImage_getWidth(session, image, &width);
    p_ArImage_getHeight(session, image, &height);
    p_ArImage_getFormat(session, image, &format);
    p_ArImage_getNumberOfPlanes(session, image, &count);
    QByteArray jpeg;
    if (width > 0 && height > 0 && count >= 3 && format == AR_IMAGE_FORMAT_YUV_420_888) {
        const uint8_t *plane[3] = {};
        int32_t lengths[3] = {}, rowStride[3] = {}, pixelStride[3] = {};
        for (int i = 0; i < 3; ++i) {
            p_ArImage_getPlaneData(session, image, i, &plane[i], &lengths[i]);
            p_ArImage_getPlaneRowStride(session, image, i, &rowStride[i]);
            p_ArImage_getPlanePixelStride(session, image, i, &pixelStride[i]);
        }
        if (plane[0] && plane[1] && plane[2]) {
            QImage rgb(width, height, QImage::Format_RGB888);
            for (int y = 0; y < height; ++y) {
                uchar *dst = rgb.scanLine(y);
                for (int x = 0; x < width; ++x) {
                    const int yi = y * rowStride[0] + x * pixelStride[0];
                    const int ui = (y / 2) * rowStride[1] + (x / 2) * pixelStride[1];
                    const int vi = (y / 2) * rowStride[2] + (x / 2) * pixelStride[2];
                    if (yi >= lengths[0] || ui >= lengths[1] || vi >= lengths[2]) continue;
                    const int Y = plane[0][yi], U = int(plane[1][ui]) - 128, V = int(plane[2][vi]) - 128;
                    dst[x * 3] = uchar(qBound(0, Y + int(1.402f * V), 255));
                    dst[x * 3 + 1] = uchar(qBound(0, Y - int(0.344136f * U) - int(0.714136f * V), 255));
                    dst[x * 3 + 2] = uchar(qBound(0, Y + int(1.772f * U), 255));
                }
            }
            QBuffer buffer(&jpeg);
            buffer.open(QIODevice::WriteOnly);
            rgb.save(&buffer, "JPG", 85);
        }
    }
    p_ArImage_release(image);
    return jpeg;
#else
    return {};
#endif
}

ArSessionBackend::FrameData ArCoreSession::frame() const
{
    QMutexLocker lock(&m_mutex);
    return m_data;
}

void ArCoreSession::shutdown()
{
#ifdef Q_OS_ANDROID
    if (!m_session) return;
    auto *session = sessionOf(m_session);
    if (m_cameraOn) p_ArSession_pause(session);
    if (m_intrinsics) p_ArCameraIntrinsics_destroy(static_cast<ArCameraIntrinsics *>(m_intrinsics));
    if (m_textureIntrinsics) p_ArCameraIntrinsics_destroy(static_cast<ArCameraIntrinsics *>(m_textureIntrinsics));
    if (m_pose) p_ArPose_destroy(static_cast<ArPose *>(m_pose));
    if (m_frame) p_ArFrame_destroy(frameOf(m_frame));
    p_ArSession_destroy(session);
    m_textureIntrinsics = m_intrinsics = m_pose = m_frame = m_session = nullptr;
    m_cameraOn = false;
#endif
}
