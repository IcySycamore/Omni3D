# ADR-0006：对外作业协议 —— 复用 OGC API-Processes 语义 + NodeODM 形状

- 状态：Accepted
- 日期：2026-09-17
- 关联：[`../01-industry-research.md`](../01-industry-research.md)、[`../02-wire-protocol.md`](../02-wire-protocol.md) §2/§6

## 背景

调研结论（详见 01）：**重建界没有"像 OpenAI 那样"的统一协议**。分层看：

- **模型推理层**有事实标准（OpenAI 兼容 API、KServe Open Inference Protocol V2）；
- **数据/产物层**标准最成熟（glTF、3D Tiles、COPC、LAS/LAZ、PLY、EPT、Potree 八叉树…）；
- **作业服务层没有统一标准**：唯一正式标准 **OGC API - Processes**（与重建无关，是通用异步作业），
  最接近"事实默认"的是 **NodeODM**（但只有 OpenDroneMap 一系实现，不是生态标准）。

同时，参考实现的作业协议是自研的：multipart 提交 + 轮询 + 裸 `{error}` 字符串 +
token 走自定义头（NodeODM 更糟：token 走 query）。

## 决策

**不发明新协议**，两个来源各取所长：

| 取谁                               | 取什么                                                                                                                                                                                 |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **OGC API - Processes (18-062r2)** | **作业语义**：process → job 资源 → 状态/结果/删除/取消；"标准"名头与 OpenAPI/合规套件对齐                                                                                              |
| **NodeODM REST**                   | **词汇与端点形状**：`/info`（能力自述）、`/options`（参数自述）、`init→upload→commit`（预上传）、状态码枚举（QUEUED/RUNNING/FAILED/COMPLETED/CANCELED）、`webhook`、`download/{asset}` |
| **自己收紧**                       | 凭据 **`Authorization: Bearer`**（不用 query token）；错误 **RFC 9457 problem+json + 稳定 code**；写操作 **`Idempotency-Key`**；路径版本 **`/v1`**；追踪 `X-Request-Id`                |

明确**不抄**：OpenAI 的 `chat/completions` 语义（我们不是对话模型，产物是几十 MB 的二进制）；
NodeODM 的 token-in-query 与 `{success: bool, error: string}` 错误体。

## 代价与后果

- **不是 100% 合规某个标准**：我们"形似 OGC 的 job 资源 + 形似 NodeODM 的端点"，
  因此不能对外宣称"符合 OGC API-Processes"（若要宣称，需按其 conformance 类逐条实现，
  代价是引入 GIS 的数据模型，收益低）；
- **自研客户端不能白拿现成 SDK**（NodeODM/OGC 的 SDK 都不会直接可用）——
  缓解：OpenAPI 生成 TS/C# 客户端（见 ADR-0008）；
- **`progress` 语义变更**：旧实现 0–100 整数，新协议 0–1 浮点 → 属破坏性变更，必须写进迁移说明。

## 未决

- 是否真的去拿 OGC 合规认证（**建议不做**，除非有政府/测绘类客户要求）；
- `retry` 端点是否保留（"取消后重建"是否够用）；
- 标注/测量端点是否随 `v1` 改名（现在是 `/api/sessions/{id}/annotations`，见线协议 §9）。
