"""图片画廊组件。

独立 QWidget：显示图片缩略图网格，点击进入大图预览（红框标记当前选中），
可点 X 返回缩略图视图。由主窗口嵌入 widget_2 区域。
"""
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

THUMB_SIZE = 150
LARGE_SIZE = (620, 300)
THUMBNAIL_COLUMNS = 4


class ImageGallery(QWidget):
    """图片画廊：缩略图网格 + 大图预览。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.image_paths = []
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)

    def set_images(self, image_paths):
        """设置并显示图片列表。"""
        self.image_paths = list(image_paths)
        self._show_thumbnails()

    # ---------- 内部视图切换 ----------

    def _clear(self):
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _show_thumbnails(self):
        self._clear()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        grid_widget = QWidget()
        grid = QGridLayout(grid_widget)

        for idx, path in enumerate(self.image_paths):
            row, col = divmod(idx, THUMBNAIL_COLUMNS)
            label = QLabel()
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                label.setPixmap(pixmap.scaled(THUMB_SIZE, THUMB_SIZE, Qt.KeepAspectRatio))
            label.setAlignment(Qt.AlignCenter)
            label.mousePressEvent = lambda _event, p=path: self._show_large(p)
            grid.addWidget(label, row, col)

        scroll.setWidget(grid_widget)
        self._layout.addWidget(scroll)

    def _show_large(self, path):
        self._clear()

        # 大图 + 返回按钮
        header = QWidget()
        header_layout = QHBoxLayout(header)
        large = QLabel()
        pixmap = QPixmap(path)
        if not pixmap.isNull():
            large.setPixmap(pixmap.scaled(*LARGE_SIZE, Qt.KeepAspectRatio))
        large.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(large, 1)

        back = QPushButton("X")
        back.setFixedSize(50, 30)
        back.clicked.connect(self._show_thumbnails)
        header_layout.addWidget(back, 0, Qt.AlignTop)

        # 底部小缩略图（当前选中红框标记）
        strip = QScrollArea()
        strip.setWidgetResizable(True)
        strip_widget = QWidget()
        strip_layout = QHBoxLayout(strip_widget)
        for p in self.image_paths:
            label = QLabel()
            pixmap = QPixmap(p)
            if not pixmap.isNull():
                label.setPixmap(pixmap.scaled(50, 50, Qt.KeepAspectRatio))
            label.setStyleSheet(
                "border: 2px solid red;" if p == path else "border: none;"
            )
            label.mousePressEvent = lambda _event, pp=p: self._show_large(pp)
            strip_layout.addWidget(label)
        strip.setWidget(strip_widget)

        self._layout.addWidget(header)
        self._layout.addWidget(strip)
