"""检查最近几条 commit message 的编码是否正确（不是乱码）。

为什么需要它：PowerShell 显示 git 输出时会把 UTF-8 按 GBK 解，看起来像乱码，
但**显示乱码**与**真的写错了**是两回事。终端折行/编码都会骗人，所以这里
直接拿 bytes 用 UTF-8 解，再检测「中文是否可读」。
"""
from __future__ import annotations

import subprocess
import sys

REPO = r"d:\PROJECT\Omni3D"
# 这几个词只有编码正确时才会出现
PROBES = ["万向轴", "视角操作块", "帮助文档", "设计 token"]


def main() -> int:
    out = subprocess.run(
        ["git", "log", "-3", "--pretty=%h%x09%B%x00"],
        capture_output=True,
        cwd=REPO,
    )
    if out.returncode != 0:
        print("git 失败:", out.stderr.decode("utf-8", "replace"))
        return 1

    raw = out.stdout
    text = raw.decode("utf-8", errors="replace")
    ok = True
    for chunk in text.split("\x00"):
        chunk = chunk.strip()
        if not chunk:
            continue
        head, _, body = chunk.partition("\t")
        print(f"--- {head} ---")
        # 判定 1：能不能找到确定存在的词
        hits = [p for p in PROBES if p in body]
        # 判定 2：有没有典型 mojibake 特征（UTF-8 被当 GBK 解的产物）
        bad = [m for m in ("涓", "鐨", "锛", "閿", "\ufffd") if m in body]
        first = body.splitlines()[0] if body.splitlines() else ""
        print(f"  首行     : {first}")
        print(f"  命中词   : {hits}")
        print(f"  乱码特征 : {bad}")
        if bad:
            ok = False
            print("  => 判定   : 乱码（需要重写 message）")
        else:
            print("  => 判定   : OK")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
