"""应用配置。

集中管理模型路径、计算设备、默认参数，避免散落在各文件中。
"""
import os

import torch

# 项目根目录（app/ 的上两级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Fast3R 预训练模型：HF 仓库名，或相对项目根目录的本地权重目录
CHECKPOINT_DIR = os.path.join(PROJECT_ROOT, "jedyang97", "Fast3R_ViT_Large_512")

# 计算设备：优先 CUDA
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# 默认图像分辨率（UI 可选 512 / 224）
DEFAULT_RESOLUTION = 512

# 推理参数
INFERENCE_DTYPE = torch.float32
ALIGN_CONF_PERCENTILE = 85       # 局部→全局对齐的置信度百分位
VIS_CONF_PERCENTILE = 10         # 可视化置信度百分位
VIS_GLOBAL_CONF_DROP = 1.5       # 丢弃低置信度视图的全局阈值
VIS_POINT_SIZE = 0.0004          # 点云点大小
