# Omni3D 服务清单（API 参考）

> 本文是**接口的唯一权威来源**。领域词汇见 `CONTEXT.md`，分层与模块地图见 `docs/ARCHITECTURE.md`。

当前项目对外提供 **两个服务**：

| 服务           | 地址                                             | 实现                              | 何时存在                       |
| -------------- | ------------------------------------------------ | --------------------------------- | ------------------------------ |
| **重建服务器** | `http://127.0.0.1:50865`（`HOST`/`PORT` 可覆盖） | `web/server.py`（FastAPI）        | 总是（`python web/server.py`） |
| **App 本地桥** | `http://127.0.0.1:50687`                         | `qt_app/src/ar_bridge_server.cpp` | 仅移动端 App 内                |

> 原则：**重建能力只在服务器实现一次**；web / desktop / qt_app 都只是它的 client。

---

## 一、重建服务器（:50865）

### 1.1 页面与健康

| 方法 | 路径      | 说明                          |
| ---- | --------- | ----------------------------- |
| GET  | `/`       | 返回前端页面 `web/index.html` |
| GET  | `/health` | 模型就绪状态                  |

```jsonc
// GET /health
{ "ready": true, "device": "cuda", "error": null }
```

- `ready=false` 时，重建接口返回 **503**（模型首次加载需数分钟）。
- 交互式文档：`/docs`、`/redoc`、`/openapi.json`（FastAPI 自带）。

### 1.2 认证（挑战-应答）

明文密码**不上网、不落库**。约定：

```
verifier = sha256(salt + password)          ← 客户端计算
proof    = sha256(nonce + verifier)         ← 客户端计算
服务器比对 sha256(nonce + verifier_stored)
```

> `salt`/`nonce` 为 hex 字符串，拼接按其 ASCII 字节；密码按 UTF-8 字节；摘要为小写 hex。

| 方法 | 路径                         | 请求                             | 响应                                | 说明                                                  |
| ---- | ---------------------------- | -------------------------------- | ----------------------------------- | ----------------------------------------------------- |
| POST | `/api/auth/salt`             | `{username}`                     | `{salt}`                            | 注册前领 salt；用户名不合法 → **400**、重名 → **409** |
| POST | `/api/auth/register`         | `{username, salt, verifier}`     | `{ok, username}`                    | 创建账号；用户名不合法 → **400**、重名 → **409**      |
| POST | `/api/auth/challenge`        | `{username}`                     | `{nonce, salt}`                     | 登录第一步；用户不存在 → **401**                      |
| POST | `/api/auth/login`            | `{username, nonce, proof}`       | `{ok, token, username, expires_in}` | 登录第二步；失败 → **401**                            |
| GET  | `/api/auth/me`               | 头 `X-Auth-Token`                | `{ok, username, expires_in}`        | 校验令牌；无效/过期 → **401**                         |
| POST | `/api/auth/logout`           | 头 `X-Auth-Token`                | `{ok}`                              | 注销令牌                                              |
| GET  | `/api/auth/claim/preview`    | `client_id=` + 头 `X-Auth-Token` | `{ok, count, username, client_id}`  | 预览将并入多少条匿名历史（用于“是否并入”提示）        |
| POST | `/api/auth/claim?client_id=` | 头 `X-Auth-Token`                | `{ok, moved, username}`             | 把该 `client_id` 的**匿名历史并入账号**               |

**约束**：

- `nonce` **一次性**、5 分钟过期（重放 → 401）。
- 令牌有效期 **30 分钟（滑动）**：每次带 token 的请求都会续期，**持续操作不会掉线**，
  只有**静默超过 30 分钟**才失效。可用环境变量 `OMNI3D_SESSION_TTL`（秒）调整。
  令牌为内存态，服务器重启后需要重新登录（客户端会收到 401 并自动回登录界面）。
- **用户名规则**（服务端强制）：3–32 个字符，仅 `[A-Za-z0-9_.-]`。
- **密码规则**（至少 8 位、不得全空白）：**只能在客户端校验**。
  协议只上行 `verifier`，服务器从未接触明文密码，因此无法复核密码强度；
  网页端实同名规则（`web/index.html`）；服务端额外强制**用户名**规则。
- `claim` 同时迁移 **SQLite 会话层**与**内存任务表**的归属，保证并入后立即在历史列表可见。

### 1.3 重建任务

| 方法   | 路径                                   | 说明                                                               |
| ------ | -------------------------------------- | ------------------------------------------------------------------ |
| POST   | `/reconstruct`                         | **同步**重建（兼容旧客户端/web），阻塞直到出结果                   |
| POST   | `/api/tasks`                           | **异步**入队，立即返回 `task_id`（**202**）                        |
| GET    | `/api/tasks/{task_id}?include_result=` | 轮询状态；`done` 时附完整结果                                      |
| GET    | `/api/tasks?limit=20&client_id=`       | 任务列表（不含大数据）；**传 `client_id` 或带 token 时按归属隔离** |
| DELETE | `/api/tasks/{task_id}`                 | 删除任务（运行中不可删 → 404）                                     |

> `GET /api/tasks` 不带任何身份参数时保持旧行为（列出全部），仅便于调试脚本；
> 两个客户端都会带上身份，因此只能看到自己的任务。

> **历史用哪个接口？** 需要**持久化 + 按账号隔离**的历史（两个客户端的「历史记录」页）
> 请统一用 `GET /api/history`（SQLite 会话层）；`GET /api/tasks` 只是**内存任务表**，
> 服务器重启即清空，且仅在任务尚未落库时才有额外信息（如运行中进度）。

**上传契约**（`multipart/form-data`，两个重建接口一致）：

| 字段              | 类型        | 说明                                                                      |
| ----------------- | ----------- | ------------------------------------------------------------------------- |
| `files`           | file[]      | 图片序列，或**单个视频**（配合 `is_video=true`）                          |
| `resolution`      | int         | `512`（默认，完整画幅）或 `224`（**仅预览，勿用于测量**；会被裁成正方形） |
| `intrinsics`      | JSON 字符串 | 可选，每视图 3×3 内参                                                     |
| `extrinsics`      | JSON 字符串 | 可选，每视图 **col-major 4×4** 位姿                                       |
| `is_video`        | bool        | `/api/tasks` 专用                                                         |
| `frame_count`     | int         | `/api/tasks` 专用，视频均匀抽帧数（默认 16）                              |
| `client_id`       | str         | 未登录时的匿名归属键                                                      |
| 头 `X-Auth-Token` | str         | 可选；提供则历史归到该 username                                           |

**任务状态**：`queued` → `running` → `done` / `failed`

```jsonc
// GET /api/tasks/{id}?include_result=true
{
  "task_id": "ae20bba464f14784",
  "status": "done",
  "progress": 1.0,
  "stage": "完成",
  "error": null,
  "is_video": true,
  "num_views": 5,
  "result": {
    "ok": true, "num_views": 5, "num_points": 1460540, "elapsed_s": 2.3,
    "num_points_raw": 1532000,     // SOR（离群点剔除）**之前**的点数
    "num_points_removed": 71460,   // 被 SOR 剔除的点数
    "render_points": 60000,        // 实际回传给客户端的渲染点数
    "points": [[x, y, z, r, g, b], "..."],   // 6 元素：坐标 + **真实 RGB**（0~255）
    "has_colors": true,            // false → 客户端自行按高度着色
    "has_ply": true,               // PLY 不再内嵌在 JSON 里！
    "ply_bytes": 21908318,         // 完整 PLY 体积（二进制）
    "device": "cuda", "is_video": true,
    "scale": 1.198,                 // 米制尺度；未对齐时为 null
    "metric": {                     // 尺度换算详情（见 §1.5）
      "aligned": true,
      "scale": 1.198,
      "n_views": 5,                 // 参与对齐的帧数
      "source": "extrinsics",       // extrinsics | none
      "extrinsics_provided": true,
      "intrinsics": [{"fx": 512.0, "fy": 512.0, "cx": 320.0, "cy": 240.0}]
    }
  }
}
```

> **点云回传约定（重要）**
>
> - `points` 是**渲染子集**（均匀抽样，上限 `config.MAX_RENDER_POINTS`，默认 60000，
>   可用环境变量 `OMNI3D_MAX_RENDER_POINTS` 覆盖），每项为 `[x, y, z, r, g, b]`。
>   没有颜色时退化为 `[x, y, z]`（客户端按高度着色）。
> - **完整点云不走 JSON**：早期版本把整份 ASCII PLY 放进 `result.ply`，
>   百万点时响应体会膨胀到几十 MB，手机端解析直接卡死。
>   现在 PLY 落盘为**二进制小端**文件，客户端统一用 `GET /api/history/{id}/ply` 下载。
> - 服务端会先用 `OMNI3D_CONF_PERCENTILE`（对应 `config.VIS_CONF_PERCENTILE`，默认 10，
>   `0` = 不过滤）**过滤置信度最低的点**，
>   再按 `config.SOR_K` / `config.SOR_STD`（`OMNI3D_SOR_K` / `OMNI3D_SOR_STD`，
>   默认 8 / 2.0，任一为 `0` 即关闭）做 **SOR 统计离群点剔除**。
>   - `num_points_raw` = 置信度过滤后、SOR **前**的点数
>   - `num_points` = SOR **后**的点数（即 PLY 与测量使用的点数）
>   - `num_points_removed` = `num_points_raw - num_points`
>   - 剔除后的点云**同时**用于渲染、PLY 导出与测量（看到的 = 量到的 = 导出的）

### 1.4 历史（按 username 隔离）

| 方法   | 路径                                           | 说明                                       |
| ------ | ---------------------------------------------- | ------------------------------------------ |
| GET    | `/api/history?client_id=&limit=50`             | 列出归属的历史（轻量，无点云/PLY）         |
| GET    | `/api/history/{id}?client_id=&include_points=` | 单条；`include_points=true` 时带渲染点云   |
| GET    | `/api/history/{id}/ply?client_id=`             | 下载完整 PLY（**二进制小端**，含真实 RGB） |
| DELETE | `/api/history/{id}?client_id=`                 | 删除一条（**级联删除标注**）               |
| PUT    | `/api/sessions/{id}/annotations`               | **幂等整体替换**该会话的标注（见下）       |

- 归属由 `_owner_of(token, client_id)` 决定：登录 → `user:<username>`，匿名 → `anon:<client_id>`。
- 不匹配归属一律 **404**（历史隔离）。
- 响应含 `owner`、`username`、`scale`、`real_distance`、`status` 等字段。
- `GET /api/history/{id}` 还附带 **`annotations`**（该会话的标注整体，无则 `null`）。

#### 标注：`PUT /api/sessions/{id}/annotations`

```http
PUT /api/sessions/{id}/annotations?client_id=alice
Content-Type: application/json

{
  "version": 1,
  "elements": [
    { "id": "e1", "kind": "point",   "points": [[x, y, z]] },
    { "id": "e2", "kind": "point",   "points": [[x, y, z]] },
    { "id": "e3", "kind": "segment", "refs": ["e1", "e2"] }
  ]
}
```

- **幂等整体替换**：请求体就是该会话标注的**全部内容**，`session_id` 是主键，
  所以**重复提交不会产生重复数据**；新载荷里没有的元素会消失（不是合并）。
- 为什么不做事件溯源：测量结果是**产物**，客户端撤销 = 回写上一快照；
  单用户工具不需要操作日志 / 补偿 / 版本冲突解决。
- **必须按会话隔离**：`scale` 是会话级因子、各会话坐标系不同，跨会话连点没有几何意义。
- 未落盘的临时选择只存在于客户端内存，不走这个接口。
- 状态码：`200` 成功（回显存储的标注）；`400` 请求体不是 JSON 对象；
  `404` 会话不存在**或**归属不匹配。

### 1.5 全量点云吸附（`POST /api/sessions/{id}/snap`）

**为什么需要**：客户端拿到的 `points` 是**渲染子集**（默认 6 万 / 全量约 200 万），
等于每约 24 个点只留 1 个、点距被放大约 5 倍。直接用渲染点选点，量出的距离会天然
带上采样误差。所以选点后要拿坐标回来**吸附到全量点云**上（SOR 之后、与 PLY 下载
完全一致的那一份）。

```http
POST /api/sessions/{session_id}/snap?client_id=alice
Content-Type: application/json
X-Auth-Token: <可选>

{ "points": [[x, y, z], ...], "max_distance": 0.05 }
```

| 字段           | 类型              | 说明                                                         |
| -------------- | ----------------- | ------------------------------------------------------------ |
| `points`       | `[[x,y,z], ...]`  | **批量**：框选 / 多选一次请求多个点，一次最多 `500` 个        |
| `max_distance` | number \| `null`  | 超过该距离判为未命中；`null`（或不传）表示不限制              |

```json
{
  "ok": true,
  "hits": 1,
  "elapsed_ms": 3.1,
  "results": [
    { "hit": true, "point": [1.001, -0.02, 0.5], "distance": 0.0013 },
    { "hit": false, "reason": "out_of_range", "distance": 12.7 }
  ]
}
```

- `results` 与请求的 `points` **等长且同序**。
- 未命中区分两种 `reason`：`out_of_range`（超 `max_distance`）与 `empty`（该会话
  没有点云）。**绝不**硬给一个远处的点 —— 宁可明说「这里没点到东西」。
- 实现：`scipy.spatial.cKDTree` + 按会话 **LRU 缓存**（`OMNI3D_SNAP_CACHE`，默认 3），
  详见 `web/snap_index.py`。缓存以「文件大小 + mtime」为签名，PLY 被覆盖会自动重建。
- 资源与实测数字见 [`PERFORMANCE.md`](PERFORMANCE.md) 的吸附一节。

**状态码**：`400` 入参非法（空数组 / 非 3 维 / 含 NaN / 超过 500 点 / `max_distance` 非数字）；
`404` 会话不存在**或归属不匹配**（两者不可区分，避免探测 id 是否存在）。

### 1.6 真实尺度

有**两条路径**，结果都以 `scale`（及 `metric`）体现：

**① AR 位姿驱动（主）** —— 上传时带 `extrinsics`（App 内 AR 扫描）

服务器用「模型预测的相机轨迹」与「AR 米制轨迹」做相似配准，得到 `p_metric = s·R·p_model + T`，
**直接把点云变换到真实米制 + AR 世界坐标系**。重建返回时 `scale` 即已就绪，并写入会话历史。

> 护栏：有效帧数 < 2、轨迹几乎不动、尺度离谱（≤1e-9 或 ≥1e9）时**拒绝对齐**，
> 回退为任意尺度（`scale: null`），由客户端走路径②。

**② 标尺校准（兜底）** —— 事后由用户指定

| 方法 | 路径                    | 说明                                           |
| ---- | ----------------------- | ---------------------------------------------- |
| POST | `/api/tasks/{id}/scale` | 由「两点 + 已知真实距离」反推 `scale` 并持久化 |

```jsonc
// 请求体
{ "point_a": [x, y, z], "point_b": [x, y, z], "real_distance": 1.23 }
// 响应
{ "ok": true, "task_id": "...", "persisted": true,
  "scale": 72.21, "model_distance": 0.0208, "real_distance": 1.23 }
```

`scale = real_distance / ‖point_a − point_b‖`；此后前端测量：`真实距离 = scale × 模型距离`。

> `intrinsics` 目前**不参与**几何求解，仅经校验后以摘要形式写入 `metric.intrinsics`。

---

## 二、App 本地桥（:50687，仅 Android）

让网页访问手机原生能力（AR 位姿、系统文件对话框、华为点云）。
所有响应带 CORS 头。**桌面端运行时装出来的同一批接口对 `/ar/file/*` 与 `/ar/scan/*` 返回“仅 App 内可用”**。

| 方法     | 路径                  | 说明                                                                            |
| -------- | --------------------- | ------------------------------------------------------------------------------- |
| GET      | `/ar/health`          | 存活探针                                                                        |
| GET      | `/ar/status`          | `{ok, ready, tracking, scale}`                                                  |
| GET      | `/ar/pose`            | `{ok, pose:[16 枚 col-major 4×4], tracking}`                                    |
| GET/POST | `/ar/history`         | App 私有目录历史（JSON）读写                                                    |
| POST     | `/ar/file/pick`       | 弹系统文件选择器，返回文件二进制（`X-Filename` 头）                             |
| GET      | `/ar/file/save?name=` | 把请求体写成 PLY 到手机 `Downloads/`                                            |
| POST     | `/ar/scan/start`      | 触发扫描页（App 内）                                                            |
| POST     | `/ar/scan/settings`   | `{width,height}` 设采集分辨率                                                   |
| POST     | `/ar/scan/capture`    | 抓一帧                                                                          |
| POST     | `/ar/scan/finish`     | 完成扫描                                                                        |
| POST     | `/ar/scan/stop`       | 停止连续采集                                                                    |
| POST     | `/ar/scan/reset`      | 清空已抓帧/点云                                                                 |
| GET      | `/ar/scan/status`     | `{available, scanning, finished, frameCount, pointCloudCount, tracking, scale}` |
| GET      | `/ar/scan/data`       | 全部帧的 `poses` + `intrinsics`                                                 |
| GET      | `/ar/scan/frames/{i}` | 第 i 帧 JPEG                                                                    |
| GET      | `/ar/scan/pointcloud` | 华为 SLAM 稀疏点云 PLY                                                          |

---

## 三、状态码速查

| 码  | 场景                                                             |
| --- | ---------------------------------------------------------------- |
| 200 | 成功                                                             |
| 202 | 任务已入队（`POST /api/tasks`）                                  |
| 400 | 参数非法（JSON 解析失败 / 缺字段 / 用户名不合规则 / 两点重合等） |
| 401 | 认证失败（用户名密码错 / nonce 失效 / 未登录 / **令牌过期**）    |
| 404 | 资源不存在、归属不匹配、运行中不可删                             |
| 409 | 用户名已存在                                                     |
| 503 | 模型仍在加载                                                     |

---

## 四、快速上手（curl）

```bash
# 0) 就绪检查
curl -s http://127.0.0.1:50865/health

# 1) 注册（salt → verifier → register；此处用 python 计算摘要）
python - <<'PY'
import hashlib, json, requests, uuid
B = "http://127.0.0.1:50865"
u = "demo_" + uuid.uuid4().hex[:6]; p = "demo-pass-123"
salt = requests.post(f"{B}/api/auth/salt", json={"username": u}).json()["salt"]
vf = hashlib.sha256(salt.encode() + p.encode()).hexdigest()
requests.post(f"{B}/api/auth/register", json={"username": u, "salt": salt, "verifier": vf})
ch = requests.post(f"{B}/api/auth/challenge", json={"username": u}).json()
proof = hashlib.sha256(ch["nonce"].encode() + vf.encode()).hexdigest()
r = requests.post(f"{B}/api/auth/login", json={"username": u, "nonce": ch["nonce"], "proof": proof}).json()
print("token =", r["token"][:16], "..."); print("HEADER =", {"X-Auth-Token": r["token"]})
PY

# 2) 提交视频重建（把 <token> 换成上一步输出）
curl -s -X POST http://127.0.0.1:50865/api/tasks \
  -H "X-Auth-Token: <token>" \
  -F "files=@demo_examples/family/Family.mp4;type=video/mp4" \
  -F "is_video=true" -F "frame_count=8" -F "resolution=512"

# 3) 轮询（<task_id> 为上一步返回）
curl -s "http://127.0.0.1:50865/api/tasks/<task_id>?include_result=true" -H "X-Auth-Token: <token>"

# 4) 我的历史（按 username）
curl -s "http://127.0.0.1:50865/api/history" -H "X-Auth-Token: <token>"
```

> 客户端无需手写这些：浏览器打开 <http://127.0.0.1:50865/> → 选择示例或本地视频 → 开始重建。
