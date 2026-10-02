"""把 web/portal.html 里的 <script type="module"> 抽出来，供 `node --check` 校验语法。

和 `_check_js.py`（面板）同一个套路：不依赖浏览器，快速发现括号 / 引号 / 模板串错误。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
PORTAL = os.path.join(ROOT, "web", "portal.html")
OUT = os.path.join(HERE, "_portal_module.mjs")

with open(PORTAL, encoding="utf-8") as fh:
    html = fh.read()

blocks = re.findall(r'<script type="module">(.*?)</script>', html, flags=re.DOTALL)
if not blocks:
    print("未找到 module 脚本")
    sys.exit(1)

body = max(blocks, key=len)
with open(OUT, "w", encoding="utf-8") as fh:
    fh.write(body)
print(f"已抽出 {len(body)} 字符 -> {OUT}")
sys.exit(0)
