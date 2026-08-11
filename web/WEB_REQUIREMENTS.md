# Omni3D 网页端需求文档（Web PRD）

> 用途：交给前端 AI 重新设计/实现网页主体。
> **基准实现**：`web/index.html`（星空背景 + 玻璃拟态 + 5 页导航，含录制/拍摄/上传/AR 扫描/3D 查看/历史(含删除)/设置/帮助）。
> 服务器 FastAPI `web/server.py`（端口 50865，单一来源见 server.py SERVER_PORT），已实现删除记录 API。

---

## 1. 产品定位

Omni3D 是一个"从手机视频/图片重建 3D 模型"的服务：

- **网页是产品主体**：承担采集、3D 查看、测量、历史、设置的全部交互。
- **Qt App 是可选壳**（WebView 加载本网页）：只额外提供两样网页拿不到的能力——
  1. **AR 真实尺度位姿**（华为 AREngine，VIO 米制平移）
  2. **历史本地持久化**
- **服务器**：FastAPI（本机 127.0.0.1:50865）+ Fast3R GPU 重建；上传后返回任务 ID，轮询进度，最终拿到点云。

**核心价值主张**：网页给"无 App 用户"完整可用（视频→重建→测量，测量靠标尺校准）；装 App 后自动获得"真实米制尺度"（AR 扫描）。

---

## 2. 整体布局

桌面端：左侧固定侧栏（约 236px）+ 右侧内容区；移动端（<860px）侧栏收起为汉堡菜单 + 遮罩抽屉。

侧栏导航（5 项）：
| 名称 | 说明 |
|---|---|
| 采集 | 主页/默认页 |
| 建模结果 | 3D 查看与测量 |
| 历史记录 | 服务器任务列表 |
| 设置 | 抽帧/相机参数/AR 桥 |
| 帮助 | 全部说明性内容（组件功能与用法） |

侧栏底部固定：AR 连接状态徽标（`◈ AR 位姿 · 已连接/未连接`）。

**设计原则**：说明性文字一律放"帮助"页；功能页只放功能控件。深色玻璃拟态风格（深空背景 + 毛玻璃卡片 + 青/紫渐变强调色，参考当前实现）。

---

## 3. 页面详述

### 3.1 采集页（主页）

分两步卡片：

**① 采集视频**

- 视频预览区（`<video>`）：未选源时显示占位提示（🎥 未选择视频源）。
- 三个按钮：`● 录制`（getUserMedia 摄像头录制，再点停止）、`🗂 本地视频`（文件选择）、`✕`（清除）。
- 录制中显示：红点录制角标 + 计时器（mm:ss）。
- 选中视频后显示元信息条：`🎬 文件名 · 大小 · ✓ 已就绪`。
- **AR 状态行**：`◈ AR 位姿：已连接（App 本地桥）/ 未连接`。
- **AR 扫描按钮**：`📷 AR 扫描（真实尺度）`；未连接 App 时禁用 + 提示"需 App 连接后使用"；已连接显示"点击开始 AR 扫描（在 App 内完成）"。

**② 启动重建**

- `提交重建` 按钮（无输入时禁用）。
- 进度条 + 阶段行（排队中 / 抽帧 / 推理 / 生成点云 + 百分比）。
- **完成/失败后进度条必须清空归零**（隐藏 + 宽度 0，不留残影动画），完成后自动跳转建模结果页。

### 3.2 AR 扫描流程（关键功能）

真实尺度采集（App 内原生相机 + AREngine），网页只做触发与取数：

1. 用户点「AR 扫描」→ `POST /ar/scan/start`（通过桥）→ App 弹出原生扫描页自动采集（约 0.6s/帧）。
2. 网页轮询 `GET /ar/scan/status` 显示状态：`扫描中 · N 帧 · AR 跟踪/等待跟踪`。
3. 扫描结束后取 `GET /ar/scan/data`（frameCount / scale / poses[16×N] / intrinsics[9×N]）+ 逐个 `GET /ar/scan/frames/{i}`（JPEG）。
4. 成功：替换"视频输入"（元信息条显示 `AR 扫描 · N 帧（真实尺度位姿）✓ 已就绪`），提交按钮可用，走"图片模式"提交（见 §6 API）。
5. 失败/超时：提示，按钮恢复。
6. 需要「完成」「放弃」的关闭路径（App 端完成，网页轮询感知）。

### 3.3 建模结果页

- 全屏 three.js 点云查看器（OrbitControls：拖拽旋转、滚轮缩放）。
- 统计条：抽帧视图 / 点数 / 耗时(s)。
- **两点测量**：点模型选第 1 点 → 再点第 2 点 → 显示两点距离（悬浮 chip）。
- **标尺校准条**：`模型距离 = 真实(米) → 校准`；校准后测量按真实尺寸显示。
- 按钮：⬇ 下载 PLY、⤾ 重置视角、✕ 清除测量。
- 顶部返回采集页的入口。

### 3.4 历史记录页

- `GET /api/tasks?limit=20` 服务器任务列表（状态图标 ✅/❌/⏳ + stage + task_id + 视图数）。
- 已完成任务点「查看」→ 拉 `GET /api/tasks/{id}?include_result=true` → 渲染到建模结果页。
- **删除记录**：每条任务带「删除」按钮（运行中不可删）→ 确认后 `DELETE /api/tasks/{id}` → 刷新列表。

### 3.5 设置页（纯设置，无说明文字）

- **🎞 重建·抽帧**：radio `8 帧（快）/ 12 帧（均衡，默认）/ 16 帧（精细）` → 提交时作为视频抽帧数。
- **📷 相机参数**：按钮「从设备读取内外参」→ 读镜头焦距换内参 K + 陀螺仪外参旋转（近似）；结果以标签显示（K ✓ 焦距 / R ✓ 陀螺仪 / AR ✓ 真实位姿 / 未读取·模型自动估计位姿）。
- **◈ AR 桥状态**：显示 `已连接 · AR 位姿可用（真实尺度）` 或 `未连接 · 请通过 App 访问本页`。

### 3.6 帮助页（整合全部说明）

按组件分节（标题 + 列表/段落），包括：
采集视频用法与拍摄建议、AR 扫描流程、启动重建说明、抽帧速度区别、相机参数说明、建模结果操作（旋转/测量/标尺/下载）、测量尺度原理（AR 米制 vs 纯网页模型单位+标尺校准）、历史记录、AR 桥说明（127.0.0.1:50687，已连接=在 App 内）、关于（架构/引擎）。

---

## 4. 关键交互逻辑（JS 必做）

1. **AR 桥检测**（页面加载时 + 3s 后重试）：
   - 方式① 原生注入：`window.omni3dAR` 存在且 `getPose` 是函数 → native 模式。
   - 方式② HTTP 桥：`fetch("http://127.0.0.1:50687/ar/status")` 成功 → http 模式，启动 500ms 轮询 `/ar/pose` 存最新位姿。
   - 更新侧栏徽标 + 采集页状态 + AR 扫描按钮可用性。
2. **提交分支**：
   - 有 AR 扫描帧 → 图片模式：逐帧 `files` + `intrinsics=[K]` + `extrinsics=poses` + `is_video=false`。
   - 否则 → 视频模式：`files`（单个视频）+ `is_video=true` + `frame_count`（设置页值）+ intrinsics/extrinsics（可选）。
   - 附带 `ar_pose`（App 内当前位姿，随任务记录）。
3. **轮询**：`/api/tasks/{id}?include_result=true`，1s 间隔；done → 渲染结果 + 跳转；failed → 提示。
4. **测量尺度**：AR 扫描的 poses 带米制尺度（`scale` 来自桥）→ 测量显示 `m (AR)`；纯网页 → 模型单位，标尺校准后转真实米。

---

## 5. 服务器 API（FastAPI，本机 127.0.0.1:50865，已实现勿改）

| 接口                                      | 说明                                                                                                                                                                                   |
| ----------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /health`                             | `{ready, device}` 模型就绪状态                                                                                                                                                         |
| `POST /api/tasks`                         | multipart：`files[]` + `resolution`(224) + `is_video`("true"/"false" 字符串) + `frame_count`(int) + `intrinsics`(JSON 或 "null") + `extrinsics`(JSON 或 "null") → `{ok, task_id}`(202) |
| `GET /api/tasks/{id}?include_result=true` | `{status: queued/running/done/failed, progress, stage, result:{num_views, num_points, elapsed_s, points[:20000], ply}}`                                                                |
| `GET /api/tasks?limit=20`                 | 任务列表（返回 `{tasks:[...]}` 对象，勿当数组处理）                                                                                                                                    |
| `DELETE /api/tasks/{id}`                  | 删除一条任务记录（运行中不可删，返回 404）→ `{ok:true}` / `{ok:false,error}`                                                                                                           |

- intrinsics：每视图 3×3 K JSON；extrinsics：每视图 4×4 相机位姿 JSON。

---

## 6. AR 桥 API（App 内 127.0.0.1:50687，已实现勿改）

| 接口                                                                   | 说明                                                 |
| ---------------------------------------------------------------------- | ---------------------------------------------------- |
| `GET /ar/status`                                                       | `{ok, ready, tracking, scale}`                       |
| `GET /ar/pose`                                                         | `{ok, pose[16 列主序 4×4], tracking}`                |
| `GET /ar/history` / `POST /ar/history`                                 | App 本地历史持久化（JSON）                           |
| `POST /ar/scan/start`                                                  | 触发 App 打开原生扫描页                              |
| `GET /ar/scan/status`                                                  | `{available, scanning, frameCount, tracking, scale}` |
| `POST /ar/scan/capture` / `POST /ar/scan/stop` / `POST /ar/scan/reset` | 手动抓帧 / 停止 / 清空                               |
| `GET /ar/scan/data`                                                    | `{frameCount, scale, poses[16×N], intrinsics[9×N]}`  |
| `GET /ar/scan/frames/{i}`                                              | 第 i 帧 JPEG                                         |

所有响应带 CORS `Access-Control-Allow-Origin: *`。

---

## 7. 约束与坑（实现时注意）

- **getUserMedia 需 HTTPS 或 localhost**；在 App（QtWebView）内大概率不可用 → 网页录制是"浏览器模式"能力，App 内用 AR 扫描。
- **AR 桥只在 App 内**：独立浏览器打开时 127.0.0.1:50687 不可达 → 显示"未连接"，功能照常（视频/重建/标尺校准），只是没有真实尺度。
- three.js 用 ES Module（importmap + CDN）；exif-js 读 EXIF 焦距。
- 进度条完成后清空（防残留动画）。
- 移动端适配：视频区、按钮行、结果页全屏查看器。
- 深色主题为主（当前为深空玻璃拟态；可自行发挥但保持"科技感 + 深色"）。

---

## 8. 交付物

- 单页应用 `web/index.html`（内联 CSS/JS，模块化 script 可拆分），与现有 `web/server.py` 无缝配合（server 只托管静态文件 + API）。
- 保持现有 DOM id（`video` `recBtn` `pickBtn` `clearBtn` `submitBtn` `progressBar` 等）或一并给出映射，避免破坏服务器端渲染/现有对接。
