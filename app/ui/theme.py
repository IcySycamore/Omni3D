MAIN_WINDOW_QSS = """
/* ============ 全局 ============ */
QMainWindow, QWidget {
    background-color: #0a0e1a;
    color: #e8edf5;
    font-family: "Inter", "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 17px;   /* 15px → 17px */
}
QMainWindow {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #070b15, stop:0.4 #0d1428, stop:0.7 #111b35, stop:1 #0a1628);
}

/* ============ 卡片 ============ */
QFrame#card {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(30, 50, 90, 0.55), stop:1 rgba(16, 28, 55, 0.70));
    border: 1px solid rgba(56, 189, 248, 0.15);
    border-radius: 18px;
    padding: 0px;
}

/* ============ 标签 ============ */
QLabel { background: transparent; color: #d1d9e8; }
QLabel#appTitle {
    font-size: 34px;   /* 30px → 34px */
    font-weight: 800;
    letter-spacing: 1.5px;
    color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #38bdf8, stop:0.5 #818cf8, stop:1 #a78bfa);
}
QLabel#subtitle {
    color: #7a8aa8;
    font-size: 18px;   /* 16px → 18px */
    font-weight: 400;
    letter-spacing: 0.5px;
}
QLabel#sectionTitle {
    color: #94a3b8;
    font-size: 16px;   /* 14px → 16px */
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.8px;
}
QLabel#statusText {
    color: #e2e8f0;
    font-size: 16px;   /* 14px → 16px */
    background: rgba(13, 21, 38, 0.8);
    border: 1px solid rgba(56, 189, 248, 0.12);
    border-radius: 10px;
    padding: 12px 16px;
}
QLabel#hint {
    color: #5a6a88;
    font-size: 15px;   /* 13px → 15px */
    font-style: italic;
}

/* ============ 主按钮 ============ */
QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #3b82f6, stop:0.5 #6366f1, stop:1 #8b5cf6);
    border: none;
    border-radius: 12px;
    padding: 14px 30px;   /* 12px 26px → 14px 30px */
    color: #ffffff;
    font-weight: 600;
    font-size: 17px;   /* 15px → 17px */
    letter-spacing: 0.3px;
}
QPushButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #60a5fa, stop:0.5 #818cf8, stop:1 #a78bfa);
    padding: 12px 30px 16px 30px;
}
QPushButton:pressed {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #2563eb, stop:0.5 #4f46e5, stop:1 #7c3aed);
    padding: 14px 30px;
}
QPushButton:disabled {
    background: #283248;
    color: #5a6a88;
    padding: 14px 30px;
}

/* ============ 次要按钮 ============ */
QPushButton#secondary {
    background: transparent;
    border: 1.5px solid rgba(56, 189, 248, 0.3);
    border-radius: 12px;
    padding: 12px 26px;
    color: #94a3b8;
    font-weight: 500;
    font-size: 16px;   /* 14px → 16px */
}
QPushButton#secondary:hover {
    background: rgba(56, 189, 248, 0.08);
    border-color: #38bdf8;
    color: #e2e8f0;
}
QPushButton#secondary:pressed {
    background: rgba(56, 189, 248, 0.15);
}

/* ============ 单选 ============ */
QRadioButton {
    color: #cbd5e1;
    spacing: 12px;
    padding: 6px 0;
    font-weight: 450;
    font-size: 16px;   /* 14px → 16px */
}
QRadioButton::indicator {
    width: 22px;   /* 20px → 22px */
    height: 22px;  /* 20px → 22px */
    border-radius: 11px;
    border: 2px solid #475569;
    background: #1a2540;
}
QRadioButton::indicator:hover {
    border-color: #60a5fa;
    background: #1e2d4a;
}
QRadioButton::indicator:checked {
    border-color: #818cf8;
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.5,
        stop:0 #ffffff, stop:0.3 #ffffff, stop:0.35 #818cf8, stop:1 #6366f1);
}

/* ============ 滚动区 ============ */
QScrollArea {
    background: transparent;
    border: none;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;   /* 8px → 10px */
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #3b82f6, stop:1 #8b5cf6);
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #60a5fa, stop:1 #a78bfa);
}
QScrollBar::add-line, QScrollBar::sub-line {
    height: 0;
}

/* ============ 视频预览 ============ */
QVideoWidget#videoView {
    background: #050a14;
    border: 1px solid rgba(56, 189, 248, 0.10);
    border-radius: 14px;
}

/* ============ 菜单 / 状态栏 ============ */
QMenuBar {
    background: transparent;
    color: #94a3b8;
    font-size: 16px;   /* 14px → 16px */
}
QMenuBar::item:selected {
    background: rgba(56, 189, 248, 0.10);
    color: #e2e8f0;
}
QStatusBar {
    background: transparent;
    color: #5a6a88;
    font-size: 15px;   /* 13px → 15px */
}
"""