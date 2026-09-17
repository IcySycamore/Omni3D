# Omni3D 部署指南（v1.0.0-mvp）

> ⚠️ 本指南描述的是 **Python 参考实现**。本仓冻结后由 C# / .NET 重写取代，
> 交接说明（含必须复现的契约）见 [`HANDOVER.md`](HANDOVER.md)。

## 部署形态概览

Omni3D 是**三个可独立部署的服务**（同一镜像，按 `ROLE` 起不同进程）：

| 服务     | 端口  | 职责                         | 需要 GPU                      |
| -------- | ----- | ---------------------------- | ----------------------------- |
| `api`    | 50865 | 重建任务 / 会话历史 / 测量   | ✅（`MOCK_MODE=true` 时不要） |
| `pages`  | 50866 | 面板页面（纯静态）           | ✖                             |
| `portal` | 50867 | 账号 / 计费 / API Key / 流水 | ✖                             |

三个服务共享同一个 `data/` 目录（SQLite），所以**默认假设同机部署**；
分子机器部署时必须把控制面数据库拆出去（见 `HANDOVER.md` §4）。

| 方式                       | 适用场景                   | 依赖                           | 复杂度 |
| -------------------------- | -------------------------- | ------------------------------ | ------ |
| **Docker Compose（推荐）** | 服务器 / 云主机 / 快速验证 | Docker + Compose               | 低     |
| **Docker 裸跑**            | 需要自定义参数             | Docker                         | 低     |
| **本地源码运行**           | 开发调试                   | Python 3.10~3.12、CUDA（可选） | 中     |

## 快速开始（Docker Compose）

```bash
cd Omni3D
docker-compose up --build -d          # 默认：api(MOCK) + pages + portal
```

启动后：

```text
面板    http://localhost:50866        （用这个）
官网    http://localhost:50867
API     http://localhost:50865/health
```

只想跑单体（不需要官网）：`docker-compose up --build -d api pages`。

```bash
docker-compose logs -f api      # 看日志（或 pages / portal）
docker-compose down             # 停止
```

## 切换为真实模型推理

真实模式需要：

1. CUDA 显卡与 NVIDIA 驱动；
2. **权重目录**（约 2.5GB）—— 挂到 `/models` 并用 `OMNI3D_CHECKPOINT_DIR` 指向它：

   ```bash
   # 一次就好：把 HF 权重放到 ./models/Fast3R_ViT_Large_512/
   docker-compose run --rm api ls /models/Fast3R_ViT_Large_512
   ```

3. 安装 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)；

`docker-compose.yml` 里把 `api` 的 `MOCK_MODE` 改为 `false` 并启用 GPU 段：

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

模型首次加载需数分钟，期间 `/health` 返回 `ready=false`（重建接口 503）。

## Docker 裸跑

```bash
docker build -t omni3d:latest .

# 服务商 API（mock：无模型也能跑通前端）
docker run -p 50865:50865 -e ROLE=api -e MOCK_MODE=true omni3d:latest

# 服务商 API（真实模型：挂权重卷 + 开 GPU）
docker run --gpus all -p 50865:50865 -e ROLE=api \
  -v /path/to/models:/models -e OMNI3D_CHECKPOINT_DIR=/models/Fast3R_ViT_Large_512 \
  omni3d:latest

# 面板页面
docker run -p 50866:50866 -e ROLE=pages omni3d:latest

# 官网
docker run -p 50867:50867 -e ROLE=portal omni3d:latest
```

⚠️ **裸跑多个服务时要挂同一份 `data/`**（`-v $PWD/data:/app/data`），否则官网签发的
API Key 服务商这边认不出来。

## 本地源码运行

### 环境要求

- Python 3.10 ~ 3.12（推荐 3.12；本仓开发环境为 `D:\anaconda3\envs\Omni3D`）
- PyTorch 2.3+（项目使用 `torch.nn.attention`）
- CUDA 11.8+（真实模型推理；`MOCK_MODE=true` 时不需要）

### 安装依赖

```bash
pip install -r requirements-app.txt     # 应用依赖；不要装根 requirements.txt（研究栈）
```

### 启动服务

Windows（推荐）：

```powershell
.\run.ps1 server     # 50865 服务商 API
.\run.ps1 pages      # 50866 面板
.\run.ps1 portal     # 50867 官网
.\run.ps1 test       # 全量测试
```

跨平台：

```bash
python web/server.py     # 服务商 API（首次启动加载模型，数十秒）
python web/pages.py      # 面板页面（不含模型依赖）
python web/portal.py     # 官网
python web/mock_server.py  # 无模型的假服务端（前端调试用）
```

## 端口与配置

| 环境变量                      | 默认值                                  | 说明                                                         |
| ----------------------------- | --------------------------------------- | ------------------------------------------------------------ |
| `ROLE`                        | `api`                                   | 容器入口选进程：`api` \| `pages` \| `portal`                 |
| `HOST` / `PORT`               | `127.0.0.1` / `50865`                   | 服务商 API 监听地址与端口                                    |
| `PAGES_HOST` / `PAGES_PORT`   | 同 `HOST` / `50866`                     | 面板页面                                                     |
| `PORTAL_HOST` / `PORTAL_PORT` | 同 `HOST` / `50867`                     | 官网                                                         |
| `SERVE_PAGE`                  | `1`                                     | 服务商 API 是否顺便托管面板页面（纯 API 部署置 `0`）         |
| `API_ORIGIN`                  | 空                                      | 页面默认去哪个地址找 API（空 = 按“页面同主机 + API 端口”推） |
| `CORS_ORIGINS`                | `*`                                     | 允许的跨源来源（令牌走头而非 Cookie，故不用凭据）            |
| `MOCK_MODE`                   | `false`                                 | 容器入口：`api` 角色跑 mock 服务而非真模型                   |
| `OMNI3D_CHECKPOINT_DIR`       | 仓库内 `jedyang97/Fast3R_ViT_Large_512` | **权重目录**（容器/客户环境指到挂载卷）                      |
| `OMNI3D_SESSION_TTL`          | `1800`                                  | 令牌闲置过期秒数                                             |
| `OMNI3D_API_KEYS`             | 空                                      | 静态 API Key（`key=用户名,...`；正式环境用官网签发的 Key）   |
| `BETA_FREE`（代码常量）       | `True`                                  | 验证阶段：包照扣、余额不扣、额度不足不拦                     |

> 端口常量的**单一来源**是 `web/hosting.py`，别在别处再写一份默认值。

## 常见问题

### Q1: 构建时提示找不到 `torch.nn.attention`

当前基础镜像 `pytorch/pytorch:2.4.0-cuda12.4-cudnn9-devel` 已包含 PyTorch 2.4，不会出现该问题。若在旧环境遇到，请升级 PyTorch 到 2.3+。

### Q2: 真实模型启动失败

检查以下几点：

- `OMNI3D_CHECKPOINT_DIR` 指向的目录里是否有 `model.safetensors` 与 `config.json`（容器里默认是 `/models/Fast3R_ViT_Large_512`）
- 容器是否正确映射 GPU（`--gpus all` 或 compose 的 `deploy.resources.reservations.devices`）
- `/health` 里的 `error` 字段会带出加载异常

### Q3: 镜像体积过大

基础镜像包含 CUDA 开发套件，体积较大。可换成 `pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime`；
**权重（2.5GB）绝不能进镜像**，一律用挂载卷 + `OMNI3D_CHECKPOINT_DIR`。

### Q4: 面板能打开、但所有请求网络错误

跨源缺 `CORS_ORIGINS`（默认 `*` 已放开）。服务端日志干净、浏览器控制台报 CORS 时先查这个；
另外确认页面里的服务器地址（设置 → 服务）与实际 API 地址一致。

### Q5: 官网签发的 API Key 在服务商这边不被认

两者必须看到**同一份 `data/`**（`omni3d_portal.db`）。分机器部署时请先按 `HANDOVER.md` §4
把控制面拆出去（内部 API + mTLS），不要靠共享文件。

## 文件说明

- `Dockerfile`：镜像构建定义（`ROLE=api|pages|portal`）
- `docker-compose.yml`：三服务编排
- `scripts/docker-entrypoint.sh`：按 `ROLE` 选进程
- `.dockerignore`：排除无需打包进镜像的文件（含权重、数据、临时帧）
- `requirements-app.txt`：应用依赖（与根 `requirements.txt` 的研究栈分离）
