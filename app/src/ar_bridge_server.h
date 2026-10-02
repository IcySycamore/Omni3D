#pragma once

#include <QObject>
#include <QTcpServer>
#include <QTcpSocket>
#include <QMutex>
#include <QVector>

/**
 * App 本地 HTTP 桥服务（127.0.0.1:50687）。
 *
 * 网页（WebView 或外部浏览器）通过 fetch 轮询本服务：
 *   GET /ar/status   -> {"ok":true,"ready":true,"tracking":bool,"scale":float}
 *   GET /ar/pose     -> {"ok":true,"pose":[16 floats col-major 4x4],"tracking":bool}
 *   GET /ar/history  -> {"ok":true,"tasks":[...]}     （历史持久化）
 *   POST /ar/history -> 保存一条历史（JSON body）
 *   GET /ar/health   -> {"ok":true}
 * 所有响应带 CORS 头（Access-Control-Allow-Origin:*），供外部页面跨域访问。
 */
class ArBridgeServer : public QObject
{
    Q_OBJECT
public:
    static ArBridgeServer *instance();

    bool start(quint16 port = 50687);
    void stop();

    // 由 AR 后端 / 传感器每帧更新
    void updatePose(const QVector<float> &pose4x4ColMajor, bool tracking, float scale = 1.0f);

    // 历史持久化：load 从本地文件读取，save 追加
    QVector<QJsonObject> loadHistory();
    void saveHistory(const QJsonObject &task);

signals:
    // 网页触发 /ar/scan/start 时发出，QML 收到后显示扫描页
    void scanRequested();

private:
    explicit ArBridgeServer(QObject *parent = nullptr);
    void handleClient(QTcpSocket *sock);
    void handleRequest(QTcpSocket *sock, const QByteArray &method, const QByteArray &path, const QByteArray &body);
    QByteArray jsonReply(const QJsonObject &obj);
    void writeCors(QTcpSocket *sock, const QByteArray &body, int status = 200,
                   const QByteArray &contentType = "application/json",
                   const QByteArray &extraHeaders = QByteArray());

    QTcpServer m_server;
    QMutex m_mutex;
    QVector<float> m_pose = {1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1};
    bool m_tracking = false;
    float m_scale = 1.0f;
    QString m_historyFile;
};
