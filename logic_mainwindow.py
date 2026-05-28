import os
import shutil
import threading
import time
import torch
import vtk
import numpy as np
from PyQt5 import QtCore, QtWidgets, QtGui
from PyQt5.QtCore import pyqtSlot, Qt, QDir, QUrl, QThread, pyqtSignal
from PyQt5.QtWidgets import QApplication, QWidget, QMainWindow, QFileDialog, QGridLayout, QVBoxLayout, QLabel, QScrollArea, QPushButton
from PyQt5.QtGui import QColor, QImage, QPixmap
from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent

from MainWindow import Ui_MainWindow
from images_upload_display import display_images

from fast3r.models.fast3r import Fast3R
from fast3r.dust3r.utils.image import load_images
from fast3r.dust3r.inference_multiview import inference
from fast3r.viz.video_utils import extract_frames_from_video
from process import start_visualization, align_local_pts3d_to_global


class window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.m = Ui_MainWindow()
        self.m.setupUi(self)
        self.setWindowFlags(Qt.FramelessWindowHint)
        
        # 应用样式美化
        self.apply_styling()
        
        # 初始化变量
        self.image_paths = []
        self.image_resolution = 512
        self.model = None
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        
        # 创建可以滚动的区域
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        gallery_widget = QWidget()
        self.gallery_layout = QGridLayout(gallery_widget)
        scroll_area.setWidget(gallery_widget)
        
        # 初始化布局
        main_layout = QVBoxLayout()
        main_layout.addWidget(scroll_area)
        self.m.widget_2.setLayout(main_layout)

        # 创建媒体播放器
        self.media_player = QMediaPlayer(None, QMediaPlayer.VideoSurface)
        
        # 加载模型
        self.load_model()

    def apply_styling(self):
        """应用样式美化"""
        self.setStyleSheet("""
            QMainWindow {
                background-color: #2a2a4a;
            }
            QPushButton {
                background-color: #16213e;
                color: #e2e2e2;
                border: 1px solid #0f3460;
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #0f3460;
                border-color: #e94560;
            }
            QPushButton:pressed {
                background-color: #e94560;
            }
            QPushButton#pushButton_3 {
                background-color: #e94560;
                font-size: 16px;
                padding: 10px;
            }
            QPushButton#pushButton_3:hover {
                background-color: #ff6b6b;
            }
            QLabel {
                color: #e2e2e2;
                font-size: 14px;
            }
            QLabel#label {
                font-size: 28px;
                font-weight: bold;
                color: #e94560;
            }
            QLabel#label_2, QLabel#label_4 {
                font-size: 13px;
                font-weight: bold;
                color: #0f3460;
                background-color: #e2e2e2;
                border-radius: 4px;
                padding: 4px;
            }
            QRadioButton {
                color: #e2e2e2;
                font-size: 13px;
                spacing: 8px;
            }
            QRadioButton::indicator {
                width: 14px;
                height: 14px;
                border-radius: 7px;
            }
            QRadioButton::indicator:unchecked {
                background-color: #16213e;
                border: 1px solid #0f3460;
            }
            QRadioButton::indicator:checked {
                background-color: #e94560;
                border: 1px solid #e94560;
            }
            QScrollArea {
                border: 1px solid #0f3460;
                border-radius: 8px;
                background-color: #1a1a3a;
            }
            QLabel#label_3 {
                background-color: #1a1a3a;
                border: 1px solid #0f3460;
                border-radius: 8px;
                padding: 10px;
                font-family: monospace;
            }
        """)
        
        # 修改按钮文字
        self.m.pushButton.setText("📷 上传图片")
        self.m.pushButton_2.setText("🎬 上传视频")
        self.m.pushButton_3.setText("🚀 开始重建")
        self.m.radioButton.setText("512 (高精度)")
        self.m.radioButton_2.setText("224 (快速)")
        self.m.radioButton_3.setText("🔍 选点模式")
        self.m.radioButton_4.setText("📏 连线模式")

    def load_model(self):
        """加载 Fast3R 模型"""
        checkpoint_dir = "jedyang97/Fast3R_ViT_Large_512"
        self.m.label_3.setText("正在加载模型，请稍候...")
        try:
            self.model = Fast3R.from_pretrained(checkpoint_dir).to(self.device)
            self.model.eval()
            self.m.label_3.setText("模型加载完成，可以上传图片了")
            print("模型加载完成")
        except Exception as e:
            self.m.label_3.setText(f"模型加载失败: {str(e)[:50]}...")
            print(f"模型加载失败: {e}")

    @pyqtSlot()
    def on_toolButton_4_clicked(self):
        self.showMinimized()

    @pyqtSlot()
    def on_toolButton_3_clicked(self):
        self.close()

    @pyqtSlot()
    def on_pushButton_clicked(self):
        self.upload_images()

    @pyqtSlot()
    def on_pushButton_2_clicked(self):
        self.upload_video()

    @pyqtSlot()
    def on_radioButton_clicked(self):
        self.image_resolution = 512
        print(f"分辨率设置为: 512")

    @pyqtSlot()
    def on_radioButton_2_clicked(self):
        self.image_resolution = 224
        print(f"分辨率设置为: 224")

    @pyqtSlot()
    def on_pushButton_3_clicked(self):
        self.process_images_wrapper()

    def upload_images(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择图片", "", "图片文件 (*.png *.jpg *.jpeg *.bmp)")
        if files:
            self.image_paths = files
            display_images(self.m, self.image_paths)
            self.m.label_3.setText(f"已选择 {len(files)} 张图片")
            print(f"上传了 {len(files)} 张图片")

    def upload_video(self):
        video_file, _ = QFileDialog.getOpenFileName(self, "选择视频", "", "视频文件 (*.mp4 *.avi *.mov *.mkv)")
        if video_file:
            self.m.label_3.setText("正在提取视频帧...")
            QtWidgets.QApplication.processEvents()
            
            temp_dir = os.path.join("temp_preview_frames", "preview")
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            os.makedirs(temp_dir, exist_ok=True)
            
            frame_paths = extract_frames_from_video(video_file, temp_dir)
            self.image_paths = frame_paths
            display_images(self.m, self.image_paths)
            
            self.m.label_3.setText(f"已从视频提取 {len(self.image_paths)} 帧")
            print(f"提取了 {len(frame_paths)} 帧")
            
            # 视频预览（可选）
            video_url = QUrl.fromLocalFile(video_file)
            media_content = QMediaContent(video_url)
            self.media_player.setMedia(media_content)

    def process_images_wrapper(self):
        if not self.image_paths:
            self.m.label_3.setText("请先上传图片或视频")
            return
        if self.model is None:
            self.m.label_3.setText("模型还在加载中，请稍后再试")
            return
        
        image_size = self.image_resolution
        
        self.m.label_3.setText(f"正在处理 {len(self.image_paths)} 张图片...\n加载图片中...")
        QtWidgets.QApplication.processEvents()
        
        start_load_time = time.time()
        images = load_images(self.image_paths, size=image_size, verbose=True)
        end_load_time = time.time()
        load_time = end_load_time - start_load_time
        
        self.m.label_3.setText(f"正在处理 {len(self.image_paths)} 张图片...\n图片加载时间: {load_time:.2f} 秒\n模型推理中...")
        QtWidgets.QApplication.processEvents()
        
        output_dict, profiling_info = inference(
            images,
            self.model,
            self.device,
            dtype=torch.float32,
            verbose=True,
            profiling=True,
        )
        
        model_forward_time = profiling_info['total_time']
        
        self.m.label_3.setText(f"正在处理 {len(self.image_paths)} 张图片...\n图片加载: {load_time:.2f} 秒\n模型推理: {model_forward_time:.2f} 秒\n准备可视化...")
        QtWidgets.QApplication.processEvents()
        
        align_local_pts3d_to_global(
            preds=output_dict['preds'],
            views=output_dict['views'],
            min_conf_thr_percentile=85)
        
        start_visualization(self.m, output_dict, min_conf_thr_percentile=10, 
                           global_conf_thr_value_to_drop_view=1.5, point_size=0.0004)
        
        self.m.label_3.setText(f"处理完成！共 {len(self.image_paths)} 张图片，{model_forward_time:.2f} 秒")
        
        # 打印点云信息
        for view_idx, pred in enumerate(output_dict['preds']):
            point_cloud = pred['pts3d_in_other_view'].cpu().numpy()
            print(f"Point Cloud Shape for view {view_idx}: {point_cloud.shape}")


if __name__ == "__main__":
    app = QApplication([])
    wid = window()
    wid.show()
    app.exec_()