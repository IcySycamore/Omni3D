# Omni3D 部署指南

本文档说明如何以标准化、服务化的方式部署 Omni3D Web 服务。

## 部署形态概览

| 方式 | 适用场景 | 依赖 | 复杂度 |
|------|----------|------|--------|
| **Docker Compose（推荐）** | 服务器、云主机、本地快速验证 | Docker + Docker Compose | 低 |
| **Docker 裸跑** | 需要自定义参数 | Docker | 低 |
| **本地源码运行** | 开发调试 | Python 3.10~3.12、CUDA（可选） | 中 |

## 快速开始（Docker Compose）

### 1. 前置要求

- 安装 [Docker](https://docs.docker.com/get-docker/)
- 安装 [Docker Compose](https://docs.docker.com/compose/install/)
-（真实模型推理可选）安装 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)

### 2. 启动 mock 服务

默认使用 mock 后端，无需 GPU 与模型权重即可启动：

```bash
cd Omni3D
docker-compose up --build -d
```

服务启动后访问：

```text
http://localhost:50865
```

### 3. 查看日志

```bash
docker-compose logs -f omni3d
```

### 4. 停止服务

```bash
docker-compose down
```

## 切换为真实模型推理

真实模式需要：

1. CUDA 显卡与 NVIDIA 驱动
2. 模型权重文件（`jedyang97/Fast3R_ViT_Large_512/` 目录下存在可加载的权重）
3. 安装 `pytorch3d`（构建较慢，已在 `Dockerfile` 中注释说明）

修改 `docker-compose.yml`：

```yaml
environment:
  - MOCK_MODE=false
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: all
          capabilities: [gpu]
```

并在 `Dockerfile` 中取消 `INSTALL_PYTORCH3D` 相关注释后重新构建：

```bash
docker-compose down
docker-compose up --build -d
```

## Docker 裸跑

```bash
# mock 模式
docker build -t omni3d:latest .
docker run -p 50865:50865 -e MOCK_MODE=true omni3d:latest

# 真实模式（需 GPU）
docker run --gpus all -p 50865:50865 -e MOCK_MODE=false omni3d:latest
```

## 本地源码运行

### 环境要求

- Python 3.10 ~ 3.12（推荐 3.12）
- PyTorch 2.3+（项目使用 `torch.nn.attention`）
- CUDA 11.8+（真实模型推理）

### 安装依赖

```bash
conda create -n omni3d python=3.12
conda activate omni3d
pip install -r requirements.txt
```

### 启动服务

```bash
# mock 模式（无 GPU 也可运行）
python web/mock_server.py

# 真实模式
python web/server.py
```

## 端口与配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `HOST` | `0.0.0.0` | 服务监听地址 |
| `PORT` | `50865` | 服务监听端口 |
| `MOCK_MODE` | `true` | `true` 启用 mock 后端，`false` 启用真实模型 |

## 常见问题

### Q1: 构建时提示找不到 `torch.nn.attention`

当前基础镜像 `pytorch/pytorch:2.4.0-cuda12.4-cudnn9-devel` 已包含 PyTorch 2.4，不会出现该问题。若在旧环境遇到，请升级 PyTorch 到 2.3+。

### Q2: 真实模型启动失败

检查以下几点：

- `jedyang97/Fast3R_ViT_Large_512/` 是否存在有效权重
- 容器是否正确映射 GPU（`--gpus all` 或 docker-compose 的 `deploy.resources.reservations.devices`）
- `pytorch3d` 是否安装成功

### Q3: 镜像体积过大

基础镜像包含 CUDA 开发套件，体积较大。如需缩小体积，可基于 `pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime` 构建运行时镜像。

## 文件说明

- `Dockerfile`：容器镜像构建定义
- `docker-compose.yml`：一键编排服务
- `scripts/docker-entrypoint.sh`：容器启动入口，根据 `MOCK_MODE` 选择服务
- `.dockerignore`：排除无需打包进镜像的文件
