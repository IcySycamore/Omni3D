# CONTEXT —— Omni3D 领域语言与分层约定

> 本文件是项目的**领域词汇表 + 架构约束**，供人与 AI 共用。
> 新增代码、评审 PR、写文档时，请使用这里的词汇，并遵守这里的约束。

---

## 1. 一句话定位

Omni3D 从**手机视频 / 图片**重建**真实尺度 3D 点云**：网页采集 → 服务器稠密重建 → 查看 / 测量 / 下载。

---

## 2. 核心分层（不可逆的依赖方向）

```
client（web / desktop）  ──HTTP──►  server（唯一一种）  ──►  model（Fast3R）
        ▲                                  ▲
  移动端 App 壳 = 复用 web client + 本地桥 :50687
                                            └─ 控制面（官网 portal）：账号 / 计费 / 发 Key
```

**角色构成（务必分清）：**

| 角色                   | 是什么                                                     | 位置                        |
| ---------------------- | ---------------------------------------------------------- | --------------------------- |
| **web client**         | **唯一的客户端实现**（在浏览器里跑）                       | `web/index.html`            |
| **面板（panel）**      | 静态页面宿主，只交付页面（无模型依赖）                     | `web/pages.py`              |
| **服务商（provider）** | 重建 API（数据面）：任务编排 / 会话 / 点云 / 测量          | `web/server.py`             |
| **官网（portal）**     | 账号 / 计费 / API Key / 流水（控制面），即“官方服务商”本身 | `web/portal.py`             |
| **移动端 App**         | 不是第二种 client：web client 的 WebView 壳 + 本地桥       | `qt_app/`                   |
| ~~桌面 client~~        | 已归档：PyQt5 + VTK 的旧实现，不再维护                     | `archive/desktop-pyqt-vtk/` |

**硬约束：**

1. **server 只有一种。** 所有重建能力只在服务器实现一次。
2. **只允许有一个客户端实现。** 任何重建 / 尺度 / 历史 / 测量逻辑都不允许在 client 里重复实现；
   client 只负责「采集」与「呈现」。**不要为桌面再起一套客户端**——浏览器就是桌面端。
3. **移动端 App 不实现独立 UI**（它就是 web client 的壳）。新增功能优先做在网页侧，
   不要在 App 里另起一套界面。
4. **model 层（`fast3r/`）是 vendored 第三方代码**，只消费其推理入口，不在其中做业务改动。
5. **控制面与数据面分离**：账号 / 计费 / 发 Key 只在**官网**（`web/portal.py`）；重建与会话只在
   **服务商**（`web/server.py`）。两者**共享同一套认证实现**（`web/auth_api.py`）与同一份 SQLite，
   但职责不互串：服务商不自己管账号，官网不自己跑重建。

---

## 3. 领域词汇表

| 术语                      | 定义                                                                      | 实现位置                                           |
| ------------------------- | ------------------------------------------------------------------------- | -------------------------------------------------- | --- | ------------ | ------------------------------------------------------------------ | ---------------------------------------- |
| **采集**                  | 获取输入图像。四种方式（录制 / 连拍 / 本地文件 / AR 扫描）**互斥**        | `web/index.html`、`qt_app`                         |
| **提交重建**              | 把采集包以 multipart 契约提交给服务器                                     | `POST /api/tasks`（异步）或 `/reconstruct`（同步） |
| **任务 / 会话**           | 一次重建请求。`task_id` 同时也是会话层里的 `session_id`                   | `web/task_queue.py`、`web/session_store.py`        |
| **稠密点云**              | Fast3R 输出的逐像素 3D 点（`pts3d_in_other_view`）                        | `app/core/pipeline.py`                             |
| **模型坐标系**            | 重建得到的点云所在坐标系，**任意单位**                                    | —                                                  |
| **真实尺度**              | 把模型坐标换算为**米**的因子 `scale`                                      | `app/core/scale.py`                                |
| **尺度反推**              | 用户点选两点 + 输入已知真实距离 → 反推 `scale`                            | `POST /api/tasks/{id}/scale`                       |     | **米制对齐** | 用外部 AR 位姿把点云从模型坐标系换算到米制世界系：`p' = s·R·p + T` | `app/core/pipeline.py: metric_alignment` |
| **metric 块**             | 结果里的 `result.metric`：`{aligned, scale, n_views, source, intrinsics}` | `app/core/pipeline.py`                             |     | **标尺校准** | 尺度反推的产品化说法（无 AR 时的手动路径）                         | 前端交互 + 上述 API                      |
| **AR 扫描**               | App 内用华为 AREngine 采集，帧携带**米制 VIO 位姿**（前置提供 scale）     | `qt_app/src/ar_scan_controller.*`                  |
| **稀疏点云融合**          | 华为 SLAM 稀疏点云与服务器稠密点云同帧叠加                                | `qt_app/src/ar_scan_controller.*`                  |
| **桥（bridge）**          | App 本地 HTTP 服务 `:50687`，让网页访问原生能力（AR / 文件 / 本地历史）   | `qt_app/src/ar_bridge_server.*`                    |
| **会话层**                | 服务器端统一的重建历史持久化（SQLite，按 owner 隔离）                     | `web/session_store.py`                             |
| **client_id**             | 未登录时的匿名归属键；由 client **首次访问生成并持久化**                  | 网页：`localStorage`；桌面：固定 `desktop`         |
| **匿名认领（claim）**     | 登录后把本机匿名历史改挂到账号名下；**需用户手动确认**                    | `POST /api/auth/claim` + `/claim/preview`          |
| **桌面客户端**（已归档）  | 旧的 PyQt5 + VTK 客户端；已废弃，仅供查阅                                 | `archive/desktop-pyqt-vtk/`                        |
| **账号**                  | 用户名 + salt + verifier；明文密码不落库                                  | `web/auth_store.py`                                |
| **用户名规则**            | 3–32 字符、仅 `[A-Za-z0-9_.-]`；**服务端强制**                            | `web/auth_store.validate_username`                 |
| **密码规则**              | ≥ 8 位且非全空白；**只能在客户端校验**（协议不上行明文密码）              | `web/index.html`                                   |
| **verifier**              | `sha256(salt + password)`，注册时由**客户端**计算上行                     | `web/auth_store.py` / `web/index.html`             |
| **nonce / proof**         | 登录挑战-应答：`proof = sha256(nonce + verifier)`，nonce 一次性           | 同上                                               |
| **token**                 | 登录成功后签发的会话令牌，请求经 `X-Auth-Token` 头携带                    | `web/session_manager.py`                           |
| **滑动过期**              | 令牌 **30 分钟**闲置才失效；每次带 token 的请求都续期                     | `web/session_manager.py`（`OMNI3D_SESSION_TTL`）   |
| **服务商（provider）**    | 「一个地址 + 可选端口 + 一把凭据」——面板里可配多台，**账号各家独立**      | 面板「设置 → 服务」                                |
| **凭据**                  | 一台服务商的 `{token, username, apiKey}`；存 `omni3d.cred:<serverId>`     | `web/index.html`                                   |
| **API Key**               | 官网签发的长期凭据（`omni3d_…`），`X-Api-Key` 头携带；库里只存 sha256     | `web/api_keys.py`、`web/portal_store.py`           |
| **计量（metric）**        | 计费口径：`points`（点云）/ `voxels`（体素）/ `mesh`（三角面）            | `web/portal_store.py: METRICS`                     |
| **计划包（plan pack）**   | 包月调用额度（Personal 100 次 / Professional 600 次），**30 天重置**      | `web/portal_store.py: PLANS`                       |
| **用量包（usage pack）**  | 预付折扣包（100 万单位、不过期），**各包独立计算**                        | `web/portal_store.py: PACKS`                       |
| **余额（balance）**       | 按量计费的坑位（分）；充值档 $5/10/20/50                                  | `accounts.balance_cents`                           |
| **扣减顺序**              | 一次重建：**计划包 → 同计量用量包 → 余额**（唯一入口 `charge()`）         | `web/portal_store.py: charge`                      |
| **流水（ledger）**        | 一切金额变动（充值 / 购买 / 用量扣减 / 计划重置），带余额快照             | `web/portal_store.py: ledger`                      |
| **验证阶段（BETA_FREE）** | 所有额度免费发放的开关：**包照扣、余额不扣、额度不足也不拦**              | `web/portal_store.py: BETA_FREE`                   |

---

## 4. 三个端口（唯一接缝）

| 端口    | 归属               | 说明                                                             |
| ------- | ------------------ | ---------------------------------------------------------------- |
| `50865` | **服务商 API**     | 重建（`web/server.py`）；`SERVE_PAGE=1` 时顺便也托管页面（默认） |
| `50866` | **面板页面**       | 只托管页面的轻服务（`web/pages.py`，无 torch）                   |
| `50867` | **官网（portal）** | 账号 / 计费 / API Key（`web/portal.py`）                         |
| `50687` | **Qt App 桥**      | 本地 HTTP 桥，仅 App 场景存在                                    |

端口与主机名常量只在 `web/hosting.py` 里定义一次（`PORT` / `PAGES_PORT` / `PORTAL_PORT` / `HOST`），
页面首次打开读 `GET /app-config.json` 拿引导信息（默认去找哪个 API、官网在哪个端口）。

---

## 5. 数据契约

> 完整接口清单（含请求/响应示例、状态码、curl 示例）见 **[`docs/API.md`](docs/API.md)**。

- **上传（multipart）**：`files`（图片/视频）、`resolution`、`intrinsics`、`extrinsics`、
  `is_video`、`frame_count`、`client_id`
- **内外参格式**：`extrinsics` = col-major 4×4；`intrinsics` = 9 元素 K（`fx 0 cx / 0 fy cy / 0 0 1`）
- **结果**：`points`（渲染子集，`[x,y,z,r,g,b]`，上限 `OMNI3D_MAX_RENDER_POINTS` 默认 6 万）
  - `num_points`（全量，已按置信度过滤 + SOR 剔除）+ `elapsed_s` + `scale`；完整点云**不进 JSON**，
    用 `GET /api/history/{id}/ply` 下载（二进制小端 PLY，含真实 RGB）
  - 另有 `num_points_raw`（SOR 前）/ `num_points_removed`（被剔数）
  - 离群点剔除：`OMNI3D_SOR_K` / `OMNI3D_SOR_STD`（默认 8 / 2.0，**任一为 0 即关闭**）；
    剔除后**显示 / PLY / 测量”用同一份点云**（看到的 = 量到的 = 导出的）
  - 点云来源：`OMNI3D_PTS3D_SOURCE` = `local`（默认，跟随上游评测的 local head）
    或 `global`（全局 head 原始输出）；两档共用同一全局坐标系，切换零成本
- **认证头**：登录后请求携带 `X-Auth-Token: <token>`；历史归属该 token 对应的 username
- **认证接口**：`/api/auth/{salt,register,challenge,login,logout,me,claim}`
  （+ `GET /api/auth/claim/preview` 预览可并入条数）
- **未登录**：以 `client_id` 作为匿名归属（保证网页端无登录也能用）
- **历史数据源**：客户端的「历史记录」一律读 **`/api/history`**（会话层，持久 + 按归属隔离）；
  `/api/tasks` 只是**内存任务表**（重启即清，仅用于运行中进度），不要拿它当历史
- **客户端防泄漏**：只有 `token` 进 `localStorage`（网页），**密码/verifier 永不持久化**

---

## 6. 真实尺度：两条路径

| 路径                  | 何时                           | 机制                                                     | 结果                                         |
| --------------------- | ------------------------------ | -------------------------------------------------------- | -------------------------------------------- |
| **AR 位姿驱动**（主） | App 内 AR 扫描（帧带米制位姿） | 服务器用预测相机轨迹与 AR 米制轨迹做相似配准，再变换点云 | 点云直接是**米制**，`scale` 随结果返回并入库 |
| **标尺校准**（兜底）  | 纯网页 / 无 AR 位姿            | 用户点两点 + 输入真实距离 → `POST /api/tasks/{id}/scale` | 事后反推并持久化 `scale`                     |

- 实现：`app/core/pipeline.py` 的 `metric_alignment()` + `apply_similarity()`。
- **外参约定**：每帧 16 个 float，**列主序** 4×4 **cam2world**，平移为**米**
  （同 ARCore / 华为 AREngine 的 `ArPose_getMatrix`）。
- 护栏：帧数 < 2、轨迹几乎不动、尺度离谱（≤1e-9 或 ≥1e9）时**拒绝对齐**，
  回退为任意尺度（`result.scale = null`，客户端走标尺校准）。

⚠️ **仍未做**：`intrinsics` 目前只用于**校验与结果报告**（写入 `result.metric.intrinsics`，
逐视图 fx/fy/cx/cy），**未**用于约束模型焦距 —— 那需要走 DUSt3R 的全局优化
（`global_aligner`），代价较大，暂不做。

---

## 7. 目录约定

```
Omni3D/
├── web/          # server（FastAPI）+ web client（index.html）
├── archive/      # 已废弃实现（desktop-pyqt-vtk：旧的 PyQt5 + VTK 客户端）
├── qt_app/       # 移动端 App 壳（WebView 加载 web client）+ 本地桥
├── app/core/     # 共享核心（config / pipeline / scale）——被 server 使用
├── fast3r/       # vendored 模型仓库（model 层）
└── docs/         # 架构 / 审计文档（部署文档已延后至 release）
```

> Phase 2 计划（独立 PR）：物理重组为 `server/` + `client/{web,qt}`。
> 详见 `docs/ARCHITECTURE.md`。

---

## 8. 部署策略（v1.0.0-mvp 冻结线）

**本版为 Python 参考实现，冻结后不再演进**（后续开发换成 C# / .NET 重写，见
[`docs/HANDOVER.md`](docs/HANDOVER.md)）。因此这里只记录「怎么把它跑起来」：

- 本地开发：`.\run.ps1 server`（重建 API，:50865）/ `.\run.ps1 pages`（页面，:50866）/
  `.\run.ps1 portal`（官网，:50867）/ `.\run.ps1 test`（全量测试）。
  三个进程可同机可异机，共享 `data/` 下的 SQLite。
- 依赖：`requirements-app.txt`（应用依赖，**不要**装根 `requirements.txt`——那是研究栈）；
  权重放本地后用 `OMNI3D_CHECKPOINT_DIR` 指向它（默认读仓库内的 `jedyang97/`，已 ignore）。
- 容器与部署资产（`Dockerfile` / `docker-compose.yml` / `docs/DEPLOYMENT.md`）已随本版恢复，
  按「角色」起容器（`ROLE=api|pages|portal`）。
- 客户端已收敛为**单一 web 实现**；旧的 PyQt5 + VTK 桌面客户端归档在
  `archive/desktop-pyqt-vtk/`（不再维护）。
