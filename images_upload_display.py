from PyQt5 import QtCore
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QWidget,QGridLayout,QLabel,QScrollArea,QPushButton,QHBoxLayout,QVBoxLayout
from PyQt5.QtGui import QPixmap


def display_images(window,image_paths):
    # 清空所有现有的布局项
    layout = window.widget_2.layout()
    for i in reversed(range(layout.count())):
        layout.itemAt(i).widget().deleteLater()

    # 创建一个可以滚动的区域
    scroll_area = QScrollArea()
    scroll_area.setWidgetResizable(True)
    # 创建内部的 widget 来放置图片
    gallery_widget = QWidget()
    gallery_layout = QGridLayout(gallery_widget)
    # 将 gallery widget 添加到 scroll area
    scroll_area.setWidget(gallery_widget)
    layout.addWidget(scroll_area)
    window.widget_2.setLayout(layout)

    image_labels = []
    # 每行显示 6 张图
    row = 0
    col = 0
    for image_path in image_paths:
        pixmap = QPixmap(image_path)
        label = QLabel()
        label.setPixmap(pixmap.scaled(150, 150, Qt.KeepAspectRatio))  # 设置固定的高度为 150px
        label.mousePressEvent = lambda event, label=label, path=image_path, window=window, image_paths=image_paths: display_images_choosed(label, path,window,image_paths)
        image_labels.append(label)

        # 将图片和按钮添加到布局中
        gallery_layout.addWidget(label, row, col)

        col += 1
        if col >= 4:  # 每行 6 张图片
            col = 0
            row += 1  # 两行分别是图片和下载按钮


def display_images_choosed(label, path, window,image_paths):
    # 清空所有现有的布局项
    layout = window.widget_2.layout()
    for i in reversed(range(layout.count())):
        layout.itemAt(i).widget().deleteLater()

    max_widget = QWidget(window.widget_2)
    max_widget.setGeometry(QtCore.QRect(0, 0, 620, 300))
    max_widget.setFixedSize(620,300)
    max_layout = QHBoxLayout(max_widget)

    # 显示点击的小图作为大图
    pixmap = QPixmap(path).scaled(300, 300, Qt.KeepAspectRatio)
    large_image_label = QLabel(max_widget)
    large_image_label.setPixmap(pixmap)
    large_image_label.setGeometry(QtCore.QRect(0, 0, 300, 300))

    back_button = QPushButton(max_widget)
    back_button.setGeometry(QtCore.QRect(500, 0, 50, 30))
    back_button.setObjectName("X")
    back_button.setText("X")
    back_button.clicked.connect(lambda event,window=window,image_paths=image_paths: display_images(window,image_paths))

    layout.addWidget(max_widget)

    # 创建一个可以滚动的区域
    scroll_area = QScrollArea()
    scroll_area.setWidgetResizable(True)
    # 创建内部的 widget 来放置图片
    gallery_widget = QWidget()
    gallery_layout = QGridLayout(gallery_widget)
    # 将 gallery widget 添加到 scroll area
    scroll_area.setWidget(gallery_widget)
    layout.addWidget(scroll_area)

    col = 0
    for image_path in image_paths:
        pixmap = QPixmap(image_path)
        label = QLabel()
        label.setPixmap(pixmap.scaled(50, 50, Qt.KeepAspectRatio))
        label.setStyleSheet("border: none;")  # 重置所有图片的边框
        if image_path == path:
            label.setStyleSheet("border: 2px solid red;")  # 标记选中的图片
        label.mousePressEvent = lambda event, label=label, path=image_path,window=window,image_paths=image_paths: display_images_choosed(label, path, window,image_paths)
        gallery_layout.addWidget(label, 0, col)
        col += 1
