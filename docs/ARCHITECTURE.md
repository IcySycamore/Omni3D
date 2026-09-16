# Omni3D 架构说明（server / client 分层）

> 配套领域词汇表见根目录 `CONTEXT.md`，**接口清单见 [`docs/API.md`](API.md)**。本文记录**实现层**的模块地图与改造计划。

---

## 1. 目标架构

```
┌─ client（只有一份实现）───────────────┐        ┌─ server（唯一） ──────────────┐
│  web client     web/index.html         │        │  web/server.py  :50865        │
│  ─────────────────────────────────────│◄──HTTP─►│  ├ auth_store（账号）         │
│  移动端 App = web client 的壳 + 桥      │        │  ├ session_manager（token）   │
│  （qt_app/，本地桥 :50687）             │        │  ├ task_queue（实时进度）     │
└────────────────────────────────────────┘        │  ├ session_store（历史 SQLite）│
                                                  │  └ app/core/pipeline（重建）  │
                                                  └──────────────┬────────────────┘
                                                                 │
                                                           fast3r（model）
```

**原则**：server 只有一种；**客户端只有一份实现**（`web/index.html`）；
移动端 App 是它的**原生壳**（不另做 UI，只补 AR / 文件等原生能力）。
重建 / 尺度 / 历史 / 测量逻辑只在 server 实现一次。

> 桌面端 = 浏览器直接打开同一个地址。旧的 PyQt5 + VTK 客户端已归档到
> `archive/desktop-pyqt-vtk/`（原因：两套客户端必须写两遍几何与交互，
> 而 Python 与 JS 之间无法字面共享代码；「一套 Qt 跨平台」因 VTK 无 Android 支持而被否决）。

---

## 2. 模块地图

### server

| 模块                     | 职责                                                            | 关键入口                                                                             |
| ------------------------ | --------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `web/server.py`          | FastAPI 路由与编排（**19 个接口，见 [`docs/API.md`](API.md)**） | `/reconstruct`、`/api/tasks`、`/api/history`、`/api/tasks/{id}/scale`、`/api/auth/*` |
| `web/task_queue.py`      | 单 worker 异步队列 + 内存实时进度缓存                           | `task_queue.submit()` / `.get()`                                                     |
| `web/auth_store.py`      | **用户表**：salt + verifier = `sha256(salt+pwd)`（明文不落库）  | `create_user()` / `get_verifier()`                                                   |
| `web/session_manager.py` | **会话管理**：token → username（内存 + TTL 12h）                | `create()` / `username_for()` / `drop()`                                             |
| `web/session_store.py`   | **会话层**：重建历史持久化（SQLite，按 `owner` 隔离）           | `save_session()` / `list_sessions()`                                                 |
| `app/core/pipeline.py`   | 纯函数重建管线（加载→推理→对齐→**米制尺度对齐**）               | `run_reconstruction()` / `metric_alignment()`                                        |
| `app/core/scale.py`      | **真实尺度反推**（纯函数，无重依赖）                            | `infer_scale_from_measurement()`                                                     |
| `app/core/config.py`     | 权重路径 / 设备 / 默认参数                                      | —                                                                                    |

### client

| 模块                                | 职责                                                            |
| ----------------------------------- | --------------------------------------------------------------- |
| `web/index.html`                    | **唯一的客户端实现**（采集 / 查看 / 测量 / 历史 / 账号 / 设置） |
| `qt_app/qml/WebShell.qml`           | WebView 壳，加载 web client（Android）                          |
| `archive/desktop-pyqt-vtk/`         | 已废弃的 PyQt5 + VTK 桌面客户端（不再维护，见其 README）        |
| `qt_app/src/ar_bridge_server.*`     | 本地 HTTP 桥 `:50687`（`/ar/*`、`/ar/scan/*`、`/ar/file/*`）    |
| `qt_app/src/ar_scan_controller.*`   | AR 扫描：抓帧 + 米制位姿 + 华为稀疏点云累积（Android）          |
| `qt_app/src/hw_ar_engine_session.*` | 华为 AREngine 适配（`ArSessionBackend` 子类，Android）          |

---

## 3. 两条重建路径（共用同一编排）

```
同步： POST /reconstruct ─┐
                          ├─► _reconstruct_to_result() ─► session_store.save_session()
异步： POST /api/tasks ───┘        （推理→降采样→PLY）              （历史落库）
        └─► task_queue ─► _task_processor ─► _task_processor_impl
```

> 两条路径都必须落会话层，保证「历史」是服务器唯一权威来源。

---

## 4. 会话层（`web/session_store.py`）

- **后端**：SQLite 单文件（`data/omni3d_sessions.db`），零外部依赖。
- **隔离**：所有读写都带 `owner`（登录 → `user:<username>`；匿名 → `anon:<client_id>`），各客户端只能看到自己的历史。
- **存储策略**：元数据 + 降采样点云（zlib 压缩 BLOB）入库；完整 PLY 落盘到 `data/sessions/{id}.ply`。
- **取代**：原先三套割裂机制 —— 服务器内存队列 / Qt 的 `omni3d_history.json` / web 无持久化。

### REST 接口

历史四个接口（`GET/DELETE /api/history*`）的完整定义见 **[`docs/API.md`](API.md)**。

> 登录后请求携带 `X-Auth-Token: <token>`；`client_id` 仅作为**未登录**时的匿名归属。

---

## 5. 认证与会话管理

挑战-应答，**明文密码不上网、不落库**：

```
注册：verifier = sha256(salt + password)      ← 客户端算，服务器只存 verifier
登录：nonce  ← 服务器下发（一次性，5 分钟过期）
      proof  = sha256(nonce + verifier)       ← 客户端算
      服务器比对 sha256(nonce + verifier_stored)
```

八个认证接口（`/api/auth/*`）的完整定义见 **[`docs/API.md`](API.md)**。

- **服务端**：`web/session_manager.py` 维护 `token → username`（权威映射）。
- **客户端**：`web/index.html` 的 `fetch` 拦截器统一携带 `X-Auth-Token` 并处理 401。
- **历史归属**：`_owner_of(token, client_id)` 优先取 token 的 username，使历史**对应到 username**。

### 5.1 网页 / 移动端如何接入

网页 client 由服务器**同源**托管，所以页面里没有「服务器地址」概念，也不需要在
客户端重复实现认证逻辑：

```
web/index.html
  ├─ 身份：localStorage(omni3d.client_id)  ← 匿名归属（首次访问生成）
  │         localStorage(omni3d.token)     ← 仅存令牌，**不存密码/verifier**
  ├─ 统一 fetch 拦截器（一处实现，覆盖全部同源调用）
  │    ① 仅**同源**请求附 X-Auth-Token
  │       （本机 AR 桥 :50687 是跨源，绝不能被带上 token）
  │    ② 同源 GET/DELETE 自动补 client_id；同源 FormData 自动补 client_id
  │    ③ 同源 401（且确实带了 token）→ 清除令牌 + 全局登出 + 提示重登
  └─ #page-login：登录 / 注册 / 登出 / 匿名记录并入（手动确认）
```

**为何网页端也要有纯 JS 的 SHA-256**：`crypto.subtle` 只在**安全上下文**可用，
而手机浏览器通过 `http://<局域网IP>:50865` 访问时并非安全上下文。
实现位于 `web/index.html`，其正确性由 `tests/tools/web_sha256_check.js`
（用 node 内置 crypto 交叉验证，含 55/56/64 等填充边界）守住。

**会话过期语义**：令牌 **30 分钟滑动过期**（`OMNI3D_SESSION_TTL` 可覆盖）。
两端失败姿势一致：

| 客户端     | 收到 401 后的行为                                              |
| ---------- | -------------------------------------------------------------- |
| 网页 / App | 清 `localStorage` 令牌、切到登录页、toast「登录状态已过期」    |
| 桌面端     | `ApiClient` 抛 `AuthExpiredError` → `AppController` 退回登录窗 |

> 登录接口自身在密码错误时也返回 401，因此只有**已携带令牌**的 401
> 才被当作「会话过期」，否则当作普通错误提示。

---

## 6. 真实尺度

**问题**：重建点云在**模型坐标系**中是任意单位；测距结果不是米。

**解法一（主）：AR 位姿驱动** —— `app/core/pipeline.py`

```
上传带 extrinsics（每帧 col-major 4×4 cam2world，平移为米）
        │
        ▼
① align_local_pts3d_to_global  → 写入每帧 camera_center（模型坐标系）
② metric_alignment(preds, extrinsics)
        │   「模型预测相机轨迹」×「AR 米制轨迹」做相似配准
        ▼
   s, R, T          （p_metric = s·R·p_model + T）
        │
        ▼
③ apply_similarity 就地变换 pts3d_in_other_view
        │
        ▼
点云即**米制**且位于 AR 世界坐标系；result.scale = s（并写入 session.scale）
```

护栏：有效帧数 < 2 / 轨迹几乎不动 / 尺度离谱（≤1e-9 或 ≥1e9）→ 拒绝对齐，`scale = null`。

**解法二（兜底）：标尺校准**

```
用户点选两点 (point_a, point_b) + 输入已知真实距离 (real_distance)
        │
        ▼
POST /api/tasks/{task_id}/scale   {point_a, point_b, real_distance}
        │
        ▼
scale = real_distance / ‖point_a − point_b‖       （app/core/scale.py）
        │
        ▼
session_store.update_scale(...)   → 后端历史记录 scale
```

- **AR 场景**走解法一（无需手动标定）；**纯 web 场景**走解法二。
- `intrinsics` 目前只做**校验与报告**（`result.metric.intrinsics` 逐视图 fx/fy/cx/cy），
  未参与几何求解 —— 约束模型焦距需要 DUSt3R 的全局优化（`global_aligner`），代价大，暂不做。
- 两条路径都**只在 server 实现一次**，web / desktop 共用。

测试：`tests/test_pipeline_metric.py`（19 项纯函数，含精确还原 scale）+
`tests/test_metric_integration.py`（3 项，跑真实模型，验证 scale 还原与点云距离缩放）。

---

## 7. 点云输出（可视化 / PLY）

**问题（旧实现）**：服务器把整份 **ASCII** PLY 塞进结果 JSON，且所有点用同一个颜色
`(255,180,60)`，前端还只取 `points[:20000]`、并按 `stride=8` 抽稀。结果就是
「百万点云里前端只看到 2 万点、还全是橙色」。

**现在的管线**（`web/server.py`）：

```
run_reconstruction → output_dict{preds, views}
        │
        ├─ _collect_points
        │     ① 全分辨率（不再 stride=8）
        │     ② 用 pred[conf_key] 过滤最低的 VIS_CONF_PERCENTILE%（默认 10，可配）
        │        （conf_key 随点云来源变：conf_local / conf，见 pointcloud_keys）
        │     ③ 从 views[i]["img"] 取**真实 RGB**
        │        （img 是 ImgNorm 过的张量：(x/255-0.5)/0.5 → 反归一化 (x+1)/2*255）
        ├─ _reject_outliers
        │     ④ SOR 统计离群点剔除（SOR_K/SOR_STD，任一为 0 关闭）
        │        置信度过滤挡不住几何飞点（背景飞点置信度常常不低）
        ▼
   (points, colors)                         ┌─ _sample_for_render：均匀抽样到
        │                                    │   MAX_RENDER_POINTS（默认 6 万）
        │                                    │   → 合并成 [x,y,z,r,g,b] 回传（渲染用）
        │                                    │   ⚠ 抽样必须在剔除**之后**
        └─ _pts_to_ply → binary_little_endian┘
              向量化 numpy 结构化数组，落盘到 data/sessions/{id}.ply
```

**关键约定**

| 项           | 约定                                                                        | 理由                                                                                                                       |
| ------------ | --------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| 渲染点数上限 | `config.MAX_RENDER_POINTS`（`OMNI3D_MAX_RENDER_POINTS` 可覆盖，默认 60000） | 上限过高会让手机端 JSON 解析变慢                                                                                           |
| 点云来源     | `config.PTS3D_SOURCE`（`OMNI3D_PTS3D_SOURCE` 可覆盖，默认 `local`）         | 跟随上游重建评测口径（`eval_use_pts3d_from_local_head: true`）；两档共用同一全局坐标系，且一次前向同时产出，切换**零成本** |
| PLY 运输     | **不进 JSON**，只落盘 + `GET /api/history/{id}/ply`                         | 百万点 ASCII ≈ 60~80MB，内嵌会让响应体爆掉                                                                                 |
| PLY 格式     | `binary_little_endian`（x/y/z float32 + RGB uchar）                         | 同点数 20.9MB vs ASCII 62.7MB                                                                                              |
| 点云元素     | 有颜色时 `[x,y,z,r,g,b]`，否则 `[x,y,z]` 由客户端按高度着色                 | 两端共用一套解码（网页 `decodePointCloud` / `fillHeightColors`）                                                           |

**实测（family 12 帧 @512）**：全量 1,460,540 点 → 渲染 60,000 点，结果 JSON **4.6MB**，
PLY **20.9MB**（二进制）。前 5000 个渲染点的唯一颜色数 4091（旧实现为 **1**）。

**已知限制**

- `224` 模式在 `fast3r/dust3r/utils/image.py` 里会**按短边裁成 224×224 正方形**：
  横幅丢左右两侧、竖幅丢上下两端（不是等比缩放）。该文件属 vendored 代码，
  按硬约束不修改；需要完整画幅请用 `512`。
- 历史记录页加载旧会话时点云只有 `[x,y,z]`（颜色不落库），会回退为高度着色；
  完整彩色点云随时可从 PLY 下载。

---

## 8. 改造计划

### ✅ Phase 1（本 PR）——已完成

- [x] 会话层 `web/session_store.py`（SQLite，按 `owner` 隔离）
- [x] server 接入会话层 + 历史 API；同步/异步两条路径均落库
- [x] 尺度反推纯函数 `app/core/scale.py` + API `POST /api/tasks/{id}/scale`
- [x] 认证与会话管理：`auth_store.py`（salt+verifier）+ `session_manager.py`（token→username）
- [x] 历史**对应到 username**（`owner = user:<name>` / `anon:<client_id>`）
- [x] 桌面客户端：`desktop/`（PyQt5 + VTK，登录窗 ⚙ 设置服务器）→ **已于 Phase 1.6 归档**
- [x] 废弃物清理（`__pycache__`、`temp_preview_frames/`）
- [x] 补 `CONTEXT.md`（领域词汇）与本文件

### ✅ Phase 1.5（本 PR）——客户端接入认证

- [x] 网页 client 登录/注册/登出页（`#page-login`），浏览器与移动端 App **共用同一套 UI**
- [x] 统一 `fetch` 拦截器：同源附 `X-Auth-Token`、同源 401 全局登出、AR 桥跨源放行
- [x] 匿名身份：`localStorage` 随机 `client_id`；登录后可**手动确认**并入账号
- [x] `GET /api/auth/claim/preview` 预览条数；`claim` 同时迁移会话层与内存任务表
- [x] 会话令牌 **30 分钟滑动过期**（`OMNI3D_SESSION_TTL` 可覆盖）
- [x] 用户名规则服务端强制；密码规则在客户端（协议不上行明文密码）
- [x] 任务列表按归属隔离（`GET /api/tasks?client_id=`）；网页历史页改用 `/api/history`
- [x] 桌面端 401 → `AuthExpiredError` → 退回登录窗（该客户端已在 Phase 1.7 归档）
- [x] 移动端 App 顶部工具条 ⚙：**原生**修改服务器地址（写 `home_url.txt` 后重载）

### ✅ Phase 1.6（本 PR）——点云质量

- [x] 点云上**真实 RGB**（网页 vertexColors / PLY）
- [x] 去掉 `stride=8` 抽稀与 `points[:20000]` 硬编码，上限改为 `OMNI3D_MAX_RENDER_POINTS`
- [x] 接通置信度过滤（原 `VIS_CONF_PERCENTILE` 是死配置，从未生效）
- [x] PLY 改**二进制小端**且不再内嵌 JSON（响应体从几十 MB 降到 MB 级）
- [x] 网页点云解码抽成纯函数 `decodePointCloud`，由 `tests/tools/web_pointcloud_check.js` 守住

### ✅ Phase 1.7（本 PR）——收敛为单一客户端

- [x] `desktop/` → `archive/desktop-pyqt-vtk/`（保留历史与可复用点，不再维护）
- [x] `run.ps1` 移除 `desktop` 目标；`CONTEXT.md` / `README.md` / `docs/API.md` /
      `docs/ARCHITECTURE.md` / `CONTRIBUTING.md` / `qt_app/README.md` 口径统一为「一个 web client」
- [x] 测试去掉对归档代码的依赖，改为**跨语言固定向量**：
      `tests/test_auth.py` 与 `tests/tools/web_sha256_check.js` 共用同一组 verifier/proof 字面量；
      另增加「网页端账号规则常量 == 服务端常量」的断言

### ⏳ Phase 2（独立 PR）——物理重组

- [ ] `web/` → `server/`（`server/app.py` / `auth_store.py` / `session_manager.py` / `session_store.py` / `task_queue.py`）
- [ ] 前端 `web/index.html` → `client/web/`
- [ ] `qt_app/` → `client/qt/`（**桌面端已在 Phase 1.6 归档，不再有 `client/desktop/`**）
- [ ] 同步修改 `qt_app/build_apk.ps1`、`CMakeLists.txt`、`sys.path`、所有 import
- [ ] Qt 桥的 `omni3d_history.json` 改为调用 server `/api/history`（去掉重复实现）

> Phase 2 涉及构建脚本与导入路径，破坏性大，故单独成 PR 评审。

---

## 9. 部署（已延后至 release 阶段）

为专注开发，本项目**当前的部署资产已移除**，将于打包 release 时重建。

| 已移除                  | 原因                                   |
| ----------------------- | -------------------------------------- |
| `docs/DEPLOYMENT.md`    | 部署指南（发布时重写）                 |
| `docs/frp/`、`web/frp/` | frp 隧道配置与文档                     |
| `Dockerfile`            | 上游 Fast3R demo 容器，本项目未使用    |
| `.env.example`          | Hydra 模板残留，未使用                 |
| `scripts/slurm/`        | 上游 Fast3R 集群作业脚本，本项目不训练 |
| `frp/`（本机）          | 本机 frp 运行配置                      |

**开发期如何启动服务**：本地直接 `python web/server.py`（默认 `127.0.0.1:50865`），
无需任何部署配置。

> 需要恢复上述文件时，从本改动之前的提交中 checkout 即可（git 历史保留）。
