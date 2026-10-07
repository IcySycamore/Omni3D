# Omni3D 开发者手册

## 1. 适用范围

本手册面向项目开发者和运维成员，覆盖本地开发、测试、镜像发布、GPU 云端部署和日常排障。用户侧操作请参阅 [用户使用手册](USER_GUIDE.md)。

本仓的 Python 实现是当前可运行的参考实现。修改前还应阅读：

- [贡献指南](../CONTRIBUTING.md)：分支、提交与 PR 规则；
- [架构说明](ARCHITECTURE.md)：模块职责、数据流和边界；
- [API 参考](API.md)：对外接口与认证契约；
- [部署指南](DEPLOYMENT.md)：Docker Compose 和环境变量说明。

## 2. 项目结构

| 路径 | 职责 |
| --- | --- |
| `panel/` | FastAPI 服务、网页客户端、认证、任务队列与门户页面。 |
| `panel/server.py` | 重建 API 服务入口，负责接收任务并调用核心管线。 |
| `panel/pages.py` | 面板页面服务入口。 |
| `panel/portal.py` | 账号、额度和 API Key 管理门户入口。 |
| `server/core/` | 重建管线、几何处理、尺度和运行配置。 |
| `fast3r/` | 固定版本的模型代码；除非单独论证，不直接修改。 |
| `app/` | 移动端 WebView 与 AR 扫描外壳。 |
| `tests/` | 单元测试和集成测试。 |
| `docs/` | 架构、接口、部署、用户与开发文档。 |
| `data/` | 运行期 SQLite、任务记录和产物；不提交到 Git。 |
| `models/` | 模型权重挂载目录；不提交到 Git。 |

## 3. 三服务架构

| 服务 | 默认端口 | 是否需要 GPU | 主要职责 |
| --- | ---: | :---: | --- |
| `api` | 50865 | 是 | Fast3R 推理、任务、历史、测量和 API Key 校验。 |
| `pages` | 50866 | 否 | 提供网页客户端。 |
| `portal` | 50867 | 否 | 管理账号、额度和 API Key。 |

三个服务必须共享同一份 `data/` 目录，否则 portal 签发的 API Key 无法被 api 校验。生产环境中，三个服务应由反向代理统一对外暴露；不要直接将内部容器端口暴露到公网。

## 4. 本地开发与测试

### 4.1 安装应用依赖

建议使用 Python 3.10～3.12 和带 CUDA 的 PyTorch 环境：

```powershell
<你的环境>\python.exe -m pip install -r requirements-app.txt
```

模型权重不进入仓库。将 Fast3R 权重存放在本地目录，并设置：

```powershell
$env:OMNI3D_CHECKPOINT_DIR = 'D:\models\Fast3R_ViT_Large_512'
```

### 4.2 启动与验证

```powershell
.\run.ps1 server
.\run.ps1 pages
.\run.ps1 portal
.\run.ps1 test
```

API 健康检查地址为 `http://127.0.0.1:50865/health`。真实模型首次加载期间，健康检查可能返回 `ready: false`；等待模型加载完成后再提交重建。

提交 PR 前至少执行与改动相关的测试；变更接口、架构或部署方式时，必须同步更新相应文档。

## 5. 代码与文档协作规范

1. 从最新 `main` 创建分支，例如 `docs/user-and-developer-guides`；
2. 一个提交只解决一类问题，文档提交建议使用 `docs:` 前缀；
3. 不提交模型权重、`data/`、生成结果、私钥、密码、API Key、真实公网 IP 或个人镜像仓库凭据；
4. 修改 API 时更新 `docs/API.md`；修改模块职责或数据流时更新 `docs/ARCHITECTURE.md`；
5. 通过 Pull Request 审核后再合并到 `main`。

## 6. 云端发布流程

实际运行环境采用“Git 仓库 → 容器镜像仓库 → GPU 云主机”的发布链路：

```text
本地开发与测试
  → Push 分支并创建 PR
  → 合并到 main
  → 镜像仓库从 main 构建新镜像
  → GPU ECS 拉取镜像
  → Docker Compose 重建服务
  → /health 与真实重建验收
```

### 6.1 哪些改动需要重新构建镜像

| 改动类型 | 是否需要镜像仓库重构建 | 是否需要重启服务 |
| --- | :---: | :---: |
| `README.md`、`docs/*.md` | 否 | 否 |
| Python、HTML、Dockerfile、依赖文件 | 是 | 是 |
| 云主机上的 Nginx、环境变量、Compose 覆盖配置 | 否 | 通常需要重载 Nginx 或重建对应服务 |

仅修改 Markdown 文档时，提交并推送到 Git 即可；不需要镜像构建，也不会影响在线服务。

### 6.2 ECS 更新镜像的通用步骤

确认没有正在执行的重建任务后，在云主机项目目录执行。以下镜像地址仅为占位符，实际地址从镜像仓库控制台复制，切勿写入公共文档：

```bash
cd ~/apps/Omni3D
docker pull <registry>/<namespace>/omni3d:<tag>
docker tag <registry>/<namespace>/omni3d:<tag> omni3d:latest
docker compose -f docker-compose.yml -f docker-compose.ecs.yml up -d --force-recreate --no-build
```

随后检查：

```bash
docker compose -f docker-compose.yml -f docker-compose.ecs.yml ps
curl http://127.0.0.1:15086/health
docker compose -f docker-compose.yml -f docker-compose.ecs.yml logs --tail=100 api
```

`ready: true` 且 `device: cuda` 表示模型已在 GPU 上加载完成。发布完成后，至少使用一段测试素材进行一次真实重建验收。

建议为每次可发布镜像使用明确的版本标签，而非长期只依赖 `latest`；这样可在异常时快速回退到上一稳定版本。

## 7. 当前云端运维约定

### 网络与安全

- 仅反向代理的 HTTP/HTTPS 入口对普通用户开放；
- SSH 端口仅允许管理员当前公网 IP；
- 账号/API Key 管理端口仅对管理员开放，正式发布后应迁移到独立域名和 HTTPS；
- api、pages、portal 的容器端口应绑定到回环地址，由 Nginx 转发，避免直接暴露；
- 不要在 Issue、截图、日志或文档中暴露 API Key、私钥和密码。

### 停止与恢复

停止 GPU 实例前，先确认队列中没有重建任务；停止会中断运行中的推理。模型权重、Docker 镜像和 `data/` 会保留在云盘中。若使用弹性公网 IP，实例重启后仍可使用同一公网地址。

启动实例后，等待 Docker 服务及模型加载完成，再检查 `/health`；`ready: true` 后才允许用户提交任务。

### 数据备份

至少定期备份：

```text
~/apps/Omni3D/data/
```

其中包含账号、API Key 的哈希记录、任务历史和相关产物。备份文件应放在受控位置，不应提交至 Git。

## 8. 常用排障入口

| 现象 | 优先检查 |
| --- | --- |
| 页面无法访问 | ECS 是否运行、安全组 HTTP/HTTPS 规则、Nginx 状态。 |
| 健康检查未就绪 | `api` 容器日志、模型目录、GPU 挂载、显存。 |
| 浏览器认证失败 | 服务地址、API Key、api 与 portal 是否共享 `data/`。 |
| 任务失败或卡住 | `api` 日志、任务队列、输入素材、GPU 显存和磁盘空间。 |
| 更新代码后界面未变化 | 是否已构建并拉取新镜像、容器是否已重建、浏览器是否使用缓存。 |

常用命令：

```bash
cd ~/apps/Omni3D
docker compose -f docker-compose.yml -f docker-compose.ecs.yml ps
docker compose -f docker-compose.yml -f docker-compose.ecs.yml logs -f api
curl http://127.0.0.1:15086/health
sudo systemctl reload nginx
```

## 9. 正式公开前检查清单

- [ ] 域名已解析，并已启用可信 HTTPS 证书；
- [ ] API Key 发放、吊销与最小权限流程已明确；
- [ ] 管理端口未向公网无差别开放；
- [ ] `data/` 已有定期备份与恢复验证；
- [ ] 已设置费用预算、异常告警和实例启停责任人；
- [ ] 已完成并记录至少一次真实素材的端到端验收；
- [ ] 如在中国大陆面向公众长期提供网站服务，已确认备案及相关合规要求。
