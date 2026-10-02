"""官网门户（控制面）的存储：**签发的 API Key** + 套餐 / 用量。

放在官网而不是服务商里，是因为这两件事是「控制面」的职责：

- 账号在官网注册登录（见 `auth_api.py`，两个服务共用同一份实现）；
- 官网签发 / 吊销 API Key，服务商只负责**校验**（同一个 SQLite 文件，同机部署）；
- 套餐与用量（调用次数 / 点云点数 → 分）也只由官网记账。

计费模型（三层，报价只在这一处）：

1. **按用量**（``METRICS``）：每种计量单位一个 SKU ——
   点云 200 点 = 1 分、体素 1,000 体素 = 1 分、网格 1,000 三角面 = 1 分；
2. **计划**（``PLANS``）：Personal $8.99/月 100 次、Professional $18.99/月 600 次
   —— 一次调用内不分计量，月度重置（``PLAN_PERIOD_DAYS``）；
3. **用量包**（``PACKS``）：预付折扣包（100 万单位，9 折），**不过期**。
   另有一个 **余额**（分）用来接按量计费的零星消费。

一次重建的**扣减顺序**（每个包独立计算）：

   计划包（有剩余次数 → 扣 1 次，本次不再按量计费）
   → 用量包（同计量，先买的先扣，可跨多个包）
   → 余额（剩下的按实际用量折分扣）

⚠️ ``BETA_FREE``（验证阶段）为真时：包照扣（额度是免费发的），但**不扣余额、
额度不够也不拦**；正式收费只改这一个开关。

API Key 的存储约定：
* 明文 **只在创建时返回一次**（客户端自己保存），库里只存 ``sha256(key)``；
* 存 ``prefix``（前 8 位 + ``…``）供列表展示，便于对账；
* ``last_used_at`` 由校验方顺手更新（谁在用一眼能看出来）。
"""
from __future__ import annotations

import hashlib
import math
import os
import secrets
import sqlite3
import threading
import time
from typing import Any, Optional

KEY_PREFIX_BYTES = 4  # 展示用前缀长度（8 个 hex 字符）
KEY_BODY_BYTES = 24  # 随机体，192 bit 熵
KEY_FORMAT = "omni3d_"  # 前缀便于识别（也方便 grep/告警）
MAX_KEYS_PER_USER = 10

# 验证阶段：所有方案免费使用（但**照记用量**，好让计费口径先跑通）
BETA_FREE = True

# ---------- 按用量：计量单位与单价 ----------
# 单价一律写成「多少单位 = 1 分」——口径只有这一处，页面与账单都读它。
METRICS: list[dict[str, Any]] = [
    {
        "id": "points",
        "name": "点云",
        "unit": "点",
        "units_per_cent": 200,
        "count_key": "num_points",
        "desc": "按重建出的点数结算",
    },
    {
        "id": "voxels",
        "name": "体素",
        "unit": "体素",
        "units_per_cent": 1000,
        "count_key": "num_voxels",
        "desc": "按体素分辨率下的体素数结算",
    },
    {
        "id": "mesh",
        "name": "网格",
        "unit": "三角面",
        "units_per_cent": 1000,
        "count_key": "num_triangles",
        "desc": "按重建出的网格三角面结算",
    },
]
DEFAULT_METRIC = "points"

# 旧口径常量（点云）：200 点 = 1 分（$0.01）
POINTS_PER_CENT = 200


def metric_by_id(metric_id: str) -> Optional[dict[str, Any]]:
    for m in METRICS:
        if m["id"] == metric_id:
            return m
    return None


def units_to_cents(metric_id: str, units: int) -> int:
    """用量 → 分（向上取整：不足一个单位价也按 1 分算）。"""
    m = metric_by_id(metric_id) or metric_by_id(DEFAULT_METRIC)
    n = max(0, int(units or 0))
    if n <= 0:
        return 0
    per = int(m["units_per_cent"])
    return (n + per - 1) // per


def points_to_cents(points: int) -> int:
    """点云专用口径（= ``units_to_cents("points", points)``）。"""
    return units_to_cents(DEFAULT_METRIC, points)


# ---------- 用量包（预付折扣包，**不过期**）----------
PACK_UNITS = 1_000_000  # 每种计量一个包：100 万单位
PACK_DISCOUNT = 0.9  # 预付 9 折；改这一行就改全部包的价


def _pack_skus() -> list[dict[str, Any]]:
    skus: list[dict[str, Any]] = []
    for m in METRICS:
        list_cents = units_to_cents(m["id"], PACK_UNITS)
        skus.append(
            {
                "id": f"pack_{m['id']}",
                "metric": m["id"],
                "name": f"{m['name']}包",
                "units": PACK_UNITS,
                "unit": m["unit"],
                "list_cents": list_cents,
                "list_amount": round(list_cents / 100, 2),
                "price": round(list_cents / 100 * PACK_DISCOUNT, 2),
            }
        )
    return skus


PACKS: list[dict[str, Any]] = _pack_skus()

# 余额充值档位（分）：$5 / $10 / $20 / $50
TOPUP_TIERS_CENTS = [500, 1000, 2000, 5000]


def pack_by_id(pack_id: str) -> Optional[dict[str, Any]]:
    for p in PACKS:
        if p["id"] == pack_id:
            return p
    return None


# ---------- 按计划：月度计划包 ----------
PLAN_PERIOD_DAYS = 30  # 计划包的重置周期

# ``calls`` = 这个计划包含多少次调用（每次调用不分计量，整包扣 1 次）
PLANS: list[dict[str, Any]] = [
    {
        "id": "personal",
        "name": "Personal",
        "price": 8.99,
        "period": "月",
        "period_days": PLAN_PERIOD_DAYS,
        "calls": 100,
        "desc": "个人 / 小团队，每月 100 次调用",
    },
    {
        "id": "professional",
        "name": "Professional",
        "price": 18.99,
        "period": "月",
        "period_days": PLAN_PERIOD_DAYS,
        "calls": 600,
        "desc": "专业 / 批量，每月 600 次调用",
    },
]
# 默认方案：不买计划包，纯粹按量（消耗用量包 / 余额）
DEFAULT_PLAN = "points"


def plan_supported(plan_id: str) -> bool:
    return plan_by_id(plan_id) is not None


def plan_by_id(plan_id: str) -> Optional[dict[str, Any]]:
    for p in PLANS:
        if p["id"] == plan_id:
            return p
    return None


def fmt_amount(cents: int) -> float:
    """分 → 美元（保留 2 位；允许负数 = 余额减少）。"""
    return round(int(cents or 0) / 100.0, 2)


def plan_days_left(expires_at: Optional[float]) -> Optional[int]:
    """距离计划包重置还有几天（向上取整）；不过期 → None。"""
    if not expires_at:
        return None
    return max(0, int(math.ceil((float(expires_at) - time.time()) / 86400.0)))


# 扣减来源的中文说法（流水 / 用量行共用）
COVER_LABEL = {
    "plan": "计划包抵扣",
    "packs": "用量包抵扣",
    "balance": "余额扣费",
    "packs+balance": "用量包 + 余额",
    "free": "验证阶段免费",
    "none": "未计费",
}


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_key() -> tuple[str, str, str]:
    """生成 (明文 Key, hash, 展示前缀)。明文只在这一次调用里存在。"""
    body = secrets.token_hex(KEY_BODY_BYTES)
    raw = KEY_FORMAT + body
    return raw, hash_key(raw), raw[: len(KEY_FORMAT) + 2 * KEY_PREFIX_BYTES] + "…"


class PortalStore:
    """官网的 API Key 与套餐/次数（SQLite，线程安全）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    # ---- 基础设施 ----
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_id       TEXT PRIMARY KEY,
                    key_hash     TEXT NOT NULL UNIQUE,
                    prefix       TEXT NOT NULL,
                    label        TEXT NOT NULL DEFAULT '',
                    username     TEXT NOT NULL,
                    created_at   REAL NOT NULL,
                    last_used_at REAL,
                    revoked      INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_keys_user ON api_keys(username)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    username        TEXT PRIMARY KEY,
                    plan            TEXT NOT NULL DEFAULT 'points',  -- 当前计划包（points = 无计划）
                    credits         INTEGER NOT NULL DEFAULT 0,      -- 计划包剩余次数
                    balance_cents   INTEGER NOT NULL DEFAULT 0,      -- 余额（分）
                    display_name    TEXT NOT NULL DEFAULT '',
                    email           TEXT NOT NULL DEFAULT '',
                    plan_started_at REAL,
                    plan_expires_at REAL,                            -- 计划包下次重置时刻
                    created_at      REAL NOT NULL,
                    updated_at      REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id     TEXT PRIMARY KEY,
                    username     TEXT NOT NULL,
                    plan         TEXT NOT NULL,   -- 计划 id / 用量包 sku / 'topup'
                    kind         TEXT NOT NULL DEFAULT 'plan',
                    metric       TEXT NOT NULL DEFAULT '',
                    quantity     INTEGER NOT NULL DEFAULT 0,
                    credits      INTEGER NOT NULL,   -- 本次开通的次数 / 单位数
                    amount       REAL NOT NULL,      -- 实付
                    list_amount  REAL NOT NULL DEFAULT 0,  -- 标价（验证阶段实付 0）
                    note         TEXT NOT NULL DEFAULT '',
                    created_at   REAL NOT NULL
                )
                """
            )
            # 用量：一次重建 = 一行（计量单位 + 实际用量 → 分）
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage (
                    task_id    TEXT PRIMARY KEY,
                    username   TEXT NOT NULL,
                    metric     TEXT NOT NULL DEFAULT 'points',
                    units      INTEGER NOT NULL DEFAULT 0,   -- 实际用量（点 / 体素 / 三角面）
                    points     INTEGER NOT NULL DEFAULT 0,   -- 点云专用（兼容旧口径）
                    cents      INTEGER NOT NULL DEFAULT 0,   -- 标价（分）
                    paid_cents INTEGER NOT NULL DEFAULT 0,   -- 实际从余额扣的分
                    covered_by TEXT NOT NULL DEFAULT '',     -- plan / packs / balance / free
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_user ON usage(username)"
            )
            # 用量包：一包一行，**独立计算**；expires_at 为空 = 不过期（∞）
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS packs (
                    pack_id    TEXT PRIMARY KEY,
                    username   TEXT NOT NULL,
                    sku        TEXT NOT NULL,
                    metric     TEXT NOT NULL,
                    total      INTEGER NOT NULL,
                    remaining  INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_packs_user ON packs(username)"
            )
            # 流水：一切金额变动（充值 / 购买 / 按量扣费 / 计划重置）
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger (
                    entry_id      TEXT PRIMARY KEY,
                    username      TEXT NOT NULL,
                    kind          TEXT NOT NULL,
                    amount_cents  INTEGER NOT NULL DEFAULT 0,  -- 对余额的影响（+ 收 / - 支）
                    list_cents    INTEGER NOT NULL DEFAULT 0,  -- 标价（分）
                    metric        TEXT NOT NULL DEFAULT '',
                    quantity      INTEGER NOT NULL DEFAULT 0,
                    balance_after INTEGER NOT NULL DEFAULT 0,
                    note          TEXT NOT NULL DEFAULT '',
                    created_at    REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(username)"
            )
            # 老库升级：上一版只有 accounts / orders / usage 三张表的旧列
            _ensure_columns(
                conn,
                "accounts",
                {
                    "balance_cents": "INTEGER NOT NULL DEFAULT 0",
                    "display_name": "TEXT NOT NULL DEFAULT ''",
                    "email": "TEXT NOT NULL DEFAULT ''",
                    "plan_started_at": "REAL",
                    "plan_expires_at": "REAL",
                },
            )
            _ensure_columns(
                conn,
                "orders",
                {
                    "list_amount": "REAL NOT NULL DEFAULT 0",
                    "note": "TEXT NOT NULL DEFAULT ''",
                    "kind": "TEXT NOT NULL DEFAULT 'plan'",
                    "metric": "TEXT NOT NULL DEFAULT ''",
                    "quantity": "INTEGER NOT NULL DEFAULT 0",
                },
            )
            _ensure_columns(
                conn,
                "usage",
                {
                    "metric": "TEXT NOT NULL DEFAULT 'points'",
                    "units": "INTEGER NOT NULL DEFAULT 0",
                    "paid_cents": "INTEGER NOT NULL DEFAULT 0",
                    "covered_by": "TEXT NOT NULL DEFAULT ''",
                },
            )

    # ---- 账号 / 余额 / 计划包 ----
    def _ledger(
        self,
        conn: sqlite3.Connection,
        username: str,
        *,
        kind: str,
        amount_cents: int = 0,
        list_cents: int = 0,
        metric: str = "",
        quantity: int = 0,
        note: str = "",
    ) -> None:
        """写一行流水（余额快照取当前值；调用方要先把余额改完）。"""
        row = conn.execute(
            "SELECT balance_cents FROM accounts WHERE username = ?", (username,)
        ).fetchone()
        balance_after = int(row["balance_cents"]) if row else 0
        conn.execute(
            """INSERT INTO ledger
               (entry_id, username, kind, amount_cents, list_cents, metric,
                quantity, balance_after, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "l_" + uuid_hex(),
                username,
                kind,
                int(amount_cents),
                int(list_cents),
                metric,
                int(quantity),
                balance_after,
                note,
                time.time(),
            ),
        )

    def _refresh_plan(self, conn: sqlite3.Connection, username: str) -> None:
        """计划包到点就**重置**：额度回到本月总量，并记一笔流水。

        调用方必须已经持有 ``self._lock`` 与 ``conn``。
        """
        row = conn.execute(
            "SELECT plan, credits, plan_expires_at FROM accounts WHERE username = ?",
            (username,),
        ).fetchone()
        if row is None or not row["plan_expires_at"]:
            return
        plan = plan_by_id(row["plan"])
        if plan is None:
            return
        now = time.time()
        expires = float(row["plan_expires_at"])
        if now < expires:
            return
        period = float(plan.get("period_days") or PLAN_PERIOD_DAYS) * 86400.0
        while expires <= now:
            expires += period
        total = int(plan.get("calls") or 0)
        conn.execute(
            """UPDATE accounts SET credits = ?, plan_expires_at = ?, updated_at = ?
               WHERE username = ?""",
            (total, expires, now, username),
        )
        self._ledger(
            conn,
            username,
            kind="grant",
            metric="calls",
            quantity=total,
            note=f"计划包重置（{plan['name']}）：额度回到 {total} 次",
        )

    def account(self, username: str) -> dict[str, Any]:
        """账号的计费信息（不存在则返回默认值，不改库）。"""
        with self._lock, self._connect() as conn:
            self._refresh_plan(conn, username)
            row = conn.execute(
                "SELECT * FROM accounts WHERE username = ?", (username,)
            ).fetchone()
        if row is None:
            return {
                "username": username,
                "plan": DEFAULT_PLAN,
                "credits": 0,
                "balance_cents": 0,
                "balance_usd": 0.0,
                "display_name": "",
                "email": "",
                "plan_expires_at": None,
                "plan_days_left": None,
                "exists": False,
            }
        return {
            "username": row["username"],
            "plan": row["plan"],
            "credits": int(row["credits"]),
            "balance_cents": int(row["balance_cents"]),
            "balance_usd": fmt_amount(row["balance_cents"]),
            "display_name": row["display_name"] or "",
            "email": row["email"] or "",
            "plan_started_at": row["plan_started_at"],
            "plan_expires_at": row["plan_expires_at"],
            "plan_days_left": plan_days_left(row["plan_expires_at"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "exists": True,
        }

    def ensure_account(self, username: str) -> dict[str, Any]:
        """建账号（幂等）。验证阶段没有预存额度：用多少记多少。"""
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO accounts
                   (username, plan, credits, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?)""",
                (username, DEFAULT_PLAN, now, now),
            )
        return self.account(username)

    def purchase(self, username: str, plan_id: str) -> dict[str, Any]:
        """开通计划包（``personal`` / ``professional``），或切回按量（``points``）。

        * 计划包：加本月调用次数 + 设置重置时间（验证阶段实付 0，标价照记）；
        * ``points``：只切回按量计费，不动次数、不产生订单。
        """
        self.ensure_account(username)
        now = time.time()
        if plan_id == DEFAULT_PLAN:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "UPDATE accounts SET plan = ?, updated_at = ? WHERE username = ?",
                    (plan_id, now, username),
                )
            return self.account(username)
        plan = plan_by_id(plan_id)
        if plan is None:
            raise ValueError("没有这个方案")
        calls = int(plan.get("calls") or 0)
        list_cents = int(round(float(plan.get("price") or 0.0) * 100))
        paid_cents = 0 if BETA_FREE else list_cents
        period = float(plan.get("period_days") or PLAN_PERIOD_DAYS) * 86400.0
        order_id = "o_" + uuid_hex()
        note = "验证阶段免费" if BETA_FREE else ""
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE accounts SET plan = ?, credits = credits + ?,
                   plan_started_at = ?, plan_expires_at = ?, updated_at = ?
                   WHERE username = ?""",
                (plan["id"], calls, now, now + period, now, username),
            )
            conn.execute(
                """INSERT INTO orders
                   (order_id, username, plan, kind, metric, quantity, credits,
                    amount, list_amount, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order_id,
                    username,
                    plan["id"],
                    "plan",
                    "calls",
                    calls,
                    calls,
                    fmt_amount(paid_cents),
                    fmt_amount(list_cents),
                    note,
                    now,
                ),
            )
            self._ledger(
                conn,
                username,
                kind="buy_plan",
                list_cents=list_cents,
                metric="calls",
                quantity=calls,
                note=f"购买计划包 {plan['name']}（{calls} 次 / {plan.get('period_days') or PLAN_PERIOD_DAYS} 天）",
            )
        return {"order_id": order_id, **self.account(username)}

    def buy_pack(self, username: str, pack_id: str) -> dict[str, Any]:
        """购买**用量包**：预付折扣包，不过期，按包独立计算。"""
        sku = pack_by_id(pack_id)
        if sku is None:
            raise ValueError("没有这个用量包")
        self.ensure_account(username)
        now = time.time()
        list_cents = int(sku["list_cents"])
        paid_cents = 0 if BETA_FREE else int(round(float(sku["price"]) * 100))
        row_id = "p_" + uuid_hex()
        order_id = "o_" + uuid_hex()
        note = "验证阶段免费" if BETA_FREE else ""
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO packs
                   (pack_id, username, sku, metric, total, remaining, created_at,
                    expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    row_id,
                    username,
                    sku["id"],
                    sku["metric"],
                    int(sku["units"]),
                    int(sku["units"]),
                    now,
                ),
            )
            conn.execute(
                """INSERT INTO orders
                   (order_id, username, plan, kind, metric, quantity, credits,
                    amount, list_amount, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order_id,
                    username,
                    sku["id"],
                    "pack",
                    sku["metric"],
                    int(sku["units"]),
                    int(sku["units"]),
                    fmt_amount(paid_cents),
                    fmt_amount(list_cents),
                    note,
                    now,
                ),
            )
            self._ledger(
                conn,
                username,
                kind="buy_pack",
                list_cents=list_cents,
                metric=sku["metric"],
                quantity=int(sku["units"]),
                note=f"购买{sku['name']}（{sku['units']:,} {sku['unit']} · 不过期）",
            )
        return {"order_id": order_id, "pack_id": row_id, **self.account(username)}

    def topup(self, username: str, amount_cents: int) -> dict[str, Any]:
        """余额充值（固定档位；演示环境不接支付网关）。"""
        amount = int(amount_cents or 0)
        if amount not in TOPUP_TIERS_CENTS:
            tiers = " / ".join(f"${c // 100}" for c in TOPUP_TIERS_CENTS)
            raise ValueError(f"只支持固定充值档位：{tiers}")
        self.ensure_account(username)
        now = time.time()
        order_id = "o_" + uuid_hex()
        note = "验证阶段免费" if BETA_FREE else ""
        paid_cents = 0 if BETA_FREE else amount
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE accounts SET balance_cents = balance_cents + ?,
                   updated_at = ? WHERE username = ?""",
                (amount, now, username),
            )
            conn.execute(
                """INSERT INTO orders
                   (order_id, username, plan, kind, metric, quantity, credits,
                    amount, list_amount, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
                (
                    order_id,
                    username,
                    "topup",
                    "topup",
                    "",
                    0,
                    fmt_amount(paid_cents),
                    fmt_amount(amount),
                    note,
                    now,
                ),
            )
            self._ledger(
                conn,
                username,
                kind="topup",
                amount_cents=amount,
                list_cents=amount,
                note=f"余额充值 ${amount // 100}"
                + (f"（{note}）" if note else ""),
            )
        return {"order_id": order_id, **self.account(username)}

    def set_profile(
        self, username: str, display_name: str = "", email: str = ""
    ) -> dict[str, Any]:
        """改显示名 / 邮箱（空串 = 清空）。"""
        self.ensure_account(username)
        name = (display_name or "").strip()[:40]
        mail = (email or "").strip()[:120]
        if mail and ("@" not in mail or "." not in mail.rsplit("@", 1)[-1]):
            raise ValueError("邮箱格式不对")
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE accounts SET display_name = ?, email = ?, updated_at = ?
                   WHERE username = ?""",
                (name, mail, time.time(), username),
            )
        return self.account(username)

    def orders(self, username: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE username = ? ORDER BY created_at DESC",
                (username,),
            ).fetchall()
        return [dict(r) for r in rows]

    def packs(self, username: str) -> list[dict[str, Any]]:
        """用量包列表（每包独立；``infinite`` = 不过期）。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM packs WHERE username = ? ORDER BY created_at ASC",
                (username,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            m = metric_by_id(r["metric"]) or {}
            total = int(r["total"])
            remaining = int(r["remaining"])
            out.append(
                {
                    "pack_id": r["pack_id"],
                    "sku": r["sku"],
                    "kind": "pack",
                    "metric": r["metric"],
                    "metric_name": m.get("name", r["metric"]),
                    "unit": m.get("unit", ""),
                    "name": f"{m.get('name', r['metric'])}包",
                    "total": total,
                    "remaining": remaining,
                    "used": max(0, total - remaining),
                    "remaining_usd": fmt_amount(units_to_cents(r["metric"], remaining)),
                    "created_at": r["created_at"],
                    "expires_at": r["expires_at"],
                    "infinite": r["expires_at"] is None,
                }
            )
        return out

    def plan_pack(self, username: str) -> Optional[dict[str, Any]]:
        """计划包视图（没买计划 → None）。"""
        acct = self.account(username)
        plan = plan_by_id(acct["plan"])
        if plan is None:
            return None
        total = int(plan.get("calls") or 0)
        return {
            "kind": "plan",
            "plan": plan["id"],
            "name": plan["name"],
            "metric": "calls",
            "unit": "次",
            "total": total,
            "remaining": acct["credits"],
            "used": max(0, total - acct["credits"]),
            "expires_at": acct["plan_expires_at"],
            "days_left": acct["plan_days_left"],
            "period_days": int(plan.get("period_days") or PLAN_PERIOD_DAYS),
            "price": plan["price"],
            "infinite": False,
        }

    def ledger(self, username: str, limit: int = 100) -> list[dict[str, Any]]:
        """流水（倒序）：充值 / 购买 / 按量扣费 / 计划重置。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM ledger WHERE username = ?
                   ORDER BY created_at DESC, rowid DESC LIMIT ?""",
                (username, max(1, int(limit))),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["amount_usd"] = fmt_amount(d["amount_cents"])
            d["list_usd"] = fmt_amount(d["list_cents"])
            d["cover_label"] = COVER_LABEL.get(d["kind"], "")
            out.append(d)
        return out

    def billing_enforced(self) -> bool:
        """是否需要拦（额度不足时 402）。验证阶段一律不拦。"""
        return not BETA_FREE

    def can_start(self, username: str) -> bool:
        """还有额度吗？计划包 / 用量包 / 余额任一有剩余即可。"""
        if not self.billing_enforced():
            return True
        acct = self.account(username)
        if plan_by_id(acct["plan"]) and int(acct["credits"]) > 0:
            return True
        if any(int(p["remaining"]) > 0 for p in self.packs(username)):
            return True
        return int(acct["balance_cents"]) > 0

    def charge(
        self, username: str, metric: str = DEFAULT_METRIC, units: int = 0
    ) -> dict[str, Any]:
        """按「计划包 → 用量包 → 余额」扣一次用量，并记流水。

        * 计划包：有剩余次数就**整包扣 1 次**，本次不再按量计费；
        * 用量包：同计量的包轮流扣（先买的先扣，可跨多个包）；
        * 余额：剩下的用量按单价折分扣（验证阶段不扣）。

        返回 ``{"metric","units","list_cents","paid_cents","covered_by","sources"}``。
        """
        metric_id = metric if metric_by_id(metric) else DEFAULT_METRIC
        n = max(0, int(units or 0))
        list_cents = units_to_cents(metric_id, n)
        now = time.time()
        self.ensure_account(username)
        sources: list[dict[str, Any]] = []
        with self._lock, self._connect() as conn:
            self._refresh_plan(conn, username)
            row = conn.execute(
                "SELECT plan, credits, balance_cents FROM accounts WHERE username = ?",
                (username,),
            ).fetchone()
            plan = plan_by_id(row["plan"]) if row else None
            paid_cents = 0
            remaining = n
            plan_used = bool(plan is not None and int(row["credits"]) > 0)
            if plan_used:
                conn.execute(
                    "UPDATE accounts SET credits = credits - 1, updated_at = ? WHERE username = ?",
                    (now, username),
                )
                sources.append({"kind": "plan", "name": plan["name"], "calls": 1})
                remaining = 0
            else:
                rows = conn.execute(
                    """SELECT * FROM packs WHERE username = ? AND metric = ?
                       AND remaining > 0 ORDER BY created_at ASC, rowid ASC""",
                    (username, metric_id),
                ).fetchall()
                for pk in rows:
                    if remaining <= 0:
                        break
                    take = min(remaining, int(pk["remaining"]))
                    conn.execute(
                        "UPDATE packs SET remaining = remaining - ? WHERE pack_id = ?",
                        (take, pk["pack_id"]),
                    )
                    sources.append(
                        {
                            "kind": "pack",
                            "pack_id": pk["pack_id"],
                            "units": take,
                            "left": int(pk["remaining"]) - take,
                        }
                    )
                    remaining -= take
                if remaining < n:
                    sources.append(
                        {"kind": "note", "text": "用量包抵扣", "units": n - remaining}
                    )
                need = units_to_cents(metric_id, remaining)
                if need:
                    balance = int(row["balance_cents"]) if row else 0
                    if BETA_FREE:
                        sources.append(
                            {"kind": "balance", "cents": need, "paid": 0, "note": "验证阶段免费"}
                        )
                    else:
                        paid_cents = min(need, balance)
                        if paid_cents:
                            conn.execute(
                                """UPDATE accounts SET balance_cents = balance_cents - ?,
                                   updated_at = ? WHERE username = ?""",
                                (paid_cents, now, username),
                            )
                        sources.append(
                            {
                                "kind": "balance",
                                "cents": need,
                                "paid": paid_cents,
                                "shortfall": max(0, need - paid_cents),
                            }
                        )
            if plan_used:
                covered = "plan"
            else:
                parts = []
                if any(s["kind"] == "pack" for s in sources):
                    parts.append("packs")
                if paid_cents:
                    parts.append("balance")
                if not parts:
                    parts.append("free" if BETA_FREE else "none")
                covered = "+".join(parts)
            m = metric_by_id(metric_id) or {}
            self._ledger(
                conn,
                username,
                kind="usage",
                amount_cents=-paid_cents,
                list_cents=list_cents,
                metric=metric_id,
                quantity=n,
                note=f"{m.get('name', metric_id)} {n:,} {m.get('unit', '')}"
                f" · {COVER_LABEL.get(covered, covered)}",
            )
        return {
            "metric": metric_id,
            "units": n,
            "list_cents": list_cents,
            "paid_cents": paid_cents,
            "covered_by": covered,
            "sources": sources,
        }

    # ---- 用量（一次重建 = 一行）----
    def record_usage(
        self,
        username: str,
        task_id: str,
        units: int = 0,
        metric: str = DEFAULT_METRIC,
    ) -> dict[str, Any]:
        """记一次用量（**幂等**）：入库 → 扣减（计划包 → 用量包 → 余额）→ 记流水。

        ``units`` 是本次的实际用量（点 / 体素 / 三角面）——默认按点云口径。
        """
        metric_id = metric if metric_by_id(metric) else DEFAULT_METRIC
        n = max(0, int(units or 0))
        cents = units_to_cents(metric_id, n)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO usage
                   (task_id, username, metric, units, points, cents, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    username,
                    metric_id,
                    n,
                    n if metric_id == DEFAULT_METRIC else 0,
                    cents,
                    time.time(),
                ),
            )
            first_time = cur.rowcount > 0
        if first_time:
            charge = self.charge(username, metric_id, n)
            with self._lock, self._connect() as conn:
                conn.execute(
                    "UPDATE usage SET paid_cents = ?, covered_by = ? WHERE task_id = ?",
                    (charge["paid_cents"], charge["covered_by"], task_id),
                )
        return self.usage_summary(username)

    def usage_summary(self, username: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS calls, COALESCE(SUM(points), 0) AS points,
                          COALESCE(SUM(cents), 0) AS cents,
                          COALESCE(SUM(paid_cents), 0) AS paid_cents
                   FROM usage WHERE username = ?""",
                (username,),
            ).fetchone()
            rows = conn.execute(
                """SELECT metric, COUNT(*) AS calls, COALESCE(SUM(units), 0) AS units,
                          COALESCE(SUM(cents), 0) AS cents
                   FROM usage WHERE username = ? GROUP BY metric""",
                (username,),
            ).fetchall()
        return {
            "calls": int(row["calls"]),
            "points": int(row["points"]),
            "cents": int(row["cents"]),
            "usd": fmt_amount(row["cents"]),
            "paid_cents": int(row["paid_cents"]),
            "paid_usd": fmt_amount(row["paid_cents"]),
            "by_metric": {
                r["metric"]: {
                    "calls": int(r["calls"]),
                    "units": int(r["units"]),
                    "cents": int(r["cents"]),
                    "usd": fmt_amount(r["cents"]),
                }
                for r in rows
            },
        }

    # ---- API Key ----
    def create_key(self, username: str, label: str = "") -> dict[str, Any]:
        """新建 Key。返回里带 ``key`` —— **明文只此一次**。"""
        active = self.list_keys(username)
        if len([k for k in active if not k["revoked"]]) >= MAX_KEYS_PER_USER:
            raise ValueError(f"最多只能有 {MAX_KEYS_PER_USER} 个有效 Key")
        raw, key_hash, prefix = new_key()
        key_id = "k_" + uuid_hex()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO api_keys
                   (key_id, key_hash, prefix, label, username, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (key_id, key_hash, prefix, (label or "").strip(), username, time.time()),
            )
        return {"key_id": key_id, "key": raw, "prefix": prefix, "label": label}

    def list_keys(self, username: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM api_keys WHERE username = ? ORDER BY created_at DESC",
                (username,),
            ).fetchall()
        return [
            {
                "key_id": r["key_id"],
                "prefix": r["prefix"],
                "label": r["label"],
                "created_at": r["created_at"],
                "last_used_at": r["last_used_at"],
                "revoked": bool(r["revoked"]),
            }
            for r in rows
        ]

    def revoke_key(self, username: str, key_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET revoked = 1 WHERE key_id = ? AND username = ?",
                (key_id, username),
            )
            return cur.rowcount > 0

    def verify_key(self, raw_key: Optional[str]) -> Optional[str]:
        """Key → 用户名（有效才返回）；顺手记录 last_used_at。"""
        if not raw_key:
            return None
        digest = hash_key(raw_key)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT key_id, username, revoked FROM api_keys WHERE key_hash = ?",
                (digest,),
            ).fetchone()
            if row is None or row["revoked"]:
                return None
            conn.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE key_id = ?",
                (time.time(), row["key_id"]),
            )
        return str(row["username"])


def uuid_hex() -> str:
    return secrets.token_hex(8)


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict) -> None:
    """给老库补新列（SQLite 没 IF NOT EXISTS ADD COLUMN，靠 PRAGMA 自己判）。"""
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
