# 线协议（请求 / 进度 / 结果 / 产物怎么走网络）

> **这是本包的核心。** 范围就是你说的"数据 / 产物层协议"：**客户端与服务之间，请求与结果如何传输**。
> 它是**提案**（不是既成事实）；标 ★ 的条目是**待评审决策点**，其余是"已定/建议直接照做"。
> 冻结基线（旧实现的字段与数值口径）见 [`../HANDOVER.md`](../HANDOVER.md) §3。

## 0. 三条设计原则

1. **大文件与大结果不进 JSON**：帧走**直传**、点云走**二进制分块/瓦片**，JSON 只放"清单与指针"；
2. **状态可查询、进度可推送**：一切任务都是**可 GET 的资源**，进度同时支持推送（WS/SSE）与兜底轮询；
3. **形状复用行业标准，凭据/错误自己收紧**：作业语义对齐 OGC API-Processes、词汇对齐 NodeODM；
   但**凭据用 Bearer、错误用 problem+json**（ADR-0006）。

## 1. 三层信道

| 信道               | 用途                                  | 形态                                                    |
| ------------------ | ------------------------------------- | ------------------------------------------------------- |
| **A. 控制/资源信** | 建任务、查任务、列历史、读写标注/测量 | HTTPS + JSON（`/v1/...`）                               |
| **B. 实时信**      | 进度、阶段、日志、任务完成/失败       | **WebSocket**（每用户一条，多任务复用）；SSE / 轮询兜底 |
| **C. 产物信**      | 帧上传、点云瓦片、PLY 下载            | HTTPS + **二进制**（预签名直传对象存储 + Range 请求）   |

## 2. 端点总表（MVP 最小集）

| 方法     | 路径                                 | 说明                                                                                                                         |
| -------- | ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------- |
| `GET`    | `/v1/info`                           | **能力自述**：`{engine, engineVersion, units[], maxFrames, maxResolution, maxUploadBytes, gpuSlots, queueDepth, features[]}` |
| `GET`    | `/v1/options`                        | **参数自述**：`[{name, type, domain, default, help}]`（客户端据此渲染采集/重建表单）                                         |
| `POST`   | `/v1/uploads`                        | 申请直传：→ `{upload_id, url, method, headers, expires_at, max_bytes}`                                                       |
| `POST`   | `/v1/tasks`                          | 建任务（JSON，引用 `upload_id` 或 `http(s)://`/`s3://` 源）；需 `Idempotency-Key`                                            |
| `GET`    | `/v1/tasks/{id}`                     | 任务状态 + 结果清单（**不含点云本体**）                                                                                      |
| `DELETE` | `/v1/tasks/{id}`                     | 取消 / 删除（运行中=取消）；取消后产物按保留策略清理                                                                         |
| `POST`   | `/v1/tasks/{id}/retry`               | 重试（★ 是否保留由评审定）                                                                                                   |
| `GET`    | `/v1/tasks/{id}/events`              | SSE 兜底（浏览器无 WS 时）                                                                                                   |
| `GET`    | `/v1/tasks/{id}/artifacts`           | 产物清单 + **预签名 URL**（含大小/格式/有效期）                                                                              |
| `GET`    | `/v1/tasks/{id}/pointcloud/manifest` | **点云 LOD 索引**（见 §5）                                                                                                   |
| `GET`    | `/v1/tasks/{id}/pointcloud/{node}`   | 单个瓦片/分块（二进制，支持 `Range`）                                                                                        |
| `PUT`    | `/v1/sessions/{id}/annotations`      | 标注**幂等整体替换**（沿用旧契约：客户端只传 `{id,kind,op,refs}`）                                                           |
| `POST`   | `/v1/sessions/{id}/snap`             | 全量点云吸附（批量查询最近邻）                                                                                               |
| `POST`   | `/v1/sessions/{id}/measure`          | 服务端测量（长度/面积/体积/三角形）                                                                                          |
| `POST`   | `/v1/sessions/{id}/scale`            | 真实尺度反推（两点 + 真实长度）                                                                                              |
| `GET`    | `/v1/models`                         | 引擎/模型清单（OpenAI 兼容形状：`{object:"list", data:[...]}`）                                                              |

**认证**：`Authorization: Bearer omni3d_…`（撤销即时生效：每个请求过 Portal，见 ADR-0004）。

## 3. 提交（大文件走直传）

```http
POST /v1/uploads
{"kind":"frames","count":16,"bytes":48231456,"content_type":"image/jpeg"}
→ 201 {"upload_id":"u_...","url":"https://…","method":"PUT","headers":{...},"expires_at":1699999999}
```

客户端把每一帧 `PUT` 上去（**可并发**，失败可重试单帧）→ 再建任务：

```http
POST /v1/tasks
Idempotency-Key: 3f2c…            # 客户端生成；服务端 24h 内同 key 只建一个任务
{"model":"fast3r-vitl-512",
 "inputs":{"frames":[{"upload_id":"u_...","name":"f000.jpg"}, …]},
 "params":{"resolution":512,"frames":16},   # 合法键与取值范围来自 GET /v1/options
 "callback_url":"https://…/hook"}           # 可选 webhook（ADR-0006）
→ 202 {"task_id":"t_...","status":"queued","queue_pos":2,
       "events_url":"wss://…/v1/tasks/t_.../events","estimate_s":20}
```

★ **决策点**：视频是否允许"整段直传 + 服务端抽帧"（现在是客户端抽帧后按帧上传）；
建议**两者都支持**（`inputs.video.upload_id` 或 `inputs.frames[]`），由 `/v1/options` 声明。

## 4. 进度（推送 + 兜底）

**WebSocket**：`wss://<api>/v1/tasks/{id}/events`（每用户一条连接可订阅多任务）

```jsonc
{"type":"hello","session":"s_...","server_time":1699999999}
{"type":"progress","task_id":"t_…","status":"running","progress":0.35,"stage":"抽帧","stage_code":10}
{"type":"log","task_id":"t_…","line":"…"}                       // 可选：对应 NodeODM 的 console output
{"type":"done","task_id":"t_…","artifacts":[…]}
{"type":"error","task_id":"t_…","problem":{ "type":"…","title":"…","status":500,"code":"engine.failed" }}
```

- `progress` 是 0–1（旧实现是 0–100 的整数，**这是破坏性差异，写进迁移说明**）；
- `stage_code` 用 NodeODM 风格枚举（10 抽帧 / 20 重建 / 30 对齐 / 40 出产物），`stage` 是给人看的中文；
  这样对比"肉眼看的阶段"与"机器判断的阶段"都有；
- **兜底链**：WS 不通 → SSE（`GET …/events`）→ 轮询 `GET /v1/tasks/{id}`（间隔退避 1s→5s）。

★ **决策点（浏览器坑）**：WS 建连**不能自定义请求头** → 鉴权只能 ① 子协议 `Sec-WebSocket-Protocol: bearer.<token>`、
② 建连后首帧 `{"type":"auth","token":…}`（服务端在 auth 前只允许发这一帧）。
**不要**用 query 参数带 token（会进代理/网关日志）——这就是我们明确要避开 NodeODM 的那处败笔。

## 5. 结果与点云 LOD（panel 最关心的一节）

任务完成后 `GET /v1/tasks/{id}` 返回**清单**（不含点云本体）：

```jsonc
{"task_id":"t_…","status":"done","elapsed_s":24.7,
 "stats":{"num_points":2037138,"num_points_raw":2123457,"num_points_removed":86319,
          "n_views":16,"scale":null,"unit":"u","sor":{"k":8,"std":2.0},"pts3d_source":"local"},
 "artifacts":[
   {"name":"pointcloud.ply","kind":"pointcloud","format":"ply","bytes":55000000,"url":"https://…","expires_at":…},
   {"name":"tileset.json","kind":"tileset","format":"3d-tiles","bytes":2100,"url":"https://…"},
   {"name":"scene.copc.laz","kind":"pointcloud","format":"copc","bytes":38000000,"url":"https://…"}
 ]}
```

**点云在浏览器里怎么传（这是 LOD 的核心）：**

- **方案 A（推荐）**：产物侧生成 **3D Tiles 1.1**（`tileset.json` + `.glb`/`.pnts`，
  可选 Draco / EXT_meshopt 压缩），前端用 **CesiumJS 或 Potree** 消费。
  理由：**转换器与消费者都是现成的**（PDAL / py3dtiles / Cesium），我们只写"生成 + 托管"。
- **方案 B（自研最轻，若不想引 3D Tiles）**：`GET …/pointcloud/manifest` 返回八叉树索引，
  每块单独取：

```jsonc
// manifest
{"origin":[123.4,-56.7,890.1],          // ★ 局部原点：见下"精度"一条
 "nodes":[{"id":"0/0/0","bbox":[…],"count":48000,"lod":0,"url":"…/0/0/0"},
          {"id":"0/0/1", … }]}
```

- **方案 C**：直接给 **COPC（流式 LAZ）**，客户端按范围请求（`Range`）自行调度 —— 适合 GIS 客户，
  浏览器端要用 COPC 库（生态弱于 A）。

★ **决策点**：A / B / C 选哪个（建议 A，B 作为 panel 阶段先用、A 作为对外标准产物）。

**二进制分块布局（方案 B 用，规范写死以免两端各写一套）**：

```
偏移  类型        含义
0     char[4]     magic "OMC1"
4     uint32 LE   point_count
8     float32[3]  node 中心（局部原点）
20    float32     quantization_scale（坐标 = int32 * scale + center）
24    int32[3*n]  量化后的 xyz（小端，交错）
24+12n  uint8[3n] RGB
```

**精度一条（真实的坑）**：渲染坐标必须**减去任务中心点**再送 float32/量化，
否则大坐标（例如米制世界系下的 890m）在 float32 上只有厘米级抖动，量出来的尺寸会漂。

## 6. 统一约定（凭据 / 错误 / 幂等 / 版本）

| 项   | 约定                                                                                               |
| ---- | -------------------------------------------------------------------------------------------------- |
| 凭据 | `Authorization: Bearer omni3d_…`；撤销即时生效；**永不进 query**                                   |
| 错误 | **RFC 9457 problem+json**：`{type,title,status,detail,code,instance}`；`code` 是稳定机器码（见下） |
| 幂等 | 写操作支持 `Idempotency-Key`（24h）；服务端对同 key 返回**同一个资源**而不是新建                   |
| 版本 | 路径 `/v1`；破坏性变更加 `Omni3D-Api-Version: 2026-09-01`；下线前给 `Sunset` 头                    |
| 追踪 | 请求/响应带 `X-Request-Id`；`problem.instance` 引用它，便于对账                                    |
| 限流 | 429 + `Retry-After`；上限来自 `/v1/info` 的声明                                                    |

稳定错误码（首版）：`input.too_many_frames` · `input.too_large` · `input.unsupported_type` ·
`auth.missing` · `auth.invalid_key` · `auth.expired` · `quota.insufficient` · `account.arrears` ·
`rate.limited` · `task.not_found` · `task.not_ready` · `task.cancelled` · `engine.failed` ·
`engine.busy` · `artifact.expired`。

## 7. 尺寸与限制（写进 `/v1/info` 与 `/v1/options`，两端都读它）

| 项                 | 参考值（现状）                     | 说明                                             |
| ------------------ | ---------------------------------- | ------------------------------------------------ |
| 帧数               | 默认 16（可配）                    | 上限由服务端声明；客户端据此拦住提交             |
| 分辨率             | 512（默认）/ 224                   | 224 是**裁剪成正方形**，只用于预览，不能用于测量 |
| 单帧字节           | ≤ 8 MB（建议）                     | 直传分片与重试的粒度                             |
| 单任务上传总量     | ≤ 512 MB（建议）                   | 超过 → `input.too_large`                         |
| 渲染点数上限（旧） | 60,000                             | **LOD 上线后应取消这个上限**（改为按需取块）     |
| 产物 PLY 大小      | 百万级 ≈ 30–60 MB（float64 + RGB） | 用预签名 URL + `Range` 续传下载                  |

## 8. panel 阶段的最小实现集（先做的这一半）

panel 只需要实现上面这些的**客户端半边**，并用 **mock 服务**顶上后端。mock 必须能：
`/v1/info`、`/v1/options`、`/v1/uploads`（返回一个能 PUT 的本地 URL）、`/v1/tasks`（伪造进度 0→1）、
WS `events`（推 hello/progress/done）、`/v1/tasks/{id}`、`.../pointcloud/manifest` + 分块（**用合成点云**，
比如程序生成的球体/房间，几万个点就够验证 LOD）、`.../artifacts`（放一份真 PLY 供下载）。

> 这样 panel 的**全部功能（采集→提交→进度→LOD 查看→测量→标注→历史）都能在无模型、无 GPU 的条件下做完并测完**，
> 线协议也在这一阶段被客户端测试固化。

## 9. 未决项（并入 ADR 一并评审）

1. 3D Tiles / 自研分块 / COPC —— 选哪个做标准产物（§5 ★）；
2. 视频是否支持"整段直传 + 服务端抽帧"（§3 ★）；
3. WS 鉴权走子协议还是首帧（§4 ★）；
4. 是否需要 `retry` 端点，或"取消后重建"即等价；
5. 标注/测量端点是否也随 `v1` 改名为 `/v1/tasks/{id}/annotations`（现在是 `/api/sessions/...`）。
