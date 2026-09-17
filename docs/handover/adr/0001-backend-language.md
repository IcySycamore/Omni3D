# ADR-0001：后端语言与运行时 —— C# / .NET 8+

- 状态：Accepted
- 日期：2026-09-17
- 关联：[`../03-context-glossary.md`](../03-context-glossary.md) §1.2、issue #46

## 背景

参考实现是 Python（FastAPI + torch），功能已完整。重写的触发点是**组织约束**：
团队长期只维护**一门后端语言**，Python 只是当前的临时工具。

必须澄清（避免后人误用这个 ADR）：

- **不是**因为"Python 慢"：HTTP 层不是瓶颈，瓶颈是 GPU 前向（秒级），换语言对它零影响；
- **不是**因为"Python 不安全"：现有风险都在协议与部署（token 进 query、裸 `{error}`、CORS `*`、
  密钥散落 env 与前端 localStorage），换语言不会自动消失。

## 决策

后端统一 **C# / .NET 8+**，使用：

- **ASP.NET Core + Kestrel**（HTTP/WS 一体）；
- **EF Core**（控制面/数据面数据访问）；
- **SignalR**（面板实时通道；对外仍按线协议暴露 WS，见 ADR-0007/0006）；
- 内建 JWT/Bearer 鉴权、限流（`RateLimiter`）、OpenAPI（`Microsoft.AspNetCore.OpenApi`）；
- 发布形态：`dotnet publish -r win-x64 --self-contained` → 单目录/单文件，客户机无需装运行时。

## 代价与后果

- **前端不受益**：panel/官网仍是 TS/Vue（Web 前端只能是 JS/TS），"一门语言"实际是"后端一门 + 前端 TS"；
- **推理层无法搬**（见 ADR-0003），所以"抛弃 Python"的真实含义是"把 Python 压进容器"；
- **生态迁移成本**：Python 侧的 numpy/scipy cKDTree 吸附与几何计算要改用 .NET 库或自写
  （这是工作量最大的一块，见 ADR-0004 的几何迁移）；
- **不要**为了"与前端同语言"改用 Node/TS：CPU 密集的点云处理会拖后腿，且移动端 WebView 无优势。

## 未决

- 是否用 gRPC-Web 让浏览器直连（当前判断：不必要，WS + REST 够）；
- .NET 版本与 LTS 策略（随 ADR-0002 的容器基线一起定）。
