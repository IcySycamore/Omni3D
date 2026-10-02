#include "sensor_reader.h"

#include <QJniEnvironment>
#include <QJniObject>
#include <QTimer>

#ifdef Q_OS_ANDROID
#include <QtCore/qcoreapplication.h>
#include <QtCore/qnativeinterface.h>
#endif

SensorReader::SensorReader(QObject *parent)
    : QObject(parent)
{
}

SensorReader *SensorReader::instance()
{
    static SensorReader s;
    return &s;
}

void SensorReader::start()
{
#ifdef Q_OS_ANDROID
    QJniObject ctx = QNativeInterface::QAndroidApplication::context();
    if (!ctx.isValid())
        return;
    QJniObject::callStaticMethod<void>(
        "com/omni3d/capture/ARHelper", "startSensors",
        "(Landroid/content/Context;)V", ctx.object());
    m_active = true;
    // 周期性刷新给 QML（传感器回调进静态变量，C++ 轮询读）
    QTimer *t = new QTimer(this);
    t->setInterval(66); // ~15fps
    connect(t, &QTimer::timeout, this, [this] {
        m_yaw = QJniObject::callStaticMethod<jfloat>(
            "com/omni3d/capture/ARHelper", "getYaw", "()F");
        m_pitch = QJniObject::callStaticMethod<jfloat>(
            "com/omni3d/capture/ARHelper", "getPitch", "()F");
        m_roll = QJniObject::callStaticMethod<jfloat>(
            "com/omni3d/capture/ARHelper", "getRoll", "()F");
        emit poseChanged();
    });
    t->start();
#endif
}

void SensorReader::stop()
{
#ifdef Q_OS_ANDROID
    QJniObject ctx = QNativeInterface::QAndroidApplication::context();
    if (ctx.isValid())
        QJniObject::callStaticMethod<void>(
            "com/omni3d/capture/ARHelper", "stopSensors",
            "(Landroid/content/Context;)V", ctx.object());
#endif
    m_active = false;
    emit poseChanged();
}
