#pragma once

#include <QObject>
#include <QString>

// 服务器地址（网页入口 URL）配置。
//
// App 是「网页 client 的原生壳」：WebView 加载的地址由 App 私有目录下的
// home_url.txt 决定（见 main.cpp 的 resolveHomeUrl）。地址一旦填错，
// 网页根本加载不出来，而 adb 又未必可用 —— 所以必须提供一个**完全不依赖
// 网页**的原生修改入口（WebShell.qml 顶部工具条的「⚙ 服务器」）。
//
// 本类只负责「读 / 校验 / 写」这一个文件，不改动任何业务逻辑，
// 与 main.cpp 的启动期解析保持同一份路径约定。
class ServerConfig : public QObject
{
    Q_OBJECT
public:
    explicit ServerConfig(QObject *parent = nullptr);

    static QString defaultUrl();   // 内置默认地址
    static QString filePath();     // home_url.txt 的绝对路径

    Q_INVOKABLE QString defaultUrlString() const;   // QML 侧取默认值
    Q_INVOKABLE QString current() const;            // 当前生效地址
    Q_INVOKABLE QString validate(const QString &url) const;  // 空串=合法
    Q_INVOKABLE bool save(const QString &url);      // 校验通过才落盘
    Q_INVOKABLE bool resetToDefault();              // 恢复默认并落盘
};
