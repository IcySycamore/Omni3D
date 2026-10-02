#include "server_config.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QStandardPaths>
#include <QUrl>

namespace {
const char *kDefaultUrl = "http://127.0.0.1:50865/";
const char *kFileName = "home_url.txt";
} // namespace

ServerConfig::ServerConfig(QObject *parent) : QObject(parent) {}

QString ServerConfig::defaultUrl()
{
    return QString::fromLatin1(kDefaultUrl);
}

QString ServerConfig::filePath()
{
    // 与 main.cpp resolveHomeUrl() 使用的是同一路径（Android 上为 App 私有目录）
    const QString dir =
        QStandardPaths::writableLocation(QStandardPaths::AppDataLocation);
    return dir + QStringLiteral("/") + QString::fromLatin1(kFileName);
}

QString ServerConfig::defaultUrlString() const
{
    return defaultUrl();
}

QString ServerConfig::current() const
{
    QFile file(filePath());
    if (file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        const QString value = QString::fromUtf8(file.readAll()).trimmed();
        file.close();
        if (!value.isEmpty())
            return value;
    }
    return defaultUrl();
}

QString ServerConfig::validate(const QString &url) const
{
    const QString value = url.trimmed();
    if (value.isEmpty())
        return QStringLiteral("地址不能为空");

    const QUrl parsed(value);
    if (!parsed.isValid())
        return QStringLiteral("地址格式无效");

    const QString scheme = parsed.scheme().toLower();
    if (scheme != QStringLiteral("http") && scheme != QStringLiteral("https"))
        return QStringLiteral("地址必须以 http:// 或 https:// 开头");
    if (parsed.host().isEmpty())
        return QStringLiteral("地址缺少主机名（例如 127.0.0.1）");

    return QString();
}

bool ServerConfig::save(const QString &url)
{
    const QString value = url.trimmed();
    if (!validate(value).isEmpty())
        return false;

    const QString path = filePath();
    QDir().mkpath(QFileInfo(path).absolutePath());

    QFile file(path);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate | QIODevice::Text))
        return false;
    const qint64 written = file.write(value.toUtf8());
    file.close();
    return written == value.toUtf8().size();
}

bool ServerConfig::resetToDefault()
{
    return save(defaultUrl());
}
