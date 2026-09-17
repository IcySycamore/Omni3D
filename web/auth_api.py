"""认证路由的**唯一实现**：服务商 API 与官网门户都挂这一份。

为什么抽出来：账号（注册 / 登录 / 令牌）只该有一套协议与一套校验逻辑 ——
两个服务各自复制一份端点，迟早一边改了另一边没跟上。

协议（客户端先算哈希，明文密码不上网）::

    salt      = 服务端随机；verifier = sha256(salt + password)
    登录：nonce ← /challenge；proof = sha256(nonce + verifier) → /login
    n 分钟后令牌滑动过期（``OMNI3D_SESSION_TTL`` 可覆盖）

挂载方式::

    register_auth_routes(app, store, sessions, api_key_identity=callable)

``api_key_identity`` 给出「当前请求的 API Key 对应的用户名」（官网不需要，传 None）。
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Callable, Optional

from fastapi import Body, FastAPI, Header
from fastapi.responses import JSONResponse

from auth_store import (
    AuthStore,
    compute_proof,
    new_nonce,
    new_salt,
    validate_username,
)
from session_manager import SessionManager

# nonce 临时表：nonce -> (username, created_at)，用完即弃
_nonces: dict = {}
_nonce_lock = threading.Lock()
NONCE_TTL = 300.0  # 秒


def _put_nonce(nonce: str, username: str) -> None:
    with _nonce_lock:
        now = time.time()
        for k in [k for k, (_, c) in _nonces.items() if now - c > NONCE_TTL]:
            _nonces.pop(k, None)
        _nonces[nonce] = (username, now)


def _take_nonce(nonce: str) -> Optional[str]:
    """取出并作废 nonce（一次性）；返回其绑定的 username 或 None。"""
    with _nonce_lock:
        item = _nonces.pop(nonce, None)
    if item is None:
        return None
    username, created = item
    if time.time() - created > NONCE_TTL:
        return None
    return username


def register_auth_routes(
    app: FastAPI,
    store: AuthStore,
    sessions: SessionManager,
    api_key_identity: Optional[Callable[[], Optional[str]]] = None,
    models_for_identity: Optional[Callable[[], list]] = None,
) -> dict:
    """把 salt / register / challenge / login / logout / me 挂到 app 上。

    返回端点函数字典（键：salt/register/challenge/login/logout/me）：
    调用方可以拿来做兼容别名或直接测（免得只能走 HTTP）。

    ``models_for_identity`` 非空时，``/api/auth/me`` 会一并回传**可用模型**
    ——客户端「验证 API Key」那一次请求就能把模型清单拿到手。
    官方门户不需要（它不管模型），传 None 即可。
    """

    def _key_user() -> Optional[str]:
        return api_key_identity() if api_key_identity else None

    @app.post("/api/auth/salt")
    def auth_salt(payload: dict = Body(...)):
        """注册前领取随机 salt。Body: {"username"}（用户名已存在则 409）。"""
        username = (payload.get("username") or "").strip()
        if not username:
            return JSONResponse({"error": "缺少 username"}, status_code=400)
        invalid = validate_username(username)
        if invalid:
            return JSONResponse({"error": invalid}, status_code=400)
        if store.user_exists(username):
            return JSONResponse({"error": "用户名已存在"}, status_code=409)
        return JSONResponse({"salt": new_salt()})

    @app.post("/api/auth/register")
    def auth_register(payload: dict = Body(...)):
        """注册。Body: {"username","salt","verifier"}（服务器只存 verifier）。"""
        username = (payload.get("username") or "").strip()
        salt = payload.get("salt") or ""
        verifier = payload.get("verifier") or ""
        if not username or not salt or not verifier:
            return JSONResponse(
                {"error": "缺少 username / salt / verifier"}, status_code=400
            )
        # 服务端独有的强制检查：客户端可被绕过，用户名规则必须在此兜底。
        invalid = validate_username(username)
        if invalid:
            return JSONResponse({"error": invalid}, status_code=400)
        if not store.create_user(username, salt, verifier):
            return JSONResponse({"error": "用户名已存在"}, status_code=409)
        return JSONResponse({"ok": True, "username": username})

    @app.post("/api/auth/challenge")
    def auth_challenge(payload: dict = Body(...)):
        """登录第一步：取 nonce + salt。Body: {"username"}。"""
        username = (payload.get("username") or "").strip()
        salt = store.get_salt(username)
        if not salt:
            return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
        nonce = new_nonce()
        _put_nonce(nonce, username)
        return JSONResponse({"nonce": nonce, "salt": salt})

    @app.post("/api/auth/login")
    def auth_login(payload: dict = Body(...)):
        """登录第二步：Body: {"username","nonce","proof"}。"""
        username = (payload.get("username") or "").strip()
        nonce = payload.get("nonce") or ""
        proof = payload.get("proof") or ""
        if not username or not nonce or not proof:
            return JSONResponse(
                {"error": "缺少 username / nonce / proof"}, status_code=400
            )
        bound_user = _take_nonce(nonce)
        if bound_user is None or bound_user != username:
            return JSONResponse({"error": "nonce 无效或已过期"}, status_code=401)
        verifier = store.get_verifier(username)
        if not verifier:
            return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
        if not secrets.compare_digest(compute_proof(nonce, verifier), proof):
            return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
        token = sessions.create(username)
        return JSONResponse(
            {
                "ok": True,
                "token": token,
                "username": username,
                "expires_in": sessions.ttl_seconds,
            }
        )

    @app.post("/api/auth/password")
    def auth_password(
        payload: dict = Body(...), x_auth_token: Optional[str] = Header(default=None)
    ):
        """改密：Body: ``{"username","nonce","proof","salt","verifier"}``。

        ``nonce``/``proof`` 是**旧密码**的登录证明（与 /login 同一协议，一次性）；
        通过后才写入新的 salt + verifier —— 明文密码依然不上网。
        """
        username = (payload.get("username") or "").strip()
        nonce = payload.get("nonce") or ""
        proof = payload.get("proof") or ""
        salt = payload.get("salt") or ""
        verifier = payload.get("verifier") or ""
        if not all([username, nonce, proof, salt, verifier]):
            return JSONResponse(
                {"error": "缺少 username / nonce / proof / salt / verifier"},
                status_code=400,
            )
        if not (8 <= len(salt) <= 128) or not (32 <= len(verifier) <= 128):
            return JSONResponse({"error": "salt / verifier 不合法"}, status_code=400)
        # 已经登录的会话必须就是本人（不然拿着自己的证明就能改别人的密码）
        current = sessions.username_for(x_auth_token)
        if current and current != username:
            return JSONResponse(
                {"error": "登录身份与用户名不一致"}, status_code=403
            )
        bound_user = _take_nonce(nonce)
        if bound_user is None or bound_user != username:
            return JSONResponse({"error": "nonce 无效或已过期"}, status_code=401)
        old = store.get_verifier(username)
        if not old or not secrets.compare_digest(compute_proof(nonce, old), proof):
            return JSONResponse({"error": "原密码不对"}, status_code=401)
        if not store.set_password(username, salt, verifier):
            return JSONResponse({"error": "账号不存在"}, status_code=404)
        return JSONResponse({"ok": True, "username": username})

    @app.post("/api/auth/logout")
    def auth_logout(x_auth_token: Optional[str] = Header(default=None)):
        """注销当前令牌。"""
        return JSONResponse({"ok": sessions.drop(x_auth_token)})

    @app.get("/api/auth/me")
    def auth_me(x_auth_token: Optional[str] = Header(default=None)):
        """当前身份：``X-Api-Key`` 优先，其次 ``X-Auth-Token``。

        顺带回传这台服务商**给这个身份**的可用模型（面板的模型菜单就靠它）。
        """
        models = models_for_identity() if models_for_identity else None
        name = _key_user()
        if name:
            payload = {"ok": True, "username": name, "via": "api_key"}
            if models is not None:
                payload["models"] = models
            return JSONResponse(payload)
        username = sessions.username_for(x_auth_token)
        if not username:
            return JSONResponse({"error": "未登录"}, status_code=401)
        payload = {
            "ok": True,
            "username": username,
            "via": "token",
            "expires_in": sessions.ttl_seconds,
        }
        if models is not None:
            payload["models"] = models
        return JSONResponse(payload)

    return {
        "salt": auth_salt,
        "register": auth_register,
        "challenge": auth_challenge,
        "login": auth_login,
        "password": auth_password,
        "logout": auth_logout,
        "me": auth_me,
    }
