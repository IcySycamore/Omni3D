"""Omni3D 重建核心：配置、几何、点云与推理流水线。

被 ``panel/server.py``（重建 API）、``panel/task_queue.py``（异步任务）以及
``scripts/`` 下的基准脚本共用；里面**没有**任何网络 / 界面 / 进程代码。

（本包原名 ``app/``，与「App = 手机采集端」语义相撞 —— 现在 ``app/`` 就是那台
Android / Qt 采集壳，本包因此改名 ``server/``。）
"""
