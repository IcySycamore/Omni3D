"""Omni3D **官网**（门户 / 控制面）。

三个角色各占一个端口，互不混在一起：

==================  ==================  ================================================
面板 panel          ``PAGES_PORT``      web/pages.py —— 用户干活的地方（web client）
**官网 portal**     ``PORTAL_PORT``     **本文件** —— 注册登录 / 买套餐 / 管 API Key
服务商 API          ``API_PORT``        web/server.py —— 真正跑重建（数据面）
==================  ==================  ================================================

官网与服务商**同机部署、共享同一份 SQLite**（``data/``）：账号在
``omni3d_users.db``（`auth_store`），API Key 与次数在 ``omni3d_portal.db``
（`portal_store`）。服务商只负责**校验** Key、按次扣减；签发与计费都在这里。

服务商只负责**校验** Key。

计费（详见 `portal_store`）：

* **按用量**：点云 200 点 = 1 分 / 体素 1,000 = 1 分 / 网格 1,000 面 = 1 分；
* **按计划**：Personal $8.99 100 次 / Professional $18.99 600 次（月度重置）；
* **用量包**：预付折扣包（100 万单位，9 折，不过期）；
* 另有**余额**接零星按量消费；扣减顺序：计划包 → 用量包 → 余额。

登录协议与服务器 API 完全一致（``auth_api.register_auth_routes``）：
注册 / 登录 / 改密 的**唯一实现**只此一份，两边挂同一个函数。
"""
from __future__ import annotations

import os
import sys

_WEB_DIR = os.path.dirname(os.path.abspath(__file__))
if _WEB_DIR not in sys.path:
    sys.path.insert(0, _WEB_DIR)

from fastapi import Body, FastAPI, Header  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from auth_api import register_auth_routes  # noqa: E402
from auth_store import AuthStore  # noqa: E402
from hosting import (  # noqa: E402
    API_ORIGIN,
    API_PORT,
    ASSETS_DIR,
    PAGE_PORT,
    PORTAL_HOST,
    PORTAL_PORT,
)
from portal_store import (  # noqa: E402
    BETA_FREE,
    METRICS,
    PACKS,
    PLANS,
    PLAN_PERIOD_DAYS,
    POINTS_PER_CENT,
    TOPUP_TIERS_CENTS,
    PortalStore,
    plan_by_id,
)
from session_manager import SessionManager  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_ROOT, "data")

auth_store = AuthStore(os.path.join(DATA_DIR, "omni3d_users.db"))
session_manager = SessionManager()
portal_store = PortalStore(os.path.join(DATA_DIR, "omni3d_portal.db"))

app = FastAPI(title="Omni3D 官网", version="0.1.0")
# 品牌图（logo）与面板共用同一份静态资源
if os.path.isdir(ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")
# 账号（salt / register / challenge / login / logout / me）——与服务商同一份实现
register_auth_routes(app, auth_store, session_manager)


def _username_of(token: str | None) -> str | None:
    return session_manager.username_for(token)


def _login_or_401(token: str | None):
    """打到用户名；未登录就返回 401 响应（调用方 isinstance 判一下）。"""
    username = _username_of(token)
    if not username:
        return JSONResponse({"error": "未登录"}, status_code=401)
    return username


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """官网页面（单文件，自带样式与脚本）。"""
    with open(os.path.join(_WEB_DIR, "portal.html"), encoding="utf-8") as fh:
        return fh.read()


@app.get("/api/p/config")
def portal_config():
    """页面启动时读：定价表（按用量 / 按计划 / 用量包）+ 地址 + 验证阶段标记。"""
    return {
        "plans": PLANS,
        "metered": METRICS,
        "packs": PACKS,
        "topup_tiers": TOPUP_TIERS_CENTS,
        "plan_period_days": PLAN_PERIOD_DAYS,
        "pages_port": PAGE_PORT,
        "api_port": API_PORT,
        "api_origin": API_ORIGIN,
        "portal_port": PORTAL_PORT,
        # 验证阶段：所有方案免费使用（用量照记）
        "beta_free": BETA_FREE,
        "points_per_cent": POINTS_PER_CENT,
        # 演示环境不接支付网关
        "payment": "demo",
    }


@app.get("/api/p/me")
def portal_me(x_auth_token: str | None = Header(default=None)):
    """控制台首页数据：资料 + 余额 + 计划包 + 用量包 + 用量（首次访问建账号）。"""
    username = _login_or_401(x_auth_token)
    if not isinstance(username, str):
        return username
    account = portal_store.ensure_account(username)
    return JSONResponse(
        {
            "ok": True,
            **account,
            "plan_pack": portal_store.plan_pack(username),
            "packs": portal_store.packs(username),
            "usage": portal_store.usage_summary(username),
            "beta_free": BETA_FREE,
        }
    )


@app.get("/api/p/ledger")
def portal_ledger(
    limit: int = 100, x_auth_token: str | None = Header(default=None)
):
    """流水：充值 / 购买 / 按量扣费 / 计划重置。"""
    username = _login_or_401(x_auth_token)
    if not isinstance(username, str):
        return username
    return JSONResponse({"ok": True, "ledger": portal_store.ledger(username, limit)})


@app.post("/api/p/topup")
def portal_topup(
    payload: dict = Body(...), x_auth_token: str | None = Header(default=None)
):
    """余额充值（演示：不接支付）。Body: {"amount_cents": 500}。"""
    username = _login_or_401(x_auth_token)
    if not isinstance(username, str):
        return username
    try:
        amount = int(payload.get("amount_cents") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"error": "金额不合法"}, status_code=400)
    try:
        account = portal_store.topup(username, amount)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **account})


@app.get("/api/p/packs")
def portal_packs(x_auth_token: str | None = Header(default=None)):
    """当前账号的用量包（每个包独立；不过期 → infinite）。"""
    username = _login_or_401(x_auth_token)
    if not isinstance(username, str):
        return username
    return JSONResponse({"ok": True, "packs": portal_store.packs(username)})


@app.post("/api/p/packs")
def portal_buy_pack(
    payload: dict = Body(...), x_auth_token: str | None = Header(default=None)
):
    """购买用量包。Body: {"pack_id": "pack_points"}。"""
    username = _login_or_401(x_auth_token)
    if not isinstance(username, str):
        return username
    pack_id = (payload.get("pack_id") or "").strip()
    try:
        out = portal_store.buy_pack(username, pack_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **out})


@app.post("/api/p/profile")
def portal_profile(
    payload: dict = Body(default={}), x_auth_token: str | None = Header(default=None)
):
    """改账号资料（显示名 / 邮箱）。改密走 ``/api/auth/password``。"""
    username = _login_or_401(x_auth_token)
    if not isinstance(username, str):
        return username
    try:
        account = portal_store.set_profile(
            username, payload.get("display_name") or "", payload.get("email") or ""
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **account})


@app.post("/api/p/purchase")
def portal_purchase(
    payload: dict = Body(...), x_auth_token: str | None = Header(default=None)
):
    """买计划包（演示：不接支付）。Body: {"plan_id"}。"""
    username = _login_or_401(x_auth_token)
    if not isinstance(username, str):
        return username
    plan_id = (payload.get("plan_id") or "").strip()
    if plan_by_id(plan_id) is None and plan_id != "points":
        return JSONResponse({"error": "没有这个套餐"}, status_code=400)
    try:
        account = portal_store.purchase(username, plan_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **account})


@app.get("/api/p/orders")
def portal_orders(x_auth_token: str | None = Header(default=None)):
    """订单（计划包 / 用量包 / 充值）。"""
    username = _login_or_401(x_auth_token)
    if not isinstance(username, str):
        return username
    return JSONResponse({"ok": True, "orders": portal_store.orders(username)})


@app.get("/api/p/keys")
def portal_keys(x_auth_token: str | None = Header(default=None)):
    """列出当前账号的 API Key（只有前缀，明文不落库）。"""
    username = _username_of(x_auth_token)
    if not username:
        return JSONResponse({"error": "未登录"}, status_code=401)
    return JSONResponse({"ok": True, "keys": portal_store.list_keys(username)})


@app.post("/api/p/keys")
def portal_create_key(
    payload: dict = Body(default={}), x_auth_token: str | None = Header(default=None)
):
    """新建 API Key —— 响应里的 ``key`` 是**唯一一次**能看到明文的机会。"""
    username = _username_of(x_auth_token)
    if not username:
        return JSONResponse({"error": "请先登录"}, status_code=401)
    label = (payload or {}).get("label") or ""
    try:
        created = portal_store.create_key(username, label)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **created}, status_code=201)


@app.post("/api/p/keys/{key_id}/revoke")
def portal_revoke_key(key_id: str, x_auth_token: str | None = Header(default=None)):
    username = _username_of(x_auth_token)
    if not username:
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not portal_store.revoke_key(username, key_id):
        return JSONResponse({"error": "没有这个 Key"}, status_code=404)
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn

    print(f"[portal] Omni3D 官网 http://{PORTAL_HOST}:{PORTAL_PORT}/")
    print(f"[portal] 面板 panel :{PAGE_PORT} | 服务商 API :{API_PORT}")
    uvicorn.run(app, host=PORTAL_HOST, port=PORTAL_PORT)
