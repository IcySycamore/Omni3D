import os
import shutil
import threading
import time
import torch
import vtk
from PyQt5 import QtCore, QtWidgets, QtGui
from PyQt5.QtCore import pyqtSlot, Qt, QDir,QUrl,QThread, pyqtSignal
from PyQt5.QtWidgets import QApplication,QWidget,QMainWindow,QFileDialog,QGridLayout,QVBoxLayout,QLabel,QScrollArea,QPushButton
from PyQt5.QtGui import QColor,QImage,QPixmap
from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent

from MainWindow import Ui_MainWindow
from images_upload_display import display_images

# from fast3r.utils.checkpoint_utils import load_model
from fast3r.models.fast3r import Fast3R
from fast3r.dust3r.utils.image import load_images
from fast3r.dust3r.inference_multiview import inference
from fast3r.viz.video_utils import extract_frames_from_video
from process import start_visualization,align_local_pts3d_to_global

class window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.m=Ui_MainWindow()
        self.m.setupUi(self)
        self.setWindowFlags(Qt.FramelessWindowHint)
        # 用于存储图片路径
        self.image_paths = []
        self.image_resolution=512
        self.model = Fast3R.from_pretrained(checkpoint_dir)

        # 创建一个可以滚动的区域
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        # 创建内部的 widget 来放置图片
        gallery_widget = QWidget()
        self.gallery_layout = QGridLayout(gallery_widget)
        # 将 gallery widget 添加到 scroll area
        scroll_area.setWidget(gallery_widget)
        # 初始化布局
        main_layout = QVBoxLayout()
        main_layout.addWidget(scroll_area)
        self.m.widget_2.setLayout(main_layout)

        # 创建媒体播放器
        self.media_player = QMediaPlayer(None, QMediaPlayer.VideoSurface)
        self.media_player.setVideoOutput(self.m.widget)




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
        self.image_resolution=self.m.radioButton.text()

    @pyqtSlot()
    def on_radioButton_2_clicked(self):
        self.image_resolution = self.m.radioButton_2.text()

    @pyqtSlot()
    def on_pushButton_3_clicked(self):
        self.process_images_wrapper()


    def upload_images(self):
        # 打开文件对话框，允许用户选择多个文件
        options = QFileDialog.Options()
        files, _ = QFileDialog.getOpenFileNames(self, "Select Images", "", "Image Files (*.png *.jpg *.bmp)", options=options)
        if files:
            # 清除当前显示的图片
            self.image_paths = files
            display_images(self.m,self.image_paths)

    def upload_video(self):
        # 打开文件对话框以选择视频文件
        video_file, _ = QFileDialog.getOpenFileName(self, "Open Video File", "",
                                                    "Video Files (*.mp4 *.avi *.mov *.mkv)")
        if video_file:
            temp_dir = os.path.join("temp_preview_frames", "preview")
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            os.makedirs(temp_dir, exist_ok=True)
            frame_paths=extract_frames_from_video(video_file, temp_dir)
            self.image_paths = frame_paths

            # 使用选择的文件创建媒体内容
            video_url = QUrl.fromLocalFile(video_file)
            media_content = QMediaContent(video_url)
            # 设置播放器的内容
            self.media_player.setMedia(media_content)
            # 播放视频
            # 启动线程
            playthread = threading.Thread(target=self.media_player.play())
            playthread.start()

    def process_images_wrapper(self):
        image_size = int(self.image_resolution)
        self.model = self.model.to(device)
        self.model.eval()

        self.m.label_3.setText(f"Processing {len(self.image_paths)} images...\nLoading and cropping images...")
        start_load_time = time.time()
        images = load_images(self.image_paths, size=image_size, verbose=True)
        end_load_time = time.time()
        load_time = end_load_time - start_load_time
        self.m.label_3.setText(f"Processing {len(self.image_paths)} images...\nImage loading and cropping time: {load_time:.2f} sec.\nRunning model inference...")

        output_dict, profiling_info = inference(
            images,
            self.model,
            device,
            dtype=torch.float32,  # or use torch.bfloat16 if supported
            verbose=True,
            profiling=True,
        )

        model_forward_time = profiling_info['total_time']
        self.m.label_3.setText(f"Processing {len(self.image_paths)} images...\nImage loading and cropping time: {load_time:.2f} sec.\nModel inference time: {model_forward_time:.2f} sec.\nPreparing visualization...")

        align_local_pts3d_to_global(
            preds=output_dict['preds'],
            views=output_dict['views'],
            min_conf_thr_percentile=85)

        start_visualization(self.m,output_dict, min_conf_thr_percentile=10, global_conf_thr_value_to_drop_view=1.5,point_size=0.0004)


        for view_idx, pred in enumerate(output_dict['preds']):
            point_cloud = pred['pts3d_in_other_view'].cpu().numpy()
            print(f"Point Cloud Shape for view {view_idx}: {point_cloud.shape}")  # shape: (1, 368, 512, 3), i.e., (1, Height, Width, XYZ)



if __name__=="__main__":
    checkpoint_dir = "jedyang97/Fast3R_ViT_Large_512"
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    is_lightning_checkpoint=False
    output_dir="./demo_outputs"


    app=QApplication([])
    wid=window()
    wid.show()
    app.exec_()
