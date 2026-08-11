# frp 隧道方案（脱离 adb / 公网访问）

## 架构

```
手机 App（WebView 加载 https://你的域名）
   │  网页内容 ──frp 隧道──► PC FastAPI 服务器（:50685，web/index.html）
   │
   └─ AR 桥 = App 进程内本地回环 http://127.0.0.1:50687（不经过隧道！）
        提供：AR扫描 / 位姿 / 文件选择 / PLY保存 / 历史
        ⚠️ HTTPS 页面 fetch 本地 http 桥 = 混合内容
           → App 已内置自动放行（ARHelper.enableMixedContent）
```

关键点：

- **网页内容**走 frp（可脱离 adb、可公网）。
- **AR 桥**始终是手机本地 `127.0.0.1:50687`，不依赖 PC/adb/隧道。
- HTTPS 页面访问本地 HTTP 桥需要 WebView 放行混合内容——**新版 APK 已自动处理**，无需手动配置。

## 一、frps.toml（服务器端，公网机）

```toml
bindPort = 7000          # frp 控制通道
vhostHTTPPort = 8080     # http 隧道入口（caddy 会反代到这里）
# 若 frps 直接提供 https（需证书）：
# vhostHTTPSPort = 8443
# tls_cert_path = "/etc/frp/server.crt"
# tls_key_path  = "/etc/frp/server.key"
subdomainHost = "example.com"   # 你的域名
auth.method = "token"
auth.token = "改成强随机token"
```

## 二、frpc.toml（PC 端，跑在 Omni3D 服务器旁）

```toml
serverAddr = "你的frp服务器IP或域名"
serverPort = 7000
auth.method = "token"
auth.token = "改成强随机token"

# Omni3D 网页 → PC :50685
[[proxies]]
name = "omni3d-web"
type = "http"
localIP = "127.0.0.1"
localPort = 50685
customDomains = ["omni3d.example.com"]   # 或 subdomain = "omni3d"
```

## 三、自动 HTTPS（推荐：frps + caddy）

frp 的 `vhostHTTPPort` 本身不签证书。在前面套一层 caddy 即可自动签发/续期 Let's Encrypt 证书：

```caddyfile
omni3d.example.com {
    reverse_proxy 127.0.0.1:8080    # 转发到 frps 的 vhostHTTPPort
}
```

- frpc 隧道类型保持 `http`（caddy 负责 TLS）。
- 手机 App 填 `https://omni3d.example.com/`。

> 若你 frps 版本已内置自动证书（如配了 `tls_enable` + 证书自动管理），可直接 `type = "https"` + `customDomains`，App 同样填 https 地址。

## 四、纯 HTTP 模式（局域网 / 不要求 TLS）

无 TLS 时不需要混合内容放行（页面是 http，桥也是 http）：

- frps：`vhostHTTPPort = 8080`，不配证书。
- App 填 `http://frp服务器IP:8080/`（或域名）。
- 局限：明文传输，仅建议可信网络。

## 五、让 App 加载隧道地址（homeUrl）

新版 APK 支持三种入口，优先级从高到低：

```bash
# 方式① Intent extra（推荐首次配置；会自动写入 App 私有目录持久化）
adb shell am start -n com.omni3d.capture/org.qtproject.qt.android.bindings.QtActivity \
    --es homeUrl "https://omni3d.example.com/"

# 方式② 私有目录 home_url.txt（adb push；脱离 adb 后仍生效）
adb push home_url.txt /sdcard/omni3d_home.txt
adb shell run-as com.omni3d.capture sh -c \
  "cp /sdcard/omni3d_home.txt files/home_url.txt"

# 方式③ 默认：http://127.0.0.1:50685/（adb reverse 场景）
```

配置一次后（方式①或②），App 每次启动都会读取持久化的 `home_url.txt`，**之后可以拔掉 USB 脱离 adb**。

## 六、切换回 adb 模式

删除持久化文件即可回退默认：

```bash
adb shell run-as com.omni3d.capture rm files/home_url.txt
adb shell am force-stop com.omni3d.capture
```

## 七、注意事项

- 桥 `127.0.0.1:50687` 的 CORS 已是 `*`，跨源到 https 域名无问题。
- 混合内容放行只对 **App 内 WebView** 生效；用普通手机浏览器打开 https 页面时，
  fetch 本地桥仍会被浏览器阻止（浏览器策略，无法绕过）——请始终用 App 访问。
- 若放行后仍连不上桥，检查 `logcat` 中 `ARHelper: mixed content ALLOWED on WebView` 是否出现。
