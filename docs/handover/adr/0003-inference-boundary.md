# ADR-0003：推理层边界 —— Python 只做无状态 GPU worker

- 状态：Accepted
- 日期：2026-09-17
- 关联：ADR-0001、ADR-0002、[`../adr/0009-model-code-handover.md`](0009-model-code-handover.md)

## 背景

模型是 **Fast3R（PyTorch）**：自定义 RoPE / attention 掩码 / KV cache / 位姿回归，
代码在参考实现里是 vendored 副本（`fast3r/`、`dust3r/`、`croco/`），还**含我们的本地修改**
（中文 docstring + `blocks.py` 语法修复）。它无法随语言重写一起搬走。

同时，围绕推理的那一圈（HTTP、队列、会话、几何测量、吸附、PLY）**不是 Python 专属**，
在 C# 里完全可写——这两半必须分开决定。

## 决策

- **Python 保留为"无状态 GPU worker"**：只接受「帧 + 参数」→ 返回「点云 / 相机位姿 / 指标」，
  **不碰数据库、不碰账务、不认识用户**；对外零 HTTP 业务能力；
- C# 网关用 **gRPC** 调它（可选协议：**KServe Open Inference Protocol V2**，或极简自定义服务定义）；
- 一个 GPU 一个 worker 进程（隔离显存、可单独重启），由 Api 的任务调度器分配槽位；
- 交付形态：固定 CUDA 基础镜像 + worker 可执行入口，权重走**挂载卷**（`OMNI3D_CHECKPOINT_DIR` 思路保留）；
- **ONNX / TensorRT 导出另立 spike**，不在本次重构主线上（见"未决"）。

## 代价与后果

- **"抛弃 Python"的真实边界**：产品里不再有 Python **业务代码**，但运行时镜像里仍有 Python + torch；
- **多一层序列化**：帧数据要经 gRPC 传给 worker（建议传**引用**而非字节：worker 直接读对象存储/共享卷，
  避免大图在进程间复制）；
- **worker 是单点**：一个 GPU 崩了那台就少一个槽位，需要 Api 侧的槽位健康检查与重排；
- **不做 ONNX 的代价**：永远依赖 CUDA 镜像（部署体积大、版本敏感）。

## 未决

- ONNX 导出的验收标准（若做）：**GPU 逐层数值对齐（阈值 1e-4）** + 端到端点数/位姿对齐，
  达不到就不切；
- worker 与 Api 之间是否传"帧引用"还是"帧字节"（建议前者，取决于是不是同机部署）；
- 多模型（体素 / 网格）接入时 worker 是否需要模型路由（届时可引 KServe V2 的多版本语义）。
