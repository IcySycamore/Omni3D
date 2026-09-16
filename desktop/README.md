# Omni3D 桌面客户端（desktop/）

**PyQt5 + VTK** 实现的桌面客户端——`server` 的一种 client 实现（server 只有一种）。

```
登录窗（右上角 ⚙ → 服务器设置）  →  主窗（重建 / VTK 点云 / 历史）
```

## 关键设计

| 主题       | 做法                                                                 |
| ---------- | -------------------------------------------------------------------- |
| 技术栈     | PyQt5 + VTK（`QVTKRenderWindowInteractor`）                          |
| 服务器地址 | **只在登录窗右上角齿轮（⚙，与最小化/关闭并排）里配置**，主界面不暴露 |
| 认证       | 挑战-应答：明文密码**不上网、不落库**                                |
| 会话       | 客户端 `SessionManager` 持有 `token → username`；服务端为权威映射    |
| 历史       | **按 username 隔离**（登录用户只看得到自己的历史）                   |
| 重建       | 全部由服务器完成，客户端只负责采集提交与呈现                         |

## 认证协议

```
注册：verifier = sha256(salt + password)        ← 客户端算，服务器只存 verifier
登录：nonce    ← 服务器下发
      proof    = sha256(nonce + verifier)       ← 客户端算
      服务器比对 sha256(nonce + verifier_stored)
```

对端实现见 `web/auth_store.py`；客户端实现见 `api_client.py`，两处必须一致。
`nonce` 一次性且 5 分钟过期，可防重放。

## 依赖

```
PyQt5  vtk  requests  numpy
```

## 运行

```powershell
# 1) 先启动服务器（唯一 server）
python web/server.py            # 默认 127.0.0.1:50865

# 2) 启动桌面客户端
python desktop/main.py
```

首次使用：点「注册」创建账号（或先在服务器所在环境用测试脚本建号），
登录后即可在左侧选择 `demo_examples` 示例或本地视频提交重建。

服务器地址改动：**登录窗右上角 ⚙ → 服务器 → 输入地址 → 测试连接 → 保存**。

## 目录

```
desktop/
├── main.py              # 入口：登录窗 ↔ 主窗
├── login_window.py      # 无边框登录窗（⚙ 设置 / — 最小化 / ✕ 关闭）
├── settings_dialog.py   # 服务器设置（齿轮进入）
├── main_window.py       # 主窗：重建 + 历史 + VTK 视图
├── vtk_view.py          # VTK 点云渲染 + 两点拾取
├── api_client.py        # HTTP 客户端 + 认证协议（verifier/proof）
├── session.py           # 客户端 SessionManager（token → username）
├── config.py            # 本地配置（服务器地址持久化到 QSettings）
└── ui/theme.py          # 深色 QSS 主题
```

## 使用

1. **素材**：`选择视频…` 或从下拉选 `demo_examples` 里的示例 → 设抽帧数
2. **开始重建**：进度条 + 阶段实时显示（后台线程，不卡界面）
3. **查看**：左侧「历史记录」双击任意一条即可加载到 VTK 视图
4. **测量**：
   - 勾选「两点测距」→ 在点云上依次点两点 → 显示模型距离
   - 点「标尺校准…」→ 输入这两点的真实距离（米）→ 调用服务端
     `POST /api/tasks/{id}/scale` 反推尺度，之后测量直接显示米
