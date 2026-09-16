# 贡献指南（Omni3D）

> 本文件适用于 **Omni3D 本仓库**。
> 仓库内 `fast3r/` 是 vendored 的上游代码，若要向上游贡献，请遵循其各自的流程。

本项目启用了 **分支保护 + PR 审核**，`main` 不接受直接推送。请按下述流程提交改动。

---

## 1. 开发流程

```powershell
# ① 从最新 main 切出功能分支（不要直接在 main 上改）
git checkout main
git pull origin main
git checkout -b feature/简短描述     # 或 fix/xxx、docs/xxx、refactor/xxx

# ② 改动 + 提交（保持原子化）
git add .
git commit -m "feat: 简述做了什么"

# ③ 推送
git push -u origin feature/简短描述
```

④ 在 GitHub 上开 **Pull Request**（base = `main`）。
⑤ 按审核意见在**同一分支**追加提交并推送，PR 会自动更新。
⑥ 审核通过后合并，并删除已合并的分支。

> 直接 `git push origin main` 会被拒绝：
> `remote: error: GH006: Protected branch update failed ... Changes must be made through a pull request.`

## 2. 提交信息约定

格式：`type: 简述`，`type` 取下列之一。

| type       | 用于     |
| ---------- | -------- |
| `feat`     | 新功能   |
| `fix`      | 缺陷修复 |
| `docs`     | 文档     |
| `refactor` | 重构     |
| `chore`    | 杂项     |
| `test`     | 测试     |

例：`feat: 桌面客户端支持两点测距`、`fix: 修正历史记录的 owner 归属`

## 3. 改代码前请先读

| 文档                                           | 内容                             | 何时必须同步更新        |
| ---------------------------------------------- | -------------------------------- | ----------------------- |
| [`CONTEXT.md`](CONTEXT.md)                     | 领域词汇 + **分层硬约束**        | 引入新术语 / 新分层     |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 模块地图、会话层、认证、尺度反推 | 调整模块职责或数据流    |
| [`docs/API.md`](docs/API.md)                   | **接口清单（唯一权威来源）**     | **新增 / 修改任何接口** |

**最重要的硬约束（摘自 `CONTEXT.md`）**：`server 只有一种`——重建 / 尺度 / 历史逻辑
只在服务器实现一次；网页与桌面客户端都只是它的 client，**不得重复实现**。

## 4. 本地运行与自测

完整步骤见 [README「运行」](README.md#-运行)。统一入口 `run.ps1`
（自动选用带依赖的解释器；若报 `No module named 'fastapi'`，说明用了全局 python）：

```powershell
<你的环境>\python.exe -m pip install -r requirements-app.txt   # 一次即可

.\run.ps1 server      # ① 起服务器（模型首次加载需数分钟）
.\run.ps1 desktop     # ② 起桌面客户端：注册/登录 → 提交 demo_examples 里的视频
.\run.ps1 test        # 跑单元 + 集成测试
.\run.ps1 bench       # 重建速度/质量基准（见 docs/PERFORMANCE.md）
```

提交 PR 前请确认：

- `.\run.ps1 test` 通过（新增纯逻辑请补 `tests/` 下的用例）
- `python -m py_compile <改动的 .py>` 通过（不引入语法错误）
- 关键改动有对应验证；**把验证方式/结果写进 PR 描述**（如 family.mp4 → 5 视图 92160 点）
- 未引入新的重复实现（先查 `CONTEXT.md` 的约束）

## 5. 代码风格

- 文档与注释用中文；Python 用 `from __future__ import annotations` + 类型标注
- 分层：`web/`（server，核心逻辑）、`desktop/`（桌面 client）、`app/core/`（server 共享核心）
- **不要修改 `fast3r/` 内的 vendored 代码**；确有必要请在 PR 中单独说明理由
- 生成物不提交：`build/`、`dist/`、`__pycache__/`、`data/`、`demo_outputs/`（见 `.gitignore`）

## 6. Issue

用 GitHub Issues 记录缺陷与需求。描述请包含：**复现步骤 / 期望结果 / 实际结果 / 环境**（OS、GPU、版本）。
