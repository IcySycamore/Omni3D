# Omni3D 交接说明 —— v1.0.0-mvp（Python 参考实现） → C# 重构

> **这份文档为谁写**：接手 C# / .NET 重构的人（以及三个月后的你自己）。
> **为什么写**：本仓（Omni3D，Python）已冻结为 **v1.0.0-mvp 参考实现**并发起归档；
> 后续开发换语言重写。归档之后，**唯一还能证明"旧行为是什么"的东西就是这份文档 + 那批测试**。
>
> 配套：领域词汇见 [`CONTEXT.md`](../CONTEXT.md)、模块地图见 [`ARCHITECTURE.md`](ARCHITECTURE.md)、
> 接口清单见 [`API.md`](API.md)、部署见 [`DEPLOYMENT.md`](DEPLOYMENT.md)。

---

## 1. 本版是什么

一句话：**从手机视频 / 图片重建出可测量的点云**，并且**已经是一个能收费的服务**。

三条腿（各自独立部署）：

| 角色                       | 端口  | 进程            | 职责                                                |
| -------------------------- | ----- | --------------- | --------------------------------------------------- |
| **面板 panel**             | 50866 | `web/pages.py`  | 只交付页面（无 torch），浏览器里干活的地方          |
| **官网 portal**            | 50867 | `web/portal.py` | 账号 / 计费 / 发 API Key / 流水（**控制面**）       |
| **服务商 API**             | 50865 | `web/server.py` | 重建任务、会话历史、点云/测量（**数据面**，需 GPU） |
| 本地 AR 桥（不在本次范围） | 50687 | `qt_app/`       | App 壳的原生能力（AR 扫描 / 文件）                  |

本地启动（Windows）：

```powershell
.\run.ps1 server    # :50865 重建 API
.\run.ps1 pages     # :50866 面板
.\run.ps1 portal    # :50867 官网
.\run.ps1 test      # 全量测试：313 项
```

依赖：`requirements-app.txt`（**不要**装根 `requirements.txt`，那是研究栈）。权重用
`OMNI3D_CHECKPOINT_DIR` 指向挂载卷（默认读仓库内被 ignore 的 `jedyang97/`）。

---

## 2. 架构现状

```
浏览器（面板 web/index.html）
   ├─ 走 /api/*  ──► 服务商 API :50865 ──► Fast3R 前向（GPU）──► 点云 / PLY / 测量
   └─ 走 /api/p/* ─► 官网 :50867 ──► 控制面 SQLite（账号 / Key / 计费 / 流水）
                        ▲
                   服务商用它校验 API Key（同一份实现 + 同一份库，同机部署）
```

**存储（都在 `data/`，SQLite）**

| 文件                                         | 表                                                                | 归谁             |
| -------------------------------------------- | ----------------------------------------------------------------- | ---------------- |
| `omni3d_users.db`                            | `users(username, salt, verifier)`                                 | 认证（两侧共用） |
| `omni3d_portal.db`                           | `api_keys` / `accounts` / `packs` / `orders` / `usage` / `ledger` | 控制面（官网）   |
| `omni3d_sessions.db` + `data/sessions/*.ply` | `sessions`                                                        | 数据面（服务商） |

**认证协议（两侧同一份实现：`web/auth_api.py`）**

```
注册：salt = 服务端随机；verifier = sha256(salt + password)   ← 客户端算，明文不上网
登录：nonce ← POST /api/auth/challenge；proof = sha256(nonce + verifier) → POST /api/auth/login
令牌：X-Auth-Token，30 分钟滑动过期（OMNI3D_SESSION_TTL）
改密：POST /api/auth/password = 旧密码的 nonce/proof + 新的 salt/verifier
API Key：官网签发（omni3d_ + 24 字节随机），库里只存 sha256；请求带 X-Api-Key
身份优先级：X-Api-Key > X-Auth-Token > 匿名（client_id）
```

**计费（只由官网记账；常量全在 `web/portal_store.py`）**

- 计量三档：点云 `200 点 = 1 分` / 体素 `1,000 = 1 分` / 网格 `1,000 三角面 = 1 分`
  （`units_to_cents()` 向上取整）；
- 计划：Personal $8.99/100 次、Professional $18.99/600 次，**30 天重置**；
- 用量包：每种计量 100 万单位、预付 9 折、**不过期**、**各包独立**；
- 余额：按量兜底，充值档 $5/10/20/50；
- **扣减顺序（唯一入口 `charge()`）**：计划包（扣 1 次即覆盖整次调用）→ 同计量用量包（先买先扣，可跨包）→ 余额；
- 每次扣减都写 `ledger`（含余额快照）；`usage` 表按 `task_id` 唯一 → **计量天然幂等**；
- `BETA_FREE=True`（验证阶段）：包照扣、**余额不扣**、额度不足**也不拦**（`billing_enforced()` 为假）。

**服务商侧的计费接入**：入队前只 `can_start()`（查有没有额度，**不预扣** —— 实际用量要跑完才知道）；
任务完成、写入会话之后才 `record_usage(user, task_id, units, "points")`。

---

## 3. 冻结的行为契约（重构必须复现）

这些是**对外可观测**的行为，重写时逐条对照。字段名建议保持（客户端/文档/账单都依赖）。

### 3.1 上传与结果

- 上传：`multipart/form-data` —— `files`（图片或视频，可多张）、`resolution`（512 | 224）、
  `intrinsics`（JSON，每视图 9 元素 K）、`extrinsics`（JSON，每视图 16 float **列主序 cam2world，单位米**）、
  `is_video`、`frame_count`、`client_id`。
- 结果 JSON：`points`（**渲染子集**，`[x,y,z,r,g,b]`，上限 `OMNI3D_MAX_RENDER_POINTS`，默认 6 万）、
  `num_points`（全量，已置信度过滤 + SOR）、`num_points_raw`、`num_points_removed`、`elapsed_s`、
  `scale`、`metric{aligned, scale, n_views, source, intrinsics}`、`task_id`。
- **完整点云不进 JSON**：走 `GET /api/history/{id}/ply`（binary_little_endian PLY，坐标 float64，含 RGB）。
- 任务：`POST /api/tasks` → 202 `{task_id, queue_pos}`；`GET /api/tasks/{id}` 轮询（`status/progress/stage`）。

### 3.2 会话层（历史）

- `task_id` **就是** `session_id`；归属键 = `user:<username>`（登录/Key）或 `anon:<client_id>`（匿名）；
- 历史一律读 `GET /api/history`（持久层），**不是** `/api/tasks`（内存表，重启即丢）；
- 标注/测量：`PUT /api/sessions/{id}/annotations` **整体替换、幂等**；数值一律服务端算
  （客户端上行的 `raw/value` 会被剥掉）；
- 尺度：`POST /api/tasks/{id}/scale`（两点 + 真实长度 → 服务端反推并持久化，服务端是唯一真相）。

### 3.3 几何口径（改了就是行为变更）

- 长度 = 两点距离；**面积 = |AB × AC|**（两向量张成的平行四边形 = 三角形面积的 2 倍，UI 必须写清）；
  体积 = |det[AB, AC, AD]|（平行六面体）。
- 未标定时显示 `u / u² / u³`；标定后按 `×s²` / `×s³` 换算成 `m`。
- 点云来源默认 **local head**（`OMNI3D_PTS3D_SOURCE=local`，对齐上游评测口径）；
  两档共用同一全局坐标系，中位最近邻只差 **0.032%** 场景对角线（本机实测，见 §3.5）。
- SOR 离群点剔除默认开（`OMNI3D_SOR_K=8`、`OMNI3D_SOR_STD=2.0`，**任一为 0 = 关闭**）；
  **显示 / PLY / 测量用同一份点云**（看到的 = 量到的 = 导出的）。
- `resolution=224` 在 vendored 上游代码里是**按短边裁成正方形**（横幅丢左右）→ 只配预览，不能用于测量。

### 3.4 前端行为

- 面板是**单文件** `web/index.html`（原生 ESM + three.js，无构建）；页面首次打开读 `GET /app-config.json`
  拿引导信息（默认 API 端口、官网端口）。
- 凭据**按服务商分开存**（`omni3d.cred:<serverId>`）：换服务商 = 换身份，绝不存在"多台共用账号"。
- 就绪灯 = **身份已验证 + 模型已加载**（只有 `/health` 通不算）。
- 选中/工具模型：选择 + 框选 + 移动三选一；测量工具"选中数正好 = 需要点数"才可应用。

### 3.5 精度 / 性能基线（本机实测，用于回归对照）

| 项                         | 数值                                       | 复现                                |
| -------------------------- | ------------------------------------------ | ----------------------------------- |
| family 16 帧 @512 全量点数 | 2,123,457 → 2,027,138（SOR 剔除 4.536%）   | `python scripts\sor_report.py`      |
| SOR 耗时                   | 1.02 s（0.48 µs/点，≈ 前向 3.85 s 的 26%） | 同上                                |
| local vs global head       | 中位最近邻差 0.032% 场景对角线             | `python scripts\ab_pts3d_source.py` |
| 单次前向                   | ≈ 3.85 s（本机 CUDA）                      | `.\run.ps1 bench`                   |

⚠️ 这些数值**依赖权重版本**；换权重 / 换精度（fp16）必须重新测，别拿旧数值当基线。

---

## 4. 重构决策（本轮设计审查的结论）

已定（详见触发这些结论的讨论记录，这里是结论）：

| 决策点           | 结论                                                                                                                                                                                                                                                                                           |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 重写动机         | **组织约束**（长期只维护一门后端语言），**不是**"Python 慢/不安全"                                                                                                                                                                                                                             |
| 后端语言         | **C# / .NET 8+**（ASP.NET Core + EF Core + SignalR）                                                                                                                                                                                                                                           |
| 推理层           | **Python 只保留无状态 GPU worker**（零业务、零数据库），C# 用 gRPC 调它；ONNX 导出另开 spike                                                                                                                                                                                                   |
| 服务拓扑         | **Portal / Api / 静态面板 三服务独立部署**，共享类库                                                                                                                                                                                                                                           |
| 控制面数据       | **Portal 独占控制面库**；Api 经**内部 API + mTLS** 校验与上报（吊销即时生效）                                                                                                                                                                                                                  |
| 热路径           | **提交时不查配额**（可用性优先）：完成后 `Charge`；靠限流 + 并发上限 + 输入上限 + `arrears` + **扣押产物**兜底                                                                                                                                                                                 |
| 传输             | 对外 **HTTPS REST + WebSocket**（每用户一个 async 会话）；内部 gRPC/TCP 到 **每 GPU 一个 worker**                                                                                                                                                                                              |
| 数据面           | Api 自己的 Postgres（独立 schema/角色）+ **S3/MinIO** 产物（provider 模式可回落本地卷）                                                                                                                                                                                                        |
| 对外协议         | **复用行业标准**（不发明新协议）：作业语义走 **OGC API - Processes Part 1: Core**，领域词汇与形状对齐 **NodeODM REST**（`/info` 能力自述 / `/options` 参数自述 / init→upload→commit / 队列状态码 / webhook）；凭据改用 Bearer，错误改用 problem+json + 幂等键；**不抄 chat 语义**（详见 §5.1） |
| 内部 worker 契约 | **KServe Open Inference Protocol (V2)**（gRPC `ModelInfer`，Triton/TorchServe/KServe 均实现）或极简自定义 gRPC                                                                                                                                                                                 |
| 前端             | **TS + Vite + Vue 3**；three.js 走 npm，THREE 对象必须 `markRaw` 出响应式系统                                                                                                                                                                                                                  |
| 任务模型         | 每用户一个通信会话（async，不是 OS 线程）；总队列 + 执行单元（GPU）分配，队列满 → 429                                                                                                                                                                                                          |

未定（必须在新仓开工前回答）：

1. **模型代码交接方式**（本仓打成 `omni3d-model` wheel / vendor 副本 / pin 上游 + 复现 spike）；
2. `provider` 角色（客户自建）的账号体系是否退化为"本地 Key 即可"；
3. 限流与并发上限的**具体阈值**（现在只有"要限"这个决定）；
4. `arrears` 的**结清流程**（补缴后何时释放产物、是否自动重试）；
5. Vue 生态细节（Pinia / UI 库 / 是否沿用现有自绘 CSS）。

---

## 5. 接口地图（旧 → 新）

| 现在（`docs/API.md`）                                                         | 重构后（建议）                                                | 变化                                |
| ----------------------------------------------------------------------------- | ------------------------------------------------------------- | ----------------------------------- |
| `POST /api/tasks`（multipart 上传）                                           | `POST /v1/uploads` + `POST /v1/tasks`（JSON 引用）            | 大文件走预签名直传，任务提交变 JSON |
| `GET /api/tasks/{id}` 轮询                                                    | `GET /v1/tasks/{id}` + `GET /v1/tasks/{id}/events`（SSE/WS）  | 推送代替轮询                        |
| `GET /api/history/{id}/ply`                                                   | `GET /v1/tasks/{id}/artifacts/pointcloud.ply`（短期签名 URL） | 产物门控（未结清不签发）            |
| `X-Auth-Token` / `X-Api-Key` 自定义头                                         | `Authorization: Bearer omni3d_…`                              | 统一凭据表达                        |
| `{error: "…"}` 临时字符串                                                     | problem+json + 稳定错误码                                     | 错误可编程                          |
| `POST /api/tasks/{id}/scale`、`/api/sessions/{id}/{annotations,snap,measure}` | 同名资源 + `/v1` 前缀                                         | 语义不变，**契约不变**              |
| `GET /health`、`GET /api/models`                                              | `GET /v1/models`（OpenAI 兼容形状）+ `/healthz`               | 探活与模型清单分离                  |

⚠️ **`/api/sessions/{id}/annotations` 的幂等整体替换语义、面积/体积公式、PLY 格式不要改** ——
改了就是数据契约变更，老客户端与已有历史都会对不上。

### 5.1 协议复用清单（**不发明新协议**）

三类现成标准，各管一层：

| 层次                | 采用                                                                                                              | 从中拿到什么                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | 要避开 / 替换                                                                                                                                   |
| ------------------- | ----------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| **作业语义**        | **OGC API - Processes Part 1: Core**（OGC 正式标准 18-062r2，有 OpenAPI 定义 + Schema 仓库 + **合规测试与认证**） | “把计算任务包装成 process、客户端 REST+JSON 执行、返回 job 资源可查状态/结果/删除”——正是我们要的形状；白拿语义定义与合规套件                                                                                                                                                                                                                                                                                                                                                                                                                     | 它的数据模型偏 GIS（coverages/vector），只需取其 **job 生命周期**部分，不必引入其数据编码                                                       |
| **领域词汇与形状**  | **NodeODM REST v2.2.1**（AGPL-3.0；航测开源界事实标准，WebODM 就是它的客户端）                                    | `GET /info`（`engine/engineVersion/maxImages/maxParallelTasks/taskQueueCount/cpuCores/availableMemory`）← **“执行单元与硬件相关 + 队列深度”就是它**；`GET /options` ← 处理参数**运行时协商**；`POST /task/new`（`options` JSON 数组 + `webhook` URL）；`init → upload/{uuid}（可多次）→ commit/{uuid}` 预上传；`GET /task/{uuid}/info` 的 `status.code` = 10 QUEUED/20 RUNNING/30 FAILED/40 COMPLETED/50 CANCELED + `progress` + `processingTime`；`GET /task/{uuid}/output?line=N` 增量拉日志；`cancel/restart/remove/list`；`download/{asset}` | ⚠️ token 走 **query 参数**（我们改 Bearer）；⚠️ 错误是裸 `{error: string}` / `{success: bool}`（我们改 **RFC 9457 problem+json** + 稳定错误码） |
| **worker 内部契约** | **KServe Open Inference Protocol (V2)**（gRPC `ModelInfer` + REST 双协议）                                        | 成文的“推理服务”接口，生态工具直接可用                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | 若只传帧+参数（不跑多模型/多版本），可用极简自定义 gRPC，别为协议而协议                                                                         |

**零碎但成型的部件**（直接用，不要自造）：CloudEvents（webhook 事件信封）、RFC 9457 `problem+json`、`Idempotency-Key`（重试去重——在“提交时不查配额”的方案下尤其重要）、
**tus** 或 S3 预签名 multipart（上传）、AsyncAPI（描述推送接口）；
产物与流式加载：**3D Tiles 1.1 / glTF 2.0 / PLY / COPC / EPT / Potree 八叉树**
（现在是一次性渲染子集 JSON，大点云会顶到 `OMNI3D_MAX_RENDER_POINTS` 上限）。

**市场缺口（也就是我们必须自己造的部分）**：开源界**没有**“官方服务商 = 账号 + 计量计费 + 发 Key + 流水 + 面板”这一整条
（WebODM 有账号/配额但**无计费**；Replicate 的 cog / HF / Fal 有托管契约但**无计费逻辑**）。
→ **控制面（Portal）与计费模型是我们的产品资产；协议层一律站到已有标准上。**

---

## 6. 迁移顺序建议（strangler）

每一步都有**可验收的出口**，不要一次全切：

1. **控制面**（Portal：账号 + 计费 + Key + 流水）→ C# + Postgres。出口：官网页面全功能可用，
   `charge()` 的扣减顺序与流水字段与旧版一致（用旧库导出做对账测试）。
2. **网关**（Api 的 REST/WS + 认证 + 额度校验）→ C#，**重建仍然转发给 Python**（旧的 `web/server.py`
   直接当 worker 用）。出口：面板不改一行代码就能跑通全流程（证明协议兼容）。
3. **几何与产物**（SOR / 吸附 / 测量 / PLY / 会话）→ C#。出口：同一份点云输入，几何结果与 Python
   实现数值一致（用 §3.5 的基线 + 现有测试向量）。
4. **worker 化**（Python 退成 gRPC worker，去掉 FastAPI/会话/账务）→ 出口：worker 无 HTTP 端口、
   无数据库连接、只吃帧返回点云。
5. **前端**（TS + Vite + Vue）→ 出口：面板与官网达到今天的功能等价，且构建产物离线可用（无 CDN）。
6. （可选）**ONNX spike** → 出口：GPU 逐层数值对齐（阈值 1e-4）+ 端到端点数与位姿对齐；
   达不到就**不切**，保留 Python worker。

---

## 7. 模型与 worker 交接 TODO

- **权重**：上游 HF `jedyang97/Fast3R_ViT_Large_512`（`model.safetensors` **2.47 GB**，未入库，
  `.gitignore` 挡住 `*.safetensors` 与 `jedyang97/`）。新仓必须 pin **revision**（不是"latest"）并记录 hash。
- ⚠️ **模型代码不是"纯上游"**：`fast3r/` `dust3r/` `croco/` 是 vendored 副本，我们改过两类东西 ——
  1. 大面积补中文 docstring；2) **修过 `blocks.py` 的语法错误**（commit `08dac33`）。
     → 所以"新仓直接 pip 装上游 Fast3R 就行"这句话**尚未验证**：开工前先做一次干净环境复现，
     复现不过就把我们的补丁做成 patch 文件（或走 wheel 方案）。
- **未定项**（§4 第 1 条）三选一，各有代价：
  - `omni3d-model` wheel（本仓打包，worker 依赖版本号）→ 版本锁最硬，需要先做打包；
  - vendor 副本搬进新仓 → 最可复现，但新仓多一堆模型代码；
  - pin 上游 commit → 与上游保持关系，但需先证明上游能跑。
- worker 契约建议：`RunTask(frames, params) → stream(progress/stage) → result(points/poses/metrics)`，
  **worker 不碰数据库、不碰账务、不认识用户**（只认一个不透明的 task 上下文）。

---

## 8. 已知陷阱（都是踩过的）

**架构 / 协议**

- 分享同一份 SQLite 只在本机成立 —— 控制面与数据面**分机器就必须拆库**（本版是同机假设）。
- 认证端点只能有一份实现（`web/auth_api.py`）；两边各复制一次迟早协议漂移。
- API Key 身份下，**需要令牌的端点会 401**，客户端会误判成"凭据失效"（`refreshAnonPreview` 踩过）。
- 吊销 Key 必须**即时**对数据面生效 —— 所以"缓存 Key + TTL"要慎重（本版是查库）。

**前端**

- `canvas.toDataURL()` 在无 `preserveDrawingBuffer` 时是**陈旧**内容，不能当相机变化的观察窗（用截图人眼看）。
- `[hidden]` 会被作者样式盖掉（`.tool-btn{display:flex}`）→ 隐藏必须写 `[hidden]{display:none!important}`。
- 删 DOM 必须同步删 `dom.*` 引用与事件绑定，否则 `addEventListener` on null 会让整页挂掉。
- 浏览器里不能用 `crypto.subtle`（非安全上下文）→ 手写了纯 JS SHA-256。
- 只有同源请求能带令牌；AR 桥 `:50687` 是跨源，带 token 就是泄漏。
- 自动化测试时：`page.keyboard.*` 在窗口未聚焦会**静默丢按键**；`page.mouse.move` 到覆盖层经常送不到
  （用合成 `PointerEvent`）；confirm 弹窗要显式处理。

**Python / 环境**

- `import torch` 必须**最先**（且早于 numpy）——本机 `fbgemm.dll` 加载顺序冲突。
- 服务重启会清空内存令牌与任务队列（表现为 401 / 任务消失），这是预期。
- 修 `web/index.html` 或 `web/portal.html` 里任何面向用户的文案后，**跑测试**（有断言盯着界面文案）。

---

## 9. 验证资产

- `.\run.ps1 test` → **313 项**（≈20s）。分区：认证 / 会话层 / 计费与门户 / 托管与端口 /
  几何测量 / 吸附 / PLY / SOR / 尺度集成 / 面板接线与文案。
- 真机数据 E2E：`temp_preview_frames/_e2e_editor_api.py`（真实会话上验证 snap/measure/annotations/owner 隔离/幂等）。
- 基准：`scripts/sor_report.py`、`scripts/ab_pts3d_source.py`、`scripts/run_benchmark.py`。
- 前端静态检查：`temp_preview_frames/_check_js.py`（面板）、`_check_portal_js.py`（官网）→ `node --check`。

---

## 10. 本版已知限制 / 明确不做

- **不做支付网关**：演示环境所有下单都是 `amount = 0`（标价照记），正式收费只需把 `BETA_FREE` 置假。
- **体素 / 网格计量只有口径，没有产出**：当前管线只输出点云；体素/网格的 SKU 是给未来模型留的。
- 队列是**进程内内存表**（单副本、重启即丢），任务进度靠轮询 —— 这是 v1 最大的技术债，
  已被 §4 的"Postgres 任务表 + 推送"取代。
- `intrinsics` 目前只用于**校验与报告**，未约束模型焦距（要走上游 `global_aligner`，代价大，未做）。
- AR 桥 / 移动端 App 壳（`qt_app/`）不在本次重构范围。
