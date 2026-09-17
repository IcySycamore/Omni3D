# Omni3D 容器镜像（v1.0.0-mvp：面板 / 服务商 API / 官网 三端口）
#
# 同一个镜像三种角色（由 ROLE 决定跑哪个进程）：
#   ROLE=api     服务商 API（重建，:50865，需 GPU）
#   ROLE=pages   面板页面（纯静态，:50866，无 torch）
#   ROLE=portal  官网（账号 / 计费 / API Key，:50867）
#   另：MOCK_MODE=true 时 api 角色跑 mock 服务（无模型，前端调试用）
#
# 用法：
#   docker build -t omni3d:latest .
#   docker run -p 50865:50865 -e ROLE=api -v /models:/models -e OMNI3D_CHECKPOINT_DIR=/models/Fast3R_ViT_Large_512 omni3d:latest
#
# 注：本镜像是 **Python 参考实现**的部署形态，v1.0.0-mvp 之后将被 C# 版本取代
# （见 docs/HANDOVER.md）。

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

# 先拷贝依赖清单，利用 Docker 缓存层。
# 应用依赖用 requirements-app.txt（研究栈 requirements.txt 不进镜像）。
COPY requirements-app.txt ./requirements-app.txt
RUN pip install --no-cache-dir -r requirements-app.txt

# 拷贝项目源码
COPY . .

# pip install -e . 让 vendored 模型包可被 import（失败不致命：必要时靠 WORKDIR 直导入）
RUN pip install --no-cache-dir -e . || true

# 三端口的监听地址与端口（与 web/hosting.py 的变量名一致）
ENV HOST=0.0.0.0 \
    PORT=50865 \
    PAGES_HOST=0.0.0.0 \
    PAGES_PORT=50866 \
    PORTAL_HOST=0.0.0.0 \
    PORTAL_PORT=50867 \
    SERVE_PAGE=0 \
    ROLE=api \
    MOCK_MODE=false

EXPOSE 50865 50866 50867

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
