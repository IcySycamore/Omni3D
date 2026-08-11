import QtQuick
import QtQuick.Controls
import QtWebView
import Omni3D

// ============================================================
// WebView 壳：App 作为网页浏览器
//  - 加载 Omni3D 网页主体（采集 / 3D 查看 / 测量 / 历史）
//  - AR 位姿 + 历史持久化由 C++ 本地 HTTP 桥提供（127.0.0.1:50687）
//    （Qt WebView 基于系统 WebView，不支持自定义 JS 注入对象，
//      故采用 fetch 本地端口的跨域桥方案）
// ============================================================
ApplicationWindow {
    id: root
    visible: true
    width: 420
    height: 800
    title: "Omni3D"
    color: "#0B1220"

    // 网页入口：默认 adb reverse 的 http://127.0.0.1:50865/；
    // frp 隧道场景由 C++ 注入 initialHomeUrl（https://域名），
    // 需 WebView 放行混合内容（main.cpp 已周期调用 ARHelper 设置）
    property string homeUrl:
        (typeof initialHomeUrl === "string" && initialHomeUrl.length > 0)
            ? initialHomeUrl
            : "http://127.0.0.1:50865/"

    // 加载状态指示
    Rectangle {
        id: loading
        anchors.fill: parent
        color: "#0B1220"
        z: 99
        Column {
            anchors.centerIn: parent
            spacing: 12
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "◈"
                font.pixelSize: 40
                color: "#22D3EE"
            }
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "Omni3D · 加载中…"
                font.pixelSize: 14
                color: "#94A3B8"
            }
        }
        MouseArea { anchors.fill: parent }  // 阻止点击
    }

    // 顶部小工具条（热重载 / 浏览器打开）
    Rectangle {
        id: chrome
        z: 20
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: 48
        color: "#0F172A"
        visible: !loading.visible

        Row {
            anchors.fill: parent
            anchors.leftMargin: 8
            anchors.rightMargin: 8
            spacing: 8

            // 统一按钮样式
            component ToolBtn: Button {
                height: 34
                anchors.verticalCenter: parent.verticalCenter
                font.pixelSize: 12
                contentItem: Text {
                    text: parent.text
                    font: parent.font
                    color: "#E2E8F0"
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    color: parent.pressed ? "#334155" : "#1E293B"
                    radius: 8
                    border.color: "#334155"
                }
            }

            ToolBtn {
                width: 92
                text: "⟳ 热重载"
                onClicked: webview.reload()
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                width: parent.width - 200
                elide: Text.ElideMiddle
                text: webview.url
                font.pixelSize: 12
                color: "#7DD3FC"
            }
            ToolBtn {
                width: 108
                text: "↗ 浏览器打开"
                onClicked: Qt.openUrlExternally(webview.url)
            }
        }
    }

    WebView {
        id: webview
        anchors.top: chrome.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        url: root.homeUrl

        onLoadingChanged: (load) => {
            loading.visible = (load && load.loading === true)
        }
        onTitleChanged: if (webview.title) root.title = webview.title
    }

    // AR 扫描覆盖层（网页经桥 /ar/scan/start 触发显示）
    //  ⚠️ QtWebView 是原生视图，恒在 Qt 表面之上 → 显示扫描页时必须隐藏 WebView
    ScanPage {
        id: scanPage
        anchors.fill: parent
        onVisibleChanged: {
            webview.visible = !visible
            chrome.visible = !visible
        }
    }
}
