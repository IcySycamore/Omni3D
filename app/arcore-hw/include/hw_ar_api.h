// 华为 AR Engine NDK C API 声明（自写，基于 libhuawei_arengine_ndk.so 导出符号 + 官方兼容声明）
// 华为官方声明 API 与 Google ARCore 对齐；本头文件仅声明本项目用到的子集。
#ifndef HW_AR_API_H
#define HW_AR_API_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct HwArSession_ HwArSession;
typedef struct HwArFrame_ HwArFrame;
typedef struct HwArCamera_ HwArCamera;
typedef struct HwArCameraIntrinsics_ HwArCameraIntrinsics;
typedef struct HwArPose_ HwArPose;
typedef struct HwArConfig_ HwArConfig;
typedef struct HwArImage_ HwArImage;
typedef struct HwArPointCloud_ HwArPointCloud;

// ---- AREngine Server 检测/安装（HarmonyOS 4.0+ 需应用自集成 Server APK）----
typedef enum HwArAvailability {
    HWAR_AVAILABILITY_UNKNOWN_ERROR = 0,
    HWAR_AVAILABILITY_UNKNOWN_CHECKING = 1,
    HWAR_AVAILABILITY_UNKNOWN_TIMED_OUT = 2,
    HWAR_AVAILABILITY_UNSUPPORTED_DEVICE_NOT_CAPABLE = 100,
    HWAR_AVAILABILITY_SUPPORTED_NOT_INSTALLED = 201,
    HWAR_AVAILABILITY_SUPPORTED_APK_TOO_OLD = 202,
    HWAR_AVAILABILITY_SUPPORTED_INSTALLED = 203,
} HwArAvailability;

typedef enum HwArInstallStatus {
    HWAR_INSTALL_STATUS_INSTALL_REQUESTED = 0,
    HWAR_INSTALL_STATUS_INSTALL_REQUESTED_NOT_CURRENTLY_AVAILABLE = 100,
    HWAR_INSTALL_STATUS_INSTALLED = 200,
    HWAR_INSTALL_STATUS_INSTALL_FAILED = 201,
} HwArInstallStatus;

// 检查设备是否已安装 AR Engine Service 且与 SDK 版本兼容（官方推荐，替代废弃的 checkAvailability）
int HwArEnginesApk_isAREngineApkReady(void *env, void *context);
// 检测 AR Engine Service 可用性（旧 API，仅供诊断）
void HwArEnginesApk_checkAvailability(void *env, void *context, HwArAvailability *out_availability);
// 请求安装/更新 AR Engine Service（旧 API；HarmonyOS 4.0+ 应自行安装 assets 内 Server APK）
void HwArEnginesApk_requestInstall(void *env, void *activity, int user_requested_install,
                                   HwArInstallStatus *out_install_status);

typedef enum HwArStatus {
    HWAR_SUCCESS = 0,
    HWAR_ERROR_INVALID_ARGUMENT = -1,
    HWAR_ERROR_NOT_YET_AVAILABLE = -2,
    HWAR_ERROR_DEADLINE_EXCEEDED = -3,
    HWAR_ERROR_UNSUPPORTED = -4,
    HWAR_ERROR_RESOURCE_EXHAUSTED = -6,
    HWAR_ERROR_ILLEGAL_STATE = -8,
} HwArStatus;

typedef enum HwArTrackingState {
    HWAR_TRACKING_STATE_STOPPED = 0,
    HWAR_TRACKING_STATE_PAUSED = 1,
    HWAR_TRACKING_STATE_TRACKING = 2,
} HwArTrackingState;

typedef enum HwArUpdateMode {
    HWAR_UPDATE_MODE_BLOCKING = 0,
    HWAR_UPDATE_MODE_LATEST_CAMERA_IMAGE = 1,
} HwArUpdateMode;

typedef enum HwArCoordinates2dType {
    HWAR_COORDINATES_2D_TEXTURE_TEXELS = 0,
    HWAR_COORDINATES_2D_TEXTURE_NORMALIZED = 1,
    HWAR_COORDINATES_2D_IMAGE_PIXELS = 2,
    HWAR_COORDINATES_2D_IMAGE_NORMALIZED = 3,
    HWAR_COORDINATES_2D_OPENGL_NORMALIZED_DEVICE_COORDINATES = 6,
    HWAR_COORDINATES_2D_VIEW = 7,
    HWAR_COORDINATES_2D_VIEW_NORMALIZED = 8,
} HwArCoordinates2dType;

// ---- Session ----
HwArStatus HwArSession_create(void *env, void *applicationContext, HwArSession **out_session);
void HwArSession_destroy(HwArSession *session);
HwArStatus HwArSession_checkSupported(void *env, void *context, HwArSession *session);
HwArStatus HwArSession_configure(HwArSession *session, const HwArConfig *config);
HwArStatus HwArSession_resume(HwArSession *session);
HwArStatus HwArSession_pause(HwArSession *session);
void HwArSession_setCameraTextureName(HwArSession *session, uint32_t texture_id);
void HwArSession_setDisplayGeometry(HwArSession *session, int32_t rotation, int32_t width, int32_t height);
HwArStatus HwArSession_update(HwArSession *session, HwArFrame *out_frame);

// ---- Config ----
void HwArConfig_create(const HwArSession *session, HwArConfig **out_config);
void HwArConfig_destroy(HwArConfig *config);
void HwArConfig_setUpdateMode(const HwArSession *session, HwArConfig *config, HwArUpdateMode update_mode);
// 设置相机预览/采集分辨率（官方 FaceActivity 示例：config.setPreviewSize(w, h)）
void HwArConfig_setPreviewSize(const HwArSession *session, HwArConfig *config,
                               int32_t width, int32_t height);

// ---- Frame ----
void HwArFrame_create(const HwArSession *session, HwArFrame **out_frame);
void HwArFrame_destroy(HwArFrame *frame);
HwArStatus HwArFrame_acquireCamera(const HwArSession *session, const HwArFrame *frame, HwArCamera **out_camera);
void HwArFrame_transformDisplayUvCoords(const HwArSession *session, const HwArFrame *frame,
                                        int32_t num_elements, const float *uvs_in, float *uvs_out);
// 取相机 CPU 图像帧（YUV_420_888），用于扫描采集 JPEG
HwArStatus HwArFrame_acquireCameraImage(const HwArSession *session, const HwArFrame *frame,
                                        HwArImage **out_image);
// 转 NDK AImage（需 <media/NdkImage.h>，此处用 void* 保持头文件可移植）
void HwArImage_getNdkImage(const HwArSession *session, const HwArImage *image, void **out_ndk_image);
void HwArImage_release(HwArImage *image);

// ---- PointCloud（SLAM 稀疏点云，世界坐标系；与 ARCore 同构签名）----
HwArStatus HwArFrame_acquirePointCloud(const HwArSession *session, const HwArFrame *frame,
                                       HwArPointCloud **out_point_cloud);
void HwArPointCloud_getNumberOfPoints(const HwArSession *session, const HwArPointCloud *point_cloud,
                                      int32_t *out_number_of_points);
// 每点 4 个 float：X, Y, Z, 置信度（0~1）
void HwArPointCloud_getData(const HwArSession *session, const HwArPointCloud *point_cloud,
                            const float **out_point_cloud_data);
void HwArPointCloud_getTimestamp(const HwArSession *session, const HwArPointCloud *point_cloud,
                                 int64_t *out_timestamp_ns);
void HwArPointCloud_release(HwArPointCloud *point_cloud);

// ---- Camera ----
void HwArCamera_getTrackingState(const HwArSession *session, const HwArCamera *camera, HwArTrackingState *out_state);
void HwArCamera_getImageIntrinsics(const HwArSession *session, const HwArCamera *camera, HwArCameraIntrinsics *out_intrinsics);
void HwArCamera_getDisplayOrientedPose(const HwArSession *session, const HwArCamera *camera, HwArPose *out_pose);
void HwArCamera_release(HwArCamera *camera);

// ---- CameraIntrinsics ----
void HwArCameraIntrinsics_create(const HwArSession *session, HwArCameraIntrinsics **out_intrinsics);
void HwArCameraIntrinsics_getFocalLength(const HwArSession *session, const HwArCameraIntrinsics *intrinsics, float *out_fx, float *out_fy);
void HwArCameraIntrinsics_getPrincipalPoint(const HwArSession *session, const HwArCameraIntrinsics *intrinsics, float *out_cx, float *out_cy);
void HwArCameraIntrinsics_getImageDimensions(const HwArSession *session, const HwArCameraIntrinsics *intrinsics, int32_t *out_width, int32_t *out_height);
void HwArCameraIntrinsics_destroy(HwArCameraIntrinsics *intrinsics);

// ---- Pose ----
void HwArPose_create(const HwArSession *session, const float *pose_raw, HwArPose **out_pose);
void HwArPose_getMatrix(const HwArSession *session, const HwArPose *pose, float *out_matrix_col_major_4x4);
void HwArPose_destroy(HwArPose *pose);

#ifdef __cplusplus
}
#endif

#endif // HW_AR_API_H
