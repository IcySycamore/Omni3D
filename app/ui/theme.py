"""精致现代主题（QSS 卡片化设计）。

设计语言：深蓝渐变背景 + 卡片分区 + 蓝青渐变主色 + 圆角，
接近现代 SaaS 仪表盘观感。
"""

MAIN_WINDOW_QSS = """
/* ============ 全局 ============ */
QMainWindow, QWidget {
    background-color: #0b1220;
    color: #e2e8f0;
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 13px;
}
QMainWindow {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #0b1220, stop:0.5 #0f172a, stop:1 #111827);
}

/* ============ 卡片 ============ */
QFrame#card {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #15203a, stop:1 #101828);
    border: 1px solid #243247;
    border-radius: 14px;
}

/* ============ 标签 ============ */
QLabel { background: transparent; color: #cbd5e1; }
QLabel#appTitle {
    color: #38bdf8;
    font-size: 24px;
    font-weight: 700;
    letter-spacing: 2px;
}
QLabel#subtitle { color: #7c8ba1; font-size: 12px; }
QLabel#sectionTitle {
    color: #94a3b8;
    font-size: 13px;
    font-weight: 600;
}
QLabel#statusText {
    color: #e2e8f0;
    font-size: 12px;
    background: #0d1526;
    border: 1px solid #243247;
    border-radius: 8px;
    padding: 8px 12px;
}
QLabel#hint { color: #64748b; font-size: 11px; }

/* ============ 主按钮（渐变） ============ */
QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #3b82f6, stop:1 #06b6d4);
    border: none;
    border-radius: 10px;
    padding: 9px 18px;
    color: #ffffff;
    font-weight: 600;
    font-size: 13px;
}
QPushButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #60a5fa, stop:1 #22d3ee);
}
QPushButton:pressed { background: #0284c7; }
QPushButton:disabled { background: #334155; color: #94a3b8; }

/* ============ 单选 ============ */
QRadioButton { color: #cbd5e1; spacing: 8px; padding: 2px 0; }
QRadioButton::indicator {
    width: 16px; height: 16px; border-radius: 8px;
    border: 2px solid #475569; background: #1e293b;
}
QRadioButton::indicator:hover { border-color: #38bdf8; }
QRadioButton::indicator:checked {
    border-color: #38bdf8;
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 #ffffff, stop:0.35 #ffffff, stop:0.4 #06b6d4, stop:1 #06b6d4);
}

/* ============ 滚动区 ============ */
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical {
    background: #334155; border-radius: 4px; min-height: 30px;
}
QScrollBar::handle:vertical:hover { background: #38bdf8; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }

/* ============ 视频预览 ============ */
QVideoWidget#videoView {
    background: #000000;
    border: 1px solid #243247;
    border-radius: 10px;
}

/* ============ 菜单 / 状态栏 ============ */
QMenuBar { background: #0b1220; color: #cbd5e1; }
QStatusBar { background: #0b1220; color: #7c8ba1; }
"""
