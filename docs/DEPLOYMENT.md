# Omni3D 部署指南

四种部署形态：**本机开发 → 局域网 → frp 公网隧道 → 云 GPU 服务器**。按需选用，可平滑升级。

---

## 0. 前置要求

| 项 | 要求 | 说明 |
|---|---|---|
| GPU | NVIDIA，≥12GB 显存（推荐） | Fast3R ViT-Large 推理；无 GPU 可用 CPU（极慢） |
| Python | 3.9+ | conda/pyenv 均可 |
| 依赖 | `pip install fastapi uvicorn numpy opencv-python pillow torch` | fast3r 依赖见 `fast3r/requirements.txt`（vendored 入库） |
| 模型权重 | `jedyang97/Fast3R_ViT_Large_512` | **未入库**，需从 HuggingFace 下载放置 |
| 网络 | 可访问 HuggingFace（下载权重） | 运行期推理不需要外网 |

**模型权重放置**（二选一）：

```bash
# 方式 A：从 HuggingFace 下载到默认路径
git lfs install
git clone https://huggingface.co/jedyang97/Fast3R_ViT_Large_512 jedyang97/Fast3R_ViT_Large_512

# 方式 B：权重放别处，改 app/core/config.py 的 CHECKPOINT_DIR 指向
```

---

## 1. 本机开发部署

```bash
cd Omni3D
python web/server.py
# 默认监听 127.0.0.1:50865（HOST/PORT 环境变量可覆盖）
```

验证：

```bash
curl http://127.0.0.1:50865/health          # {"ready":true,"device":"cuda:0",...}
curl http://127.0.0.1:50865/                # 返回网页
```

- 模型首次加载数分钟，`/health.ready` 变 `true` 后再提交任务。
- 浏览器打开 <http://127.0.0.1:50865/> 即可使用（录制/拍摄需浏览器授权摄像头）。

---

## 2. 局域网部署

```powershell
$env:HOST = "0.0.0.0"
python web/server.py
```

同一局域网设备访问 `http://<处理机内网IP>:50865/`。注意防火墙放行 50865。

> 手机浏览器直接访问即可用（无 AR 真实尺度，靠标尺校准）；要真实尺度需装 App + AR 扫描。

---

## 3. frp 公网隧道部署（当前项目使用的形态）

**架构**：公网域名 → SakuraFrp 节点（frps）→ frpc（处理机）→ `127.0.0.1:50865`。

```
手机/PC ──https──► frp-oil.com:50865 ──► frps(节点) ──隧道──► frpc(处理机) ──► 127.0.0.1:50865
```

### 3.1 处理机侧（frpc）

1. 下载 frp 客户端（`frpc`），或使用 SakuraFrp 启动器
2. 写配置 `frpc.toml`（本机文件不入库，模板见 `docs/frp/README.md`）：

```toml
serverAddr = "你的frps地址"     # 或 SakuraFrp 节点
serverPort = 7000
auth.token = "你的token"

[[proxies]]
name = "omni3d-web"
type = "https"                 # 或 http（取决于 frps 端口）
localIP = "127.0.0.1"
localPort = 50865
customDomains = ["你的域名"]
```

3. 启动：`frpc -c frpc.toml`

### 3.2 公网侧（frps / SakuraFrp）

- **自建 frps**：`vhostHTTPPort`/`vhostHTTPSPort` + TLS 证书；或用 caddy 自动 HTTPS 反代
- **SakuraFrp 面板**：创建 HTTP 隧道（本地端口 50865，绑定域名），http 访问会被强制 302 到 https

### 3.3 手机 App 指向公网地址

```bash
# 首次配置（自动持久化到 home_url.txt，之后可脱离 adb）
adb shell am start -n com.omni3d.capture/org.qtproject.qt.android.bindings.QtActivity \
    --es homeUrl "https://你的域名/"
```

App 内已内置：SakuraFrp 临时证书忽略 + 混合内容放行（https 页面 fetch 本地桥 `127.0.0.1:50687`）。

> 详见 [docs/frp/README.md](frp/README.md)。

---

## 4. 云 GPU 服务器部署（生产）

把处理逻辑整体搬到云主机，直接暴露公网：

```bash
# 云主机（Ubuntu + GPU）
git clone https://github.com/IcySycamore/Omni3D.git
cd Omni3D
# 装依赖 + 放权重（见第 0 节）
# 用 systemd 或 supervisor 常驻
HOST=0.0.0.0 python web/server.py
```

**HTTPS**（三选一）：
- **caddy**（自动 Let's Encrypt）：
  ```caddyfile
  omni3d.example.com {
      reverse_proxy 127.0.0.1:50865
  }
  ```
- nginx + certbot
- 云厂商负载均衡/证书（ALB/CLB）

**安全加固建议**：
- 服务器默认 `127.0.0.1`，公网场景务必配 TLS + 反代
- `task_queue` 为内存缓存（重启丢失），如需持久化自行扩展
- 上传大小限制按需调整（FastAPI 默认）

**App 部署**：云部署后，App 的 `homeUrl` 指向 `https://omni3d.example.com/` 即可（同一套桥逻辑，AR 能力在手机本地不受影响）。

---

## 5. 构建与部署手机 App

### 5.1 环境

| 项 | 版本 |
|---|---|
| Qt | 6.5.3（mingw_64 宿主 + android_arm64_v8a 目标） |
| Android SDK / NDK | NDK 25.x |
| JDK | 17 |
| 华为 AREngine 资产 | 已入库 `qt_app/android/assets/`（`AREngine_Server.apk` + `libhuawei_arengine_ndk.so`） |

### 5.2 构建

```powershell
cd qt_app
.\build_apk.ps1 -Project D:\PROJECT\Omni3D\qt_app -LibTarget omni3d_capture `
  -Abi arm64-v8a -ApkOut Omni3D_Capture-hw-debug.apk
```

> 改 QML/Java/Manifest 后需删除 `build-android/android-build/AndroidManifest.xml` 强制重新部署；改 CMakeLists 需删 `CMakeCache.txt`。

### 5.3 部署（开发期 adb）

```powershell
adb install -r -g Omni3D_Capture-hw-debug.apk
adb reverse tcp:50865 tcp:50865      # 让手机访问 PC 的 127.0.0.1:50865
adb reverse tcp:50685 tcp:50685      # 旧端口（若仍用 50685 版本）
adb forward tcp:15068 tcp:50687      # PC 调试桥
```

### 5.4 脱离 adb（frp 场景）

```bash
adb shell am start -n com.omni3d.capture/org.qtproject.qt.android.bindings.QtActivity \
    --es homeUrl "https://你的域名/"
```

App 自动写入 `home_url.txt` 持久化 → 之后无需 USB。

---

## 6. 故障排查

| 现象 | 排查 |
|---|---|
| `/health.ready=false` | 权重路径不对（`app/core/config.py` CHECKPOINT_DIR）/ 显存不足 / 首次加载中 |
| 网页能开但提交失败 | 看服务器终端 traceback；`task_queue` 是否就绪 |
| App 页面空白 / 一直"加载中" | 证书问题：确认新 APK（含 SSL 忽略）；`logcat -s ARHelper:I` 看 `ssl client installed` |
| App 内 AR 未连接 | 桥 `127.0.0.1:50687` 仅在 App 内；混合内容需放行（新 APK 已内置） |
| frp 502/501 | frpc 未启动 / 后端端口不是 50865 / 域名 Host 不匹配 |
| PLY 下载失败 | App 内看桥日志；PC 端直接下载验证 |

---

## 7. 相关文档

- [架构说明与使用](../README.md)
- [frp 隧道配置](frp/README.md)
- [网页端需求契约](../web/WEB_REQUIREMENTS.md)
