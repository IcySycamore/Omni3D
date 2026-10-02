"""包结构守卫：`server/` / `panel/` 改名后不能互相踩名。

这里**必须**用 subprocess 真跑一次：`sys.path` 的解析顺序是运行时行为，
静态扫源码看不出来 —— 实测印证过：光看代码会得出「一定会撞名」的错误结论，
真跑才发现「恰好没撞」，而那个「恰好」依赖一个脆弱的插入顺序。
"""
from __future__ import annotations

import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PANEL = os.path.join(_ROOT, "panel")


class TestPanelDoesNotShadowTheServerPackage:
    def test_server_resolves_to_the_top_level_package(self):
        """`panel/` 排在项目根**前面**时，`from server.core import ...` 仍须成功。

        复现 `python panel/server.py` 的处境：Python 会把脚本所在目录（panel/）
        放到 `sys.path` 前排，于是裸名 `server` 有两个候选 —— `panel/server.py`
        与顶层包 `server/`。这里刻意**先**把项目根以绝对路径放进 `sys.path`，
        再让 panel/ 插到它前面：这正好命中 `if PROJECT_ROOT not in sys.path`
        的空档（条件为假 → 一行都不插），于是 `server` 会被解析成
        `panel/server.py`，报 `ImportError: cannot import name 'core'`。

        修法是 `panel/server.py` **无条件**把项目根挪到最前。
        """
        code = (
            "import sys;"
            f"sys.path.insert(0, r'{_ROOT}');"
            f"sys.path.insert(0, r'{_PANEL}');"
            "import panel.server;"
            "print('IMPORT-OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
        )
        assert proc.returncode == 0, (
            "panel/ 遮蔽了顶层包 server —— `from server.core import ...` 命中了自己。\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
        assert "IMPORT-OK" in proc.stdout
