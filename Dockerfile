# Omni3D Web 服务容器镜像
#
# 支持两种运行模式：
#   - MOCK_MODE=true  （默认）不加载真实模型，用于快速部署、前端调试
#   - MOCK_MODE=false 加载 Fast3R 真实模型，需要 CUDA/GPU 与模型权重
#
# 用法：
#   docker build -t omni3d:latest .
#   docker run -p 50865:50865 -e MOCK_MODE=true omni3d:latest

FROM pytorch/pytorch:2.4.0-cuda12.4-cudnn9-devel

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    git wget \
    libgl1-mesa-glx libglib2.0-0 libsm6 libxrender1 libxext6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先拷贝依赖清单，利用 Docker 缓存层
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Web 服务额外依赖
RUN pip install --no-cache-dir fastapi uvicorn

# 若需要真实模型推理，可启用 pytorch3d 安装（源码编译较慢）
# ARG INSTALL_PYTORCH3D=false
# RUN if [ "$INSTALL_PYTORCH3D" = "true" ]; then \
#         pip install --no-cache-dir "git+https://github.com/facebookresearch/pytorch3d.git@stable"; \
#     fi

# 拷贝项目源码
COPY . .

# 安装项目本身（setup.py 存在时）
RUN pip install --no-cache-dir -e . || true

ENV HOST=0.0.0.0
ENV PORT=50865
ENV MOCK_MODE=true

EXPOSE 50865

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
