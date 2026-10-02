import QtQuick
import QtQuick.Controls
import Omni3D

// AR 扫描页（原生覆盖层，网页经桥触发显示）
Rectangle {
    id: root
    objectName: "scanPage"
    visible: false
    z: 200
    color: "black"

    // 预览（渲染线程创建 OES 纹理 → AREngine 开相机）
    ArScanPreview {
        id: preview
        anchors.fill: parent
    }

    // 顶部状态条
    Rectangle {
        id: statusBar
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: 56
        color: "#cc111111"

        Row {
            anchors.verticalCenter: parent.verticalCenter
            anchors.left: parent.left
            anchors.leftMargin: 16
            spacing: 12
            Rectangle {
                width: 12; height: 12; radius: 6
                anchors.verticalCenter: parent.verticalCenter
                color: Scan.tracking ? "#4caf50" : "#ff7043"
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                color: "white"
                font.pixelSize: 14
                text: Scan.tracking ? "AR 跟踪中" : "等待跟踪…"
            }
        }
        Text {
            anchors.verticalCenter: parent.verticalCenter
            anchors.right: parent.right
            anchors.rightMargin: 16
            color: "white"
            font.pixelSize: 14
            text: "已采集 " + Scan.frameCount + " 帧"
        }
    }

    // 底部控制：独立「拍摄 / 录制 / 完成 / 放弃」——进入扫描页只开预览，不自动采集
    Rectangle {
        id: controlBar
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        height: 150
        color: "#cc111111"

        Column {
            anchors.centerIn: parent
            spacing: 12
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                color: Scan.scanning ? "#4fc3f7" : "#cccccc"
                font.pixelSize: 13
                text: Scan.scanning
                      ? "● 录制中 · 缓慢绕行一周（移动时采集更佳）"
                      : "对准场景 · 「拍摄」单张 或「录制」连续采集"
            }
            Row {
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: 14
                Button {
                    text: "📷 拍摄"
                    onClicked: Scan.captureOne()
                }
                Button {
                    text: Scan.scanning ? "■ 停止" : "● 录制"
                    highlighted: Scan.scanning
                    onClicked: Scan.scanning ? Scan.stopScan() : Scan.startScan()
                }
                Button {
                    text: "完成"
                    highlighted: true
                    onClicked: {
                        Scan.finish()
                        root.visible = false
                    }
                }
                Button {
                    text: "放弃"
                    onClicked: {
                        Scan.reset()
                        Scan.stopScan()
                        root.visible = false
                    }
                }
            }
        }
    }

    // 看门狗：AREngine 会话不可用（服务被卸载等极端情况）→ 关闭，避免黑屏卡死
    Timer {
        id: watchdog
        interval: 8000
        repeat: false
        running: root.visible
        onTriggered: {
            if (!Scan.available) {
                Scan.stopScan()
                root.visible = false
            }
        }
    }

    onVisibleChanged: {
        if (visible) {
            syncSize()
        } else {
            Scan.stopScan()
        }
    }

    // 预览尺寸就绪后同步给 AREngine display geometry
    // ⚠️ 华为 AREngine setDisplayGeometry 需要物理像素（surface 像素），
    // 而 Qt Quick 的 width/height 是逻辑单位（dp）→ 必须乘 devicePixelRatio
    function syncSize() {
        if (preview.width > 0 && preview.height > 0)
            Scan.setPreviewSize(
                Math.round(preview.width * Screen.devicePixelRatio),
                Math.round(preview.height * Screen.devicePixelRatio)
            )
    }

    Component.onCompleted: {
        preview.widthChanged.connect(syncSize)
        preview.heightChanged.connect(syncSize)
        syncSize()
    }
}
