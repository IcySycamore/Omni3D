# Omni3D 远程访问：frp 隧道

当**推理服务器在内网**（开发机），而**设备端/用户在外网**时，
用 frp 把服务器的 FastAPI 服务穿透到公网，设备即可远程上传媒体与内外参。

## 拓扑

```
┌─ 设备端（公网/任意）──┐        ┌─ 公网 frps ──┐      ┌─ 内网服务器 ──────────┐
│ 网页上传媒体+内外参    │ ─────► │ :18000       │ ───► │ frpc → FastAPI :8000 │
└──────────────────────┘        └──────────────┘      └──────────────────────┘
```

## 步骤

1. **公网服务器**：运行 `frps -c frps.ini`（开放 7000 与 18000 端口）
2. **推理服务器**：先启动 FastAPI
   ```powershell
   $env:KMP_DUPLICATE_LIB_OK='TRUE'
   D:\anaconda3\envs\Omni3D\python.exe web\server.py
   ```
   再运行 `frpc -c frpc.ini`
3. **设备端/网页用户**：访问 `http://<frps IP>:18000`，即等同访问本机 `http://127.0.0.1:8000`

## UDP 实时流（进阶）

需要低延迟的实时视频/位姿流时启用 frpc 中的 `[omni3d-stream]` UDP 段，
并自定义 UDP 帧协议（帧头 + JSON 元数据 + 视频/位姿负载）。

## 安全提示

- frp 控制通道建议开启 `auth.token` 认证
- 推理接口可加 API Key（`Authorization` 头）防滥用
- 生产环境建议经 HTTPS 反代（Caddy/Nginx）暴露
