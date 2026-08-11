#pragma once

#include <QObject>

/**
 * 安卓原生传感器姿态读取（旋转向量 -> 欧拉角）。
 * 纯 JNI 调 ARHelper（SensorManager），无任何外部 AR 服务依赖。
 */
class SensorReader : public QObject
{
    Q_OBJECT
    Q_PROPERTY(float yaw READ yaw NOTIFY poseChanged)
    Q_PROPERTY(float pitch READ pitch NOTIFY poseChanged)
    Q_PROPERTY(float roll READ roll NOTIFY poseChanged)
    Q_PROPERTY(bool active READ active NOTIFY poseChanged)
public:
    static SensorReader *instance();

    float yaw() const { return m_yaw; }
    float pitch() const { return m_pitch; }
    float roll() const { return m_roll; }
    bool active() const { return m_active; }

    Q_INVOKABLE void start();
    Q_INVOKABLE void stop();

signals:
    void poseChanged();

private:
    explicit SensorReader(QObject *parent = nullptr);
    float m_yaw = 0, m_pitch = 0, m_roll = 0;
    bool m_active = false;
};
