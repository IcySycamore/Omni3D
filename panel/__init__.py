"""Omni3D 面板：页面宿主 + 重建 API + 官网三个进程，外加共用的存储 / 认证层。

为什么这里是**包**，而不是像以前那样「把目录塞进 sys.path 用裸名导入」：

`app/` → `server/` 之后，本目录里的 `server.py` 会与新顶层包 `server` **撞名**。
裸导入 `import server` 究竟命中哪一个取决于 `sys.path` 顺序，而
`from server.core import config` 会先命中 `panel/server.py`，以
`ImportError: cannot import name 'core'` 收场（确定性失败，不是风格问题）。
做成包之后内部引用一律写 `panel.xxx`，与顶层命名空间彻底隔开。

直接跑单个进程仍然可以：``python panel/server.py``
（文件自己会把项目根加进 ``sys.path``，无需先 ``pip install``）。
"""
