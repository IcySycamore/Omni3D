#include <QApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickStyle>
#include <QUrl>
#include <QTimer>
#include <QFile>
#include <QDir>
#include <QStandardPaths>

#ifdef Q_OS_ANDROID
#include <QJniObject>
#include <QtCore/qcoreapplication.h>
#include <QtCore/qnativeinterface.h>
#endif

#include "sensor_reader.h"
#include "ar_bridge_server.h"
#include "ar_scan_controller.h"
#include "ar_scan_preview.h"
#include "hw_ar_engine_session.h"

#ifdef Q_OS_ANDROID
#include <android/log.h>
#define PROBE(m) __android_log_print(ANDROID_LOG_INFO, "OmniProbe", "%s", m)
#else
#include <cstdio>
#define PROBE(m) std::fprintf(stderr, "OmniProbe: %s\n", m)
#endif

// ============================================================
// Omni3D App —— 网页浏览器壳
//   网页（采集/3D/测量/历史主体）+ 本地 HTTP 桥 127.0.0.1:50687
//   桥能力：
//     1) /ar/status  /ar/pose  —— AR 位姿（华为 AREngine 真实尺度优先，
//        不可用时回退传感器旋转位姿，scale=1 由网页标尺校准）
//     2) /ar/history —— 历史持久化（App 私有目录 JSON）
// ============================================================

static bool s_arEngineReady = false;   // AREngine 会话是否可用
static bool s_arEngineTried = false;

// 网页入口地址（默认 adb reverse 的 127.0.0.1:50865；frp 隧道可覆盖）：
//  1) Android Intent extra "homeUrl"（如 am start --es homeUrl https://xxx）→ 写入
//     私有目录 home_url.txt 持久化（脱离 adb 后仍生效）
//  2) App 私有目录 home_url.txt（也可 adb push 切换）
//  3) 默认 http://127.0.0.1:50685/
static QString resolveHomeUrl()
{
    QString url = QStringLiteral("http://127.0.0.1:50865/");
    QString fileUrl;
#ifdef Q_OS_ANDROID
    // 2) 私有目录 home_url.txt（上次持久化 / adb push）
    const QString f =
        QStandardPaths::writableLocation(QStandardPaths::AppDataLocation)
        + QStringLiteral("/home_url.txt");
    QFile file(f);
    if (file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        fileUrl = QString::fromUtf8(file.readAll()).trimmed();
        if (!fileUrl.isEmpty())
            url = fileUrl;
    }
    // 1) Intent extra 优先，并持久化
    QJniObject activity = QNativeInterface::QAndroidApplication::context();
    if (activity.isValid()) {
        QJniObject intent = activity.callObjectMethod(
            "getIntent", "()Landroid/content/Intent;");
        if (intent.isValid()) {
            QJniObject extra = intent.callObjectMethod(
                "getStringExtra", "(Ljava/lang/String;)Ljava/lang/String;",
                QJniObject::fromString(QStringLiteral("homeUrl")).object());
            if (extra.isValid()) {
                const QString v = extra.toString();
                if (!v.isEmpty() && v != fileUrl) {
                    url = v;
                    QFile out(f);
                    if (out.open(QIODevice::WriteOnly | QIODevice::Text)) {
                        out.write(v.toUtf8());
                        out.close();
                    }
                }
            }
        }
    }
#endif
    PROBE(("homeUrl: " + url.toUtf8()).constData());
    return url;
}

// 周期放行混合内容：frp HTTPS 页面 fetch http://127.0.0.1:50687（本地桥）需要
// WebView 允许混合内容。Qt 无此 API → 调 ARHelper 遍历 View 树设置
// （WebView 在 QML 加载后才创建，故持续调用一段时间兜底）
static void scheduleMixedContentAllow(QObject *parent)
{
#ifdef Q_OS_ANDROID
    QTimer *t = new QTimer(parent);
    int *tries = new int(40); // 最多 40 次 × 1s ≈ 40s
    QObject::connect(t, &QTimer::timeout, parent, [t, tries]() {
        QJniObject ctx = QNativeInterface::QAndroidApplication::context();
        if (!ctx.isValid())
            return;
        QJniObject::callStaticMethod<void>(
            "com/omni3d/capture/ARHelper", "enableMixedContent",
            "(Landroid/content/Context;)V", ctx.object());
        if (--(*tries) <= 0)
            t->stop();
    });
    t->start(1000);
#endif
}

// 初始化 AREngine（仅一次；WebView 壳无相机喂帧时 tracking=false → 回退传感器）
static void ensureArEngineInitialized()
{
    if (s_arEngineTried)
        return;
    s_arEngineTried = true;
    if (HwArEngineSession::serverReady()) {
        PROBE("ARENGINE: server ready, init...");
        // 相机纹理：默认传 0（无相机帧时 tracking=false → 回退传感器）；
        // AR 扫描时由预览渲染器创建 OES 纹理并 applyCameraTexture 启动相机
        s_arEngineReady = HwArEngineSession::instance()->initialize(0);
        PROBE(s_arEngineReady ? "ARENGINE: init OK" : "ARENGINE: init FAIL (fallback sensor)");
        ArScanController::instance()->setAvailable(s_arEngineReady);
    } else {
        PROBE("ARENGINE: server NOT ready (fallback sensor)");
        ArScanController::instance()->setAvailable(false);
    }
}

// 周期性推送位姿到桥：AREngine 可用 → AREngine 位姿；否则传感器旋转
static void feedPoseToBridge()
{
    ensureArEngineInitialized();

    // 优先 AREngine
    if (s_arEngineReady) {
        if (HwArEngineSession::instance()->glOwned()) {
            // 渲染线程已接管 AREngine（扫描中，GL 上下文线程），主线程不再 update
            return;
        }
        HwArEngineSession::instance()->update();
        const auto f = HwArEngineSession::instance()->frame();
        if (f.tracking) {
            QVector<float> p;
            for (int i = 0; i < 16; ++i)
                p.append(f.pose[i]);
            ArBridgeServer::instance()->updatePose(p, true, 1.0f);
            return;
        }
        // AREngine 未跟踪 → 回退传感器
    }

    // 传感器旋转位姿（col-major 4x4，无平移，scale=1）
    const float cy = qCos(qDegreesToRadians(SensorReader::instance()->yaw()));
    const float sy = qSin(qDegreesToRadians(SensorReader::instance()->yaw()));
    const float cp = qCos(qDegreesToRadians(SensorReader::instance()->pitch()));
    const float sp = qSin(qDegreesToRadians(SensorReader::instance()->pitch()));
    const float cr = qCos(qDegreesToRadians(SensorReader::instance()->roll()));
    const float sr = qSin(qDegreesToRadians(SensorReader::instance()->roll()));
    QVector<float> p(16, 0.0f);
    p[0]  = cy * cp;
    p[1]  = sy * cp;
    p[2]  = -sp;
    p[4]  = cy * sp * sr - sy * cr;
    p[5]  = sy * sp * sr + cy * cr;
    p[6]  = cp * sr;
    p[8]  = cy * sp * cr + sy * sr;
    p[9]  = sy * sp * cr - cy * sr;
    p[10] = cp * cr;
    p[15] = 1.0f;
    ArBridgeServer::instance()->updatePose(p, true, 1.0f);
}

int main(int argc, char *argv[])
{
    PROBE("1: main start (WebShell + AREngine)");
    QApplication app(argc, argv);
    QQuickStyle::setStyle("Basic");
    PROBE("3: style ok");

    qmlRegisterSingletonType<SensorReader>(
        "Omni3D", 1, 0, "Sensor",
        [](QQmlEngine *, QJSEngine *) -> QObject * {
            return SensorReader::instance();
        });
    qmlRegisterSingletonType<ArScanController>(
        "Omni3D", 1, 0, "Scan",
        [](QQmlEngine *, QJSEngine *) -> QObject * {
            return ArScanController::instance();
        });
    qmlRegisterType<ArScanPreview>("Omni3D", 1, 0, "ArScanPreview");

    // 本地 HTTP 桥（AR 位姿 + 历史持久化）；端口避开 50685/50686
    const bool bridgeOk = ArBridgeServer::instance()->start(50687);
    PROBE(bridgeOk ? "BRIDGE: started on 50687" : "BRIDGE: FAILED to start");

    SensorReader::instance()->start();
    QTimer *poseTimer = new QTimer(&app);
    QObject::connect(poseTimer, &QTimer::timeout, &feedPoseToBridge);
    poseTimer->start(100); // 10Hz

    // 提前初始化 AREngine（QML 加载前）：ArScanPreview 渲染线程可能先于
    // feedPoseToBridge 创建 OES 纹理并 applyCameraTexture，必须保证会话已就绪
    ensureArEngineInitialized();

    QQmlApplicationEngine engine;
    PROBE("6: engine created");
    // 网页入口可配置（frp 隧道 https 域名 / adb 默认 127.0.0.1:50685）
    const QString homeUrl = resolveHomeUrl();
    engine.rootContext()->setContextProperty("initialHomeUrl", homeUrl);
    QObject::connect(&engine, &QQmlEngine::warnings,
                     [](const QList<QQmlError> &ws) {
                         for (const auto &e : ws)
                             __android_log_print(ANDROID_LOG_ERROR, "OmniProbe", "QMLERR: %s",
                                                 e.toString().toUtf8().constData());
                     });
    QObject::connect(&engine, &QQmlApplicationEngine::objectCreated,
                     [](QObject *obj, const QUrl &url) {
                         __android_log_print(ANDROID_LOG_INFO, "OmniProbe",
                                             "OBJ: %s %s", url.toString().toUtf8().constData(),
                                             obj ? "OK" : "FAIL");
                     });

    engine.load(QUrl(QStringLiteral("qrc:/Omni3D/qml/WebShell.qml")));
    PROBE("7: load qrc done");
    if (engine.rootObjects().isEmpty()) {
        PROBE("8: rootObjects EMPTY -> return -1");
        return -1;
    }
    PROBE("8: rootObjects OK -> exec");

    // frp HTTPS 场景：周期放行 WebView 混合内容（fetch 本地桥）
    scheduleMixedContentAllow(&engine);

    // 网页触发 /ar/scan/start → 显示扫描覆盖层
    QObject::connect(ArBridgeServer::instance(), &ArBridgeServer::scanRequested,
                     &engine, [&engine]() {
                         for (auto *obj : engine.rootObjects()) {
                             QObject *page = obj->findChild<QObject *>(QStringLiteral("scanPage"));
                             if (page) {
                                 page->setProperty("visible", true);
                                 break;
                             }
                         }
                     });

    return app.exec();
}
