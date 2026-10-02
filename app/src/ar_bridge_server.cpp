#include "ar_bridge_server.h"
#include "ar_scan_controller.h"

#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <QFile>
#include <QFileInfo>
#include <QDir>
#include <QStandardPaths>
#include <QFileDialog>
#include <QUrl>
#ifdef Q_OS_ANDROID
#include <QJniObject>
#include <QJniEnvironment>
#include <QtCore/qcoreapplication.h>
#include <QtCore/qnativeinterface.h>
#endif

#ifdef Q_OS_ANDROID
static bool saveToDownloads(const QString &filename, const QByteArray &data);
#endif

ArBridgeServer::ArBridgeServer(QObject *parent)
    : QObject(parent)
{
    m_historyFile = QStandardPaths::writableLocation(QStandardPaths::AppDataLocation)
                    + QStringLiteral("/omni3d_history.json");
}

ArBridgeServer *ArBridgeServer::instance()
{
    static ArBridgeServer s;
    return &s;
}

bool ArBridgeServer::start(quint16 port)
{
    QDir().mkpath(QFileInfo(m_historyFile).absolutePath());
    const bool ok = m_server.listen(QHostAddress::LocalHost, port);
    if (ok) {
        QObject::connect(&m_server, &QTcpServer::newConnection, this, [this]() {
            while (m_server.hasPendingConnections()) {
                QTcpSocket *sock = m_server.nextPendingConnection();
                handleClient(sock);
            }
        });
    }
    return ok;
}

void ArBridgeServer::stop()
{
    m_server.close();
}

void ArBridgeServer::updatePose(const QVector<float> &pose4x4ColMajor, bool tracking, float scale)
{
    QMutexLocker lk(&m_mutex);
    if (pose4x4ColMajor.size() >= 16)
        m_pose = pose4x4ColMajor;
    m_tracking = tracking;
    m_scale = scale;
}

QVector<QJsonObject> ArBridgeServer::loadHistory()
{
    QVector<QJsonObject> out;
    QFile f(m_historyFile);
    if (!f.open(QIODevice::ReadOnly))
        return out;
    const QJsonDocument doc = QJsonDocument::fromJson(f.readAll());
    f.close();
    if (doc.isArray()) {
        for (const auto &v : doc.array())
            out.append(v.toObject());
    }
    return out;
}

void ArBridgeServer::saveHistory(const QJsonObject &task)
{
    auto tasks = loadHistory();
    tasks.prepend(task);
    while (tasks.size() > 50)
        tasks.removeLast();
    QJsonArray arr;
    for (const auto &t : tasks)
        arr.append(t);
    QFile f(m_historyFile);
    if (f.open(QIODevice::WriteOnly))
        f.write(QJsonDocument(arr).toJson(QJsonDocument::Compact));
}

void ArBridgeServer::writeCors(QTcpSocket *sock, const QByteArray &body, int status,
                               const QByteArray &contentType, const QByteArray &extraHeaders)
{
    QByteArray head;
    head += "HTTP/1.1 " + QByteArray::number(status) + " OK\r\n";
    head += "Access-Control-Allow-Origin: *\r\n";
    head += "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n";
    head += "Access-Control-Allow-Headers: Content-Type\r\n";
    // ⚠️ 必须暴露自定义响应头，否则浏览器端 resp.headers.get("X-Filename")
    //    读不到（默认只暴露 safelist 头）→ 文件名永远回退 "media"
    head += "Access-Control-Expose-Headers: X-Filename, Content-Disposition\r\n";
    head += "Content-Type: " + contentType + "\r\n";
    head += extraHeaders;
    head += "Content-Length: " + QByteArray::number(body.size()) + "\r\n";
    head += "Connection: close\r\n\r\n";
    sock->write(head + body);
    sock->flush();
    sock->disconnectFromHost();
}

QByteArray ArBridgeServer::jsonReply(const QJsonObject &obj)
{
    return QJsonDocument(obj).toJson(QJsonDocument::Compact);
}

void ArBridgeServer::handleRequest(QTcpSocket *sock, const QByteArray &method,
                                   const QByteArray &path, const QByteArray &body)
{
    if (method == "OPTIONS") {
        writeCors(sock, QByteArray(), 204);
        return;
    }

    if (path.startsWith("/ar/status")) {
        QMutexLocker lk(&m_mutex);
        writeCors(sock, jsonReply({{"ok", true}, {"ready", true},
                                   {"tracking", m_tracking}, {"scale", m_scale}}));
        return;
    }    // ---- 手机端原生文件选择（弹系统文件选择器，返回文件二进制）----
    if (path.startsWith("/ar/file/pick")) {
#ifdef Q_OS_ANDROID
        const QString file = QFileDialog::getOpenFileName(
            nullptr, QStringLiteral("选择视频/图片"), QString(),
            QStringLiteral("媒体 (*.mp4 *.webm *.avi *.mov *.3gp *.jpg *.jpeg *.png);;所有文件 (*)"));
        if (file.isEmpty()) {
            writeCors(sock, jsonReply({{ "ok", false }, { "error", QStringLiteral("已取消") }}));
            return;
        }
        QFile f(file);
        if (!f.open(QIODevice::ReadOnly)) {
            writeCors(sock, jsonReply({{ "ok", false }, { "error", QStringLiteral("读取失败") }}));
            return;
        }
        const QByteArray data = f.readAll();
        const QByteArray nameEnc = QFileInfo(file).fileName().toUtf8().toPercentEncoding();
        writeCors(sock, data, 200, "application/octet-stream",
                  "X-Filename: " + nameEnc + "\r\n");
#else
        writeCors(sock, jsonReply({{ "ok", false }, { "error", QStringLiteral("仅 App 内可用") }}));
#endif
        return;
    }
    // ---- 手机端原生文件保存（PLY → 系统下载目录）----
    if (path.startsWith("/ar/file/save")) {
#ifdef Q_OS_ANDROID
        QString filename = QStringLiteral("model.ply");
        const int qi = path.indexOf('?');
        if (qi >= 0) {
            const QString q = QString::fromUtf8(path.mid(qi + 1));
            if (q.startsWith(QStringLiteral("name="))) {
                filename = QUrl::fromPercentEncoding(q.mid(5).toUtf8());
            }
            if (filename.isEmpty())
                filename = QStringLiteral("model.ply");
        }
        const bool ok = saveToDownloads(filename, body);
        writeCors(sock, jsonReply({{ "ok", ok },
                                   { "path", ok ? ("Downloads/" + filename) : QString() },
                                   { "error", ok ? QString() : QStringLiteral("保存失败") }}));
#else
        writeCors(sock, jsonReply({{ "ok", false }, { "error", QStringLiteral("仅 App 内可用") }}));
#endif
        return;
    }    if (path.startsWith("/ar/pose")) {
        QMutexLocker lk(&m_mutex);
        QJsonArray p;
        for (float v : m_pose)
            p.append(static_cast<double>(v));
        writeCors(sock, jsonReply({{"ok", true}, {"pose", p}, {"tracking", m_tracking}}));
        return;
    }
    if (path.startsWith("/ar/history") && method == "GET") {
        const auto tasks = loadHistory();
        QJsonArray arr;
        for (const auto &t : tasks)
            arr.append(t);
        writeCors(sock, jsonReply({{"ok", true}, {"tasks", arr}}));
        return;
    }
    if (path.startsWith("/ar/history") && method == "POST") {
        QJsonParseError err;
        const QJsonDocument doc = QJsonDocument::fromJson(body, &err);
        if (err.error == QJsonParseError::NoError && doc.isObject())
            saveHistory(doc.object());
        writeCors(sock, jsonReply({{"ok", true}}));
        return;
    }
    if (path.startsWith("/ar/scan/start") && method == "POST") {
        if (!ArScanController::instance()->available()) {
            writeCors(sock, jsonReply({{"ok", false}, {"started", false},
                                       {"error", "AREngine not available"}}));
            return;
        }
        emit scanRequested(); // QML 收到后显示扫描页（页内手动拍摄/录制）
        writeCors(sock, jsonReply({{"ok", true}, {"started", true}}));
        return;
    }
    // ---- 扫描设置：相机采集分辨率（扫描前调用）----
    if (path.startsWith("/ar/scan/settings") && method == "POST") {
        ArScanController *c = ArScanController::instance();
        QJsonParseError perr;
        const QJsonDocument doc = QJsonDocument::fromJson(body, &perr);
        if (perr.error == QJsonParseError::NoError && doc.isObject()) {
            const QJsonObject o = doc.object();
            const int w = o.value("width").toInt(0);
            const int h = o.value("height").toInt(0);
            if (w > 0 && h > 0)
                c->setResolution(w, h);
        }
        writeCors(sock, jsonReply({{"ok", true}}));
        return;
    }
    // ---- 华为 SLAM 稀疏点云（PLY，世界坐标）----
    if (path.startsWith("/ar/scan/pointcloud")) {
        ArScanController *c = ArScanController::instance();
        const QByteArray ply = c->pointCloudPly();
        if (ply.isEmpty()) {
            writeCors(sock, jsonReply({{"ok", false},
                                       {"error", "no point cloud"}}), 404);
            return;
        }
        writeCors(sock, ply, 200, "application/octet-stream",
                  "X-Filename: huawei_pointcloud.ply\r\n");
        return;
    }
    if (path.startsWith("/ar/scan/capture") && method == "POST") {
        ArScanController::instance()->requestCapture();
        writeCors(sock, jsonReply({{"ok", true},
                                   {"frameCount", ArScanController::instance()->frameCount()}}));
        return;
    }
    // ---- 完成扫描（网页"完成"按钮；QML 内按钮直接调 Scan.finish()）----
    if (path.startsWith("/ar/scan/finish") && method == "POST") {
        ArScanController *c = ArScanController::instance();
        c->finish();
        writeCors(sock, jsonReply({{"ok", true}, {"frameCount", c->frameCount()}}));
        return;
    }
    if (path.startsWith("/ar/scan/stop") && method == "POST") {
        ArScanController *c = ArScanController::instance();
        c->stopScan();
        const int n = c->frameCount();
        QJsonObject obj{{"ok", true}, {"frameCount", n},
                        {"scale", c->scale()}, {"tracking", c->tracking()}};
        if (n > 0) {
            // 附带第一帧位姿 + 内参（供网页即时预览/上传）
            QJsonArray pose;
            for (float v : c->poseAt(0))
                pose.append(static_cast<double>(v));
            QJsonArray k;
            for (float v : c->intrinsicsAt(0))
                k.append(static_cast<double>(v));
            obj.insert("pose0", pose);
            obj.insert("intrinsics0", k);
        }
        writeCors(sock, jsonReply(obj));
        return;
    }
    if (path.startsWith("/ar/scan/data")) {
        ArScanController *c = ArScanController::instance();
        const int n = c->frameCount();
        QJsonArray poses, intrinsics;
        for (int i = 0; i < n; ++i) {
            QJsonArray p;
            for (float v : c->poseAt(i))
                p.append(static_cast<double>(v));
            poses.append(p);
            QJsonArray k;
            for (float v : c->intrinsicsAt(i))
                k.append(static_cast<double>(v));
            intrinsics.append(k);
        }
        writeCors(sock, jsonReply({{"ok", true}, {"frameCount", n},
                                   {"scale", c->scale()},
                                   {"poses", poses}, {"intrinsics", intrinsics}}));
        return;
    }
    if (path.startsWith("/ar/scan/status")) {
        ArScanController *c = ArScanController::instance();
        writeCors(sock, jsonReply({{"ok", true},
                                   {"available", c->available()},
                                   {"scanning", c->scanning()},
                                   {"finished", c->finished()},
                                   {"frameCount", c->frameCount()},
                                   {"pointCloudCount", c->pointCloudCount()},
                                   {"tracking", c->tracking()},
                                   {"scale", c->scale()}}));
        return;
    }
    if (path.startsWith("/ar/scan/reset") && method == "POST") {
        ArScanController::instance()->reset();
        writeCors(sock, jsonReply({{"ok", true}}));
        return;
    }
    if (path.startsWith("/ar/scan/frames/")) {
        const int idx = path.mid(QByteArrayLiteral("/ar/scan/frames/").size()).toInt();
        const QByteArray jpeg = ArScanController::instance()->jpegAt(idx);
        if (jpeg.isEmpty()) {
            writeCors(sock, jsonReply({{"ok", false}, {"error", "no frame"}}), 404);
        } else {
            writeCors(sock, jpeg, 200, "image/jpeg");
        }
        return;
    }
    if (path.startsWith("/ar/health")) {
        writeCors(sock, jsonReply({{"ok", true}}));
        return;
    }
    writeCors(sock, jsonReply({{"ok", false}, {"error", "not found"}}), 404);
}

void ArBridgeServer::handleClient(QTcpSocket *sock)
{
    QObject::connect(sock, &QTcpSocket::readyRead, this, [this, sock]() {
        static const QByteArray terminator("\r\n\r\n");
        // 1) 先攒完整的请求头
        if (!sock->property("hdrParsed").toBool()) {
            QByteArray buf = sock->property("buf").isValid()
                                 ? sock->property("buf").toByteArray() : QByteArray();
            buf += sock->readAll();
            sock->setProperty("buf", buf);
            const int hdrEnd = buf.indexOf(terminator);
            if (hdrEnd < 0)
                return; // 等更多
            const QByteArray head = buf.left(hdrEnd);
            const QByteArray bodyFirst = buf.mid(hdrEnd + 4);
            sock->setProperty("buf", QVariant());
            sock->setProperty("hdrParsed", true);
            int sp1 = head.indexOf(' ');
            int sp2 = head.indexOf(' ', sp1 + 1);
            if (sp1 < 0 || sp2 < 0) {
                sock->disconnectFromHost();
                return;
            }
            sock->setProperty("method", head.left(sp1));
            sock->setProperty("path", head.mid(sp1 + 1, sp2 - sp1 - 1));
            int len = 0;
            const QByteArray lhead = head.toLower();
            const int ci = lhead.indexOf("content-length:");
            if (ci >= 0)
                len = lhead.mid(ci + 15).trimmed().toInt();
            sock->setProperty("bodyLen", len);
            sock->setProperty("bodyBuf", bodyFirst);
        }
        // 2) 累积 body 直到完整（大文件会分多次到达）
        QByteArray b = sock->property("bodyBuf").toByteArray();
        b += sock->readAll();
        sock->setProperty("bodyBuf", b);
        const int need = sock->property("bodyLen").toInt();
        if (need > 0 && b.size() < need)
            return; // 等更多
        const QByteArray method = sock->property("method").toByteArray();
        const QByteArray path = sock->property("path").toByteArray();
        handleRequest(sock, method, path, need > 0 ? b.left(need) : b);
    });
    QObject::connect(sock, &QTcpSocket::disconnected, sock, &QObject::deleteLater);
}

// 注册 newConnection 信号
#include <QNetworkInterface>

#ifdef Q_OS_ANDROID
// 保存文件到系统下载目录：优先直接写 /sdcard/Download（鸿蒙兼容层较宽松），
// 失败则回退 MediaStore（标准 scoped storage 路径）
static bool saveToDownloads(const QString &filename, const QByteArray &data)
{
    // 1) 直接写 /sdcard/Download
    const QString dir = QStringLiteral("/storage/emulated/0/Download");
    QDir().mkpath(dir);
    QFile f(dir + QStringLiteral("/") + filename);
    if (f.open(QIODevice::WriteOnly)) {
        f.write(data);
        f.close();
        return true;
    }
    // 2) 回退 MediaStore Downloads
    QJniObject ctx = QNativeInterface::QAndroidApplication::context();
    if (!ctx.isValid())
        return false;
    QJniObject resolver = ctx.callObjectMethod(
        "getContentResolver", "()Landroid/content/ContentResolver;");
    if (!resolver.isValid())
        return false;
    QJniObject values("android/content/ContentValues");
    values.callObjectMethod("put", "(Ljava/lang/String;Ljava/lang/String;)V",
                            QJniObject::fromString(QStringLiteral("DISPLAY_NAME")).object(),
                            QJniObject::fromString(filename).object());
    values.callObjectMethod("put", "(Ljava/lang/String;Ljava/lang/String;)V",
                            QJniObject::fromString(QStringLiteral("MIME_TYPE")).object(),
                            QJniObject::fromString(QStringLiteral("application/octet-stream")).object());
    const QJniObject downloadsUri = QJniObject::getStaticObjectField(
        "android/provider/MediaStore$Downloads", "EXTERNAL_CONTENT_URI", "Landroid/net/Uri;");
    QJniObject uri = resolver.callObjectMethod(
        "insert", "(Landroid/net/Uri;Landroid/content/ContentValues;)Landroid/net/Uri;",
        downloadsUri.object(), values.object());
    if (!uri.isValid() || uri.toString().isEmpty())
        return false;
    QJniObject os = resolver.callObjectMethod(
        "openOutputStream", "(Landroid/net/Uri;)Ljava/io/OutputStream;", uri.object());
    if (!os.isValid())
        return false;
    QJniEnvironment env;
    jbyteArray arr = env->NewByteArray(data.size());
    env->SetByteArrayRegion(arr, 0, data.size(),
                            reinterpret_cast<const jbyte *>(data.constData()));
    os.callMethod<void>("write", "([B)V", arr);
    os.callMethod<void>("close", "()V");
    env->DeleteLocalRef(arr);
    return true;
}
#endif
