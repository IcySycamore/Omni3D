# 待开发项（Backlog）

> 面向本仓**当前实现**（Python 参考版）的下一批工作。
> 与 `docs/HANDOVER.md` §4 的关系：那份是「换成 C#/.NET 怎么重写」的决策，
> 这份是「在此之前，Python 版先补哪些洞」以及「三服务真正拆开怎么做」。
> ⚠️ **两项有重叠区**（前端拆分 / 三服务拆分）—— 立项前先决定
> 「哪些投入要能带到重写版」，别做了两遍。
>
> 每条都按 **问题 → 做法 → 验收 → 风险/依赖** 写，确保换个 agent 也能接手。

最后更新：2026-10-02

---

## 总览与依赖顺序

| # | 项 | 为什么排在这个位置 | 阻塞关系 |
| - | -- | ------------------ | -------- |
| 1 | **精度基线**（re10k + 自采 GT） | 没有数字，后面所有参数/档位/宣称都悬空 | 不阻塞别人，但**被所有"调参"类工作依赖** |
| 2 | **拆前端**（`panel/index.html` 11k 行） | 现在每次改 UI 都在拆弹（近期事故全是它的症状） | 与 #4 的前端部分重叠 |
| 3 | **持久化任务队列 + 真实支付闭环** | 「能收费的服务」目前只是 README 上的句子 | **必须先于 #4**，否则拆完照样重启丢任务 |
| 4 | **三服务真正拆开（含 MySQL）** | README 说"可异机共享 `data/`"，而共享层是 SQLite —— 与实现矛盾 | 依赖 #3 |

---

## 1. 精度基线：把"准不准"变成一个数

### 问题（事实，不是感觉）

- `docs/evaluation/baseline_report.md` 是**mock 后端**产物（2026-07-16，当时环境不满足 torch 2.3+），
  **不能当重建精度**。真精度至今没有数字。
- 于是下面这些全是**经验值**，没有 GT 支撑：
  `SOR_STD` 的剔除强度、`VIS_CONF_PERCENTILE`、渲染上限 60000、
  分辨率档位（224/512）、视角档位（按显存分档）。
- `residual_ratio` / `local_scale*` **两档完全相同**（对齐阶段算出，与 head 无关）
  → 不能拿来当 A/B 指标，别再误用。

### 做法

1. **跑现成的入口**：`scripts/fast3r_re10k_pose_eval.py`（re10k 位姿/尺度评估）、
   `scripts/robustmvd_eval.py`、`notebooks/re10k_metric.ipynb`。先出**一个**数也行。
2. **补帧数曲线**：2 / 4 / 8 / 16 帧各跑一遍，看误差随视角数的拐点 ——
   这直接决定「视角档位」该怎么给，以及"用户该拍多少张"。
3. **自采米制 GT（本项目的差异化验证）**：用 App 的 AR 扫描拿米制位姿，
   在同一场景量一段已知长度（卷尺/A4 长边 0.297 m），对比
   「AR 位姿对齐」与「标尺校准」两条路的尺度误差。这是 #1 里**只有你能做**的那部分。
4. **把参数与误差对齐**：至少给出 `SOR_STD` 取 1.5/2.0/2.5 时，
   剔除率与「对几何误差的影响」两条线。

### 验收

- `docs/evaluation/` 下有一份**真实数据集**报告，含：数据集/场景、帧数、分辨率、
  指标定义、数值、复现命令。
- README 增加「精度」一节，数字与报告一致（现在是空着的）。
- 报告里每个数字都能由**一条命令**复现（数据从哪来、权重从哪来要写清）。

### 风险 / 依赖

- 需要 GPU（本机 8GB → 档位 `[8]`）+ 权重 `jedyang97/Fast3R_ViT_Large_512`（2.47GB，未入库）。
- re10k 是**位姿**指标，不是点云几何精度；几何精度需要 GT 点云或用自采米制距离代理
  —— **两者要分开写，别混成一个"精度"**。
- ⚠️ 跑之前关掉抢 GPU 的进程（实测 `SlayTheSpire2.exe` / `wallpaper32.exe` 会让前向慢一倍）。

---

## 2. 拆前端：`panel/index.html`（11,144 行）不再长大

### 问题（事实）

- `panel/index.html` = **11,144 行**，CSS + HTML + 内联 `<script type="module">` 全在一个文件，
  **无构建、无模块边界、无类型检查**。
- 469 个测试里，`tests/test_web_client.py` 一个文件就占 **131 个（≈ 28%）**，
  其中大量是**对 HTML/CSS 文本的正则断言**（`assert "..." in index_html`）——
  测试在给文本打补丁，而不是在测行为。
- 近期事故**全部**是它的症状，不是偶然：
  ① `const UI_ICONS` 插在 `META_ICONS` 之前 → 模块求值 `ReferenceError` → **整个初始化中断**
  （图标全空、设置页点不动），而 `node --check` 只查语法；
  ② 字形图标漏配 → painter **静默跳过** → emoji 占位活了好几版；
  ③ hover 语言、选中态语言散在几十处规则里，改一处漏一处。

### 做法（**分四步，每步都能单独合并**，不要大爆炸）

1. **抽 CSS**：`<style>` → `panel/assets/app.css`，用 `<link>` 引入。
   行为完全不变，可一次性验证（页面像素级对比 + 现有测试）。
   ⚠️ `tests/test_web_client.py` 里所有 `_style_block()` 类断言都要改成读这个文件
   （顺便把「剥离注释」的辅助函数抽出来复用）。
2. **抽 JS 为 ES 模块**，**按现有功能边界**切（不要按"文件太大"切）：
   `icons.js`（`_SVG_OPEN` / 四张图标表 + painter）、`theme.js`、`state.js`、
   `api.js`（fetch 拦截器 + 服务商目标）、`viewer.js`（three.js + 拾取/测量）、
   `editor.js`（工具栏/元素视图/撤销）、`settings.js`、`auth.js`、`history.js`。
   ⚠️ **模块间只留显式导入**；现有的隐式全局（`dom.*`、`STATE`）先保留但集中到 `state.js`，
   第二步只搬不改逻辑。
   ⚠️ 图标表的**声明顺序**坑:抽成模块后 "const 没有提升" 会变成 import 顺序问题
   —— 守卫 `test_icon_maps_are_declared_in_dependency_order` 要同步改写成「模块依赖无环」。
3. **引入 Vite + TypeScript（增量）**：先 `allowJs: true` / `strict: false` 跑通构建，
   再按模块收敛类型。收益要有具体抓手：至少让 `dom.*` 与 API 响应有类型。
4. **把"文本断言"迁成"行为断言"**：能导出纯函数的一律导出单测
   （拾取半径、坐标变换 `elementWorldPoint` / `worldPointToElement`、
   `orderRingVertices`、`credLabel` 一类），只对**真的属于展示**的东西保留文本断言。

### 验收

- 单文件行数上限（建议 **≤ 800 行**），加一条守卫测试盯着，超了就红。
- `scripts/_check_js.py` 的抽取锚点改成 `tsc --noEmit`（语法检查升级成类型检查）。
- 「对 index.html 的文本断言」占比从 ~25% 降到 <10%（给个数字，别只写"减少"）。
- **页面行为逐轮不变**：每一步都跑一次真实交互（采集/查看/测量/设置），
  不只跑测试 —— 上一轮的教训就是"测试全绿而页面整片挂掉"。

### 风险 / 依赖

- 与 #4 的前端部分重叠；与 HANDOVER 的 `TS + Vite + Vue 3` 决策部分重叠
  → **先回答"这一步的产物能不能带到重写版"**：能带走的是**组件边界与交互契约**，
  带不走的是具体 DOM/CSS → 所以第 4 步优先做「纯函数与契约」的抽取。
- 编辑器/格式化器会重排 `index.html`（`.md` 表格也会）→ 拆分反而能减少这类噪音。

---

## 3. 持久化任务队列 + 真实支付闭环

### 3a. 任务队列持久化（**这条最急**）

**问题**：`/api/tasks` 是**内存**任务表，重启即丢；
而 `/api/history` 的会话层是 SQLite（持久）→ 两者不一致：
重启后历史里有记录、但任务永远停在"进行中"或直接消失。

**做法**

- 新增 `tasks` 表（`task_id` 主键、`owner`、`state`、`created_at` / `started_at` / `finished_at`、
  `input_meta`、`error`、`metered`），状态机：`queued → running → done | failed`。
- 启动时**恢复**：`running` 的一律标 `failed(restarted)`（或按 `input_meta` 重新入队，
  二选一并写进文档）；`queued` 的重新入队。
- 提交幂等：客户端带 `Idempotency-Key`，重复提交返回同一个 `task_id`。
- **状态流转与结果落库放在同一事务**（否则会出现"有结果没有状态"）。

**验收**：杀掉进程再起，`queued` 任务自动继续、`running` 任务有明确终态；
有测试覆盖「重启恢复」这条路径（不依赖 GPU，用假 processor）。

### 3b. 真实支付闭环

**问题**：`orders` / `ledger` 表都在，扣减逻辑也在，但**没有支付网关**——
README 说"已经是一个能收费的服务"，目前不成立。

**做法**

- 选一个网关（Stripe Checkout 最省事；国内可用支付宝当面付/微信 Native 扫码）。
- `orders` 增加 `status`（created/paid/failed/refunded）、`external_id`、`paid_at`。
- **webhook 幂等**：按 `external_id` 唯一索引去重（网关会重投）。
- 退款/对账：能按 `external_id` 反查并写一条反向 `ledger`。
- `BETA_FREE` 保留为开关，但**必须有一条测试**验证"关掉它之后余额不足会真的 402"。

**验收**：走通一次真实小额支付 → 计划包/余额到账 → 面板能立刻用；
关掉 `BETA_FREE` 后额度耗尽返回 402 且**扣押产物**（现状已有 `arrears` 设计，需接上）。

### 3c. 配额与成本敞口

- 现状：提交时只 `can_start()`、不预扣（可用性优先）→ 真收费后是成本敞口。
- 加：每 owner 并发上限、单次输入上限（已有）、**欠费冻结**、异常用量告警。

---

## 4. 三服务真正拆开（含 MySQL）

### 现状与矛盾（事实）

- 三个进程：`panel/pages.py`(50866，静态) · `panel/portal.py`(50867，控制面) ·
  `panel/server.py`(50865，数据面，需 GPU)。
- **共享一份 SQLite**（`data/*.db`）+ **共享同一套认证实现**（`auth_api.py` 被两侧挂载）。
- README 写「三服务可同机可异机，共享 `data/`」——
  而 SQLite 走网络文件系统是官方不推荐的用法，**这条宣称和实现对不上**。

### 目标形态

```
                 ┌──────────────┐
   浏览器/App ───►│ pages :50866 │  只发静态资源（无状态）
                 └──────────────┘
                        │
        ┌───────────────┴────────────────┐
        │                                │
┌───────────────┐  HTTPS   ┌──────────────────────┐
│ portal :50867 │◄────────►│ api :50865 (GPU)     │
│ 控制面         │ 内部 API │ 数据面                │
│ 账号/计费/发Key│ + mTLS   │ 重建任务/会话/测量    │
│ MySQL(portal) │          │ MySQL(api) + 对象存储 │
└───────────────┘          └──────────────────────┘
```

### 4.1 抽出数据访问层（前置，不可跳）

- 现在 SQL 散在 `panel/session_store.py`、`portal_store.py`、`auth_store.py`、`api_keys.py`。
- 抽成**仓储接口 + 两种实现**（SQLite / MySQL），由 `OMNI3D_DB_URL` 选择。
  先让 SQLite 实现跑通全部测试，再加 MySQL 实现 —— 这样迁移期间两边都能测。
- ⚠️ **禁止跨库 JOIN**：现在"数据面查额度"是**直接读 portal 的库**；
  拆开后在数据面里出现任何指向账号/计费表的 SQL 都是 bug。加一条静态守卫。

### 4.2 MySQL 迁移（逐条要核对，别照搬 SQLite 写法）

| 主题 | SQLite 现状 | MySQL 要求 |
| ---- | ----------- | ---------- |
| 驱动 | 标准库 `sqlite3` | `PyMySQL`（同步）或 `asyncmy`；**连接池**（`Pool`），`utf8mb4` + `InnoDB` |
| 迁移 | `_ensure_columns()` 的 `PRAGMA` + `ALTER` | **换 Alembic**（版本化迁移）。现方案不是迁移，只是补列 |
| 自增主键 | `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGINT AUTO_INCREMENT` |
| 布尔 | `INTEGER 0/1` | `TINYINT(1)`（不要用 `BOOL` 别名） |
| 时间 | TEXT 字符串 | `DATETIME(3)`（毫秒精度，`ledger` 需要）⚠️ 注意时区统一 UTC |
| JSON | TEXT 存 JSON | `JSON` 类型 + 应用层校验 |
| 唯一约束 | 部分索引 | 显式 `UNIQUE KEY`：**至少 `usage.task_id` 的幂等去重必须有**；其余（`orders.external_id`、Key 前缀等）逐列核对 |
| 外键 | 默认不开 | 显式 `FOREIGN KEY` + `ON DELETE` 策略（删账号时套餐/Key/流水怎么办） |

### 4.3 ⚠️ 迁移中**最容易炸**的一点：扣减事务的并发正确性

`PortalStore.charge()` 是三层扣减（计划包 → 用量包 → 余额）＋ 每步写 `ledger`。
在 SQLite 下**单写者**把并发问题掩盖了；换成 MySQL 后必须显式处理：

- 整个 `charge` 在**一个事务**里；
- 读"剩余额度"用 `SELECT ... FOR UPDATE`（或原子 `UPDATE ... SET remaining = remaining - n WHERE remaining >= n` 并检查影响行数）；
- 幂等键（`usage.task_id`）靠 `UNIQUE` 兜底，且**冲突要当成功处理**（重复回调/重试）。
- **必须写一个并发测试**：同账号并发 N 次 `charge`，断言总额不超卖、`ledger` 条数与扣减一一对应。

### 4.4 认证拆开

- 现状：`auth_api.py` 被 portal 与 api **同时挂载** → 两个服务共享同一份账号表。
- 目标：**账号只在 portal**；数据面校验 portal 签发的**短期令牌**：
  - 方案 A（简单）：数据面调用 portal 的 `POST /internal/auth/introspect`（mTLS + 缓存 TTL）。
  - 方案 B（更好）：portal 签 JWT/JWKS，数据面**离线**验签（portal 挂了也不影响数据面）。
- ⚠️ 但 `API Key` 必须仍能**直连数据面**（客户用它调 `/v1`）→ 数据面要么本地持有 Key 的
  sha256（同步机制），要么把 Key 校验也走内部 API。**这条要单独设计，别漏。**
- 守卫：`auth_api` 不能被数据面 `import`；API Key 明文永不出 portal。

### 4.5 产物存储

- 现状：PLY / 缩略图落本地 `data/sessions/` → 拆开后三台机看不到同一个文件。
- 改：S3/MinIO（预签名上传/下载），`sessions` 表只存对象键。
  ⚠️ `/api/history/{id}/ply` 的**归属校验**必须一起搬（现在是 owner 匹配，失败给 404 而文案误导）。

### 4.6 部署与可观测性

- 三份镜像 / `docker-compose` profile；readiness 区分「进程活着」与「模型已加载」。
- 结构化日志 + 每任务一条 trace（`task_id` 串起来）；GPU 显存/队列深度/扣减异常的指标。

### 验收

- 三个服务能**分别部署到三台机**（或三个独立容器），各自连自己的 MySQL；
  portal 停掉时数据面**不崩**（要么降级要么明确报错）。
- 重启 api 不丢任务（依赖 #3a）。
- 并发扣减测试通过（不超卖、`ledger` 对账一致）。
- `docs/ARCHITECTURE.md` / `DEPLOYMENT.md` / `CONTEXT.md` 的"共享一份 SQLite"描述全部更新
  （CONTEXT.md 现在明确写着"两者共享同一套认证实现与同一份 SQLite" —— 拆完这句就是错的）。

### 风险

- 与 HANDOVER 的 **C#/.NET 重写**直接重叠：如果重写已在路上，
  **只做 4.3（扣减正确性）与 4.1（仓储接口）这种能带走的资产**，别把 Python 版的拆分做全。
- 迁移期"双实现"成本高（每改一个查询要改两处）→ 给一个**截止日期**，到点删 SQLite 实现。

---

## 附：低成本高收益（不占主线，随手可做）

| 项 | 为什么 | 成本 |
| -- | ------ | ---- |
| **CI 加一个 test job** | 现在 `.github/workflows/` 只有 `docker-build.yml`，469 个测试只靠本地自觉；而本仓有爱重排文件的格式化器，CI 正好能拦 | 半小时 |
| **补 `LICENSE`** | 根目录没有。任何"对外开放/商用"的说法之前都得先有它 | 10 分钟 |
| **匿名归属改成"每部署 id"以外** | 现在是部署级 `instance_id` → 同一部署下所有未登录用户共享一套历史（自用没问题，对外是隐私问题） | 半天 |
| **帮助/文档里补"拍多少张、怎么绕"** | 重建类产品的第一痛点是采集引导，纯文档版也能救一半 | 半天 |
| **任务进度/耗时** | 长任务无反馈 → 用户以为卡死 | 1 天 |
| **渲染上限与档位的 UI 说明** | 现在 60000 是 `config.MAX_RENDER_POINTS` 的默认值（经验值），用户不知道"看到的不是全部点" | 2 小时 |
