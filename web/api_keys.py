"""``X-Api-Key`` 的**解析入口**（服务商 API 用）。

Key 有两个来源，优先级从高到低：

1. **官网签发的 Key**（``portal_store.PortalStore``）——正常途径：
   用户去官网登录 → 买套餐 / 看次数 → 建 Key → 填到面板的「服务 → API Key」；
2. 环境变量 ``OMNI3D_API_KEYS="key=用户名,..."`` —— 运维 / 老部署用的静态 Key，
   面向没有官网（或内网自建）的场合，保留兼容。

解析结果都是**用户名**：历史 / 标注 / 任务都按 ``user:<用户名>`` 归属，
与「用密码登录同一个用户名」看到的是同一份数据。
"""
from __future__ import annotations

import os
import secrets
from typing import Optional

from portal_store import PortalStore

ENV_NAME = "OMNI3D_API_KEYS"

# 官网的 Key 库（与 portal.py 同一个文件：同机部署，控制面/数据面共享一份真相）
_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "omni3d_portal.db",
)
_store: Optional[PortalStore] = None


def store() -> PortalStore:
    """懒加载门户库（导入本模块不该立刻建库 / 连库）。"""
    global _store
    if _store is None:
        _store = PortalStore(_DB_PATH)
    return _store


def parse(raw: str | None) -> dict[str, str]:
    """``"key=alice, key2=bob"`` → ``{key: "alice"}``（忽略空项与坏项）。"""
    out: dict[str, str] = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, _, name = item.partition("=")
        key, name = key.strip(), name.strip()
        if key and name:
            out[key] = name
    return out


def load() -> dict[str, str]:
    """从环境变量读当前配置。"""
    return parse(os.environ.get(ENV_NAME))


def static_username_for(
    api_key: Optional[str], mapping: Optional[dict[str, str]] = None
) -> Optional[str]:
    """静态 Key → 用户名（未配置 / 不匹配 → None）。"""
    if not api_key:
        return None
    table = load() if mapping is None else mapping
    for key, name in table.items():
        # 恒定时间比较：别让响应时间漏出「猜对了几位」
        if secrets.compare_digest(key, api_key):
            return name
    return None


def username_for(api_key: Optional[str]) -> Optional[str]:
    """完整解析：官网签发的 Key 优先，其次环境变量里的静态 Key。"""
    if not api_key:
        return None
    return store().verify_key(api_key) or static_username_for(api_key)
