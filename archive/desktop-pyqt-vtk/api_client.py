"""桌面客户端的 HTTP 客户端（唯一 server 的一种 client 实现）。

同时实现与服务器一致的挑战-应答握手：
    verifier = sha256(salt + password)              （注册时上行，服务器只存它）
    proof    = sha256(nonce + verifier)             （登录时上行）

对端实现见 `web/auth_store.py`，两处算法必须一致。
明文密码只存在于本机内存，不上网、不落库。

所有网络异常统一转换为 `ApiError`，便于 UI 层一致处理。
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Callable, Optional

import requests

DEFAULT_TIMEOUT = 15

# ---- 账号规则（必须与 web/auth_store.py 保持一致；客户端仅用于即时提示）----
USERNAME_MIN = 3
USERNAME_MAX = 32
PASSWORD_MIN = 8
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def validate_username(username: str) -> Optional[str]:
    """校验用户名；合法返回 None，否则返回提示文案。"""
    if not username:
        return "用户名不能为空"
    if len(username) < USERNAME_MIN or len(username) > USERNAME_MAX:
        return f"用户名长度需为 {USERNAME_MIN}–{USERNAME_MAX} 个字符"
    if not _USERNAME_RE.match(username):
        return "用户名只能包含字母、数字、下划线、点或连字符"
    return None


def validate_password(password: str) -> Optional[str]:
    """校验密码；合法返回 None。

    注意：本协议下服务器只收到 verifier，看不到明文密码，
    因此密码强度**只能**在客户端校验。
    """
    if not password:
        return "密码不能为空"
    if len(password) < PASSWORD_MIN:
        return f"密码至少 {PASSWORD_MIN} 个字符"
    if not password.strip():
        return "密码不能全为空白字符"
    return None


def _sha256_hex(*chunks: bytes) -> str:
    h = hashlib.sha256()
    for c in chunks:
        h.update(c)
    return h.hexdigest()


def compute_verifier(salt: str, password: str) -> str:
    """verifier = sha256(salt + password)。"""
    return _sha256_hex(salt.encode("ascii"), password.encode("utf-8"))


def compute_proof(nonce: str, verifier: str) -> str:
    """proof = sha256(nonce + verifier)。"""
    return _sha256_hex(nonce.encode("ascii"), verifier.encode("ascii"))


class ApiError(Exception):
    """网络或服务端错误（含服务器返回的 error 文案）。"""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.status = status


class AuthExpiredError(ApiError):
    """登录令牌已失效（服务器 401）。

    与普通的 401 区分开：登录/挑战接口在密码错误时也会返回 401，
    那种情况要正常展示错误文案；只有**已带 token** 却仍收到 401，
    才意味着会话过期，需要退回登录窗。
    """


class ApiClient:
    """与重建服务器交互。"""

    def __init__(self, base_url: str, token: Optional[str] = None,
                 client_id: str = "desktop"):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token
        self.client_id = client_id
        self._session = requests.Session()

    # ---- 基础设施 ----
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _headers(self) -> dict:
        return {"X-Auth-Token": self.token} if self.token else {}

    def _request(self, method: str, path: str, *, check: bool = True, **kwargs):
        """统一发起请求：网络异常 → ApiError；非 2xx → ApiError。"""
        headers = kwargs.pop("headers", None)
        merged = self._headers()
        if headers:
            merged.update(headers)
        try:
            resp = self._session.request(method, self._url(path),
                                         headers=merged, **kwargs)
        except requests.RequestException as exc:
            raise ApiError(f"无法连接服务器（{self.base_url}）：{exc}") from exc
        if check and not resp.ok:
            msg = resp.text
            try:
                msg = resp.json().get("error") or msg
            except ValueError:
                pass
            # 已带 token 却仍 401 ⇒ 会话过期（登录接口自身的 401 不算）
            if resp.status_code == 401 and self.token and not path.startswith("/api/auth/"):
                raise AuthExpiredError(str(msg), resp.status_code)
            raise ApiError(str(msg), resp.status_code)
        return resp

    # ---- 健康检查 ----
    def health(self) -> dict:
        return self._request("GET", "/health", timeout=DEFAULT_TIMEOUT).json()

    # ---- 认证 ----
    def register(self, username: str, password: str) -> None:
        """注册：先领 salt，本地算 verifier，再上行（不上传明文）。"""
        salt = self._request("POST", "/api/auth/salt", json={"username": username},
                             timeout=DEFAULT_TIMEOUT).json()["salt"]
        verifier = compute_verifier(salt, password)
        self._request("POST", "/api/auth/register",
                      json={"username": username, "salt": salt, "verifier": verifier},
                      timeout=DEFAULT_TIMEOUT)

    def login(self, username: str, password: str) -> str:
        """挑战-应答登录，返回 token。"""
        ch = self._request("POST", "/api/auth/challenge",
                           json={"username": username},
                           timeout=DEFAULT_TIMEOUT).json()
        proof = compute_proof(ch["nonce"], compute_verifier(ch["salt"], password))
        resp = self._request("POST", "/api/auth/login",
                             json={"username": username, "nonce": ch["nonce"],
                                   "proof": proof},
                             timeout=DEFAULT_TIMEOUT).json()
        self.token = resp["token"]
        return self.token

    def me(self) -> str:
        return self._request("GET", "/api/auth/me",
                             timeout=DEFAULT_TIMEOUT).json()["username"]

    def logout(self) -> None:
        try:
            self._request("POST", "/api/auth/logout", check=False,
                          timeout=DEFAULT_TIMEOUT)
        except ApiError:
            pass
        self.token = None

    def claim_anonymous(self) -> int:
        """把匿名历史并入当前账号（登录后调用）。"""
        data = self._request("POST", "/api/auth/claim",
                             params={"client_id": self.client_id},
                             timeout=DEFAULT_TIMEOUT).json()
        return int(data.get("moved", 0))

    def claim_preview(self) -> int:
        """预览「匿名记录并入账号」将迁移多少条（0 表示无需提示）。"""
        data = self._request("GET", "/api/auth/claim/preview",
                             params={"client_id": self.client_id},
                             timeout=DEFAULT_TIMEOUT).json()
        return int(data.get("count", 0))

    # ---- 历史 ----
    def list_history(self, limit: int = 50) -> dict:
        return self._request("GET", "/api/history",
                             params={"client_id": self.client_id, "limit": limit},
                             timeout=DEFAULT_TIMEOUT).json()

    def get_session(self, session_id: str, include_points: bool = False) -> dict:
        return self._request(
            "GET", f"/api/history/{session_id}",
            params={"client_id": self.client_id,
                    "include_points": str(include_points).lower()},
            timeout=DEFAULT_TIMEOUT).json()

    def delete_history(self, session_id: str) -> None:
        self._request("DELETE", f"/api/history/{session_id}",
                      params={"client_id": self.client_id},
                      timeout=DEFAULT_TIMEOUT)

    def download_ply(self, session_id: str) -> bytes:
        return self._request("GET", f"/api/history/{session_id}/ply",
                             params={"client_id": self.client_id},
                             timeout=60).content

    # ---- 重建 ----
    def submit_video(self, video_path: str, frame_count: int = 12,
                     resolution: int = 512) -> str:
        """上传视频重建，返回 task_id。"""
        data = {
            "resolution": str(resolution),
            "is_video": "true",
            "frame_count": str(frame_count),
            "intrinsics": "null",
            "extrinsics": "null",
            "client_id": self.client_id,
        }
        with open(video_path, "rb") as fh:
            files = {"files": (os.path.basename(video_path), fh, "video/mp4")}
            resp = self._request("POST", "/api/tasks", data=data, files=files,
                                 timeout=600)
        return resp.json()["task_id"]

    def submit_images(self, image_paths: list, resolution: int = 512) -> str:
        """上传图片序列重建，返回 task_id。"""
        data = {
            "resolution": str(resolution),
            "is_video": "false",
            "client_id": self.client_id,
        }
        handles = []
        files = []
        try:
            for p in image_paths:
                fh = open(p, "rb")
                handles.append(fh)
                files.append(("files", (os.path.basename(p), fh, "image/jpeg")))
            resp = self._request("POST", "/api/tasks", data=data, files=files,
                                 timeout=600)
        finally:
            for fh in handles:
                fh.close()
        return resp.json()["task_id"]

    def poll_task(self, task_id: str, on_progress: Optional[Callable[[dict], None]] = None,
                  cancelled: Optional[Callable[[], bool]] = None,
                  interval: float = 1.0, timeout: float = 1800.0) -> dict:
        """轮询任务直至完成，返回最终任务字典（含 result）。"""
        import time

        started = time.time()
        while True:
            if cancelled and cancelled():
                raise ApiError("已取消")
            if time.time() - started > timeout:
                raise ApiError("重建超时")
            task = self._request("GET", f"/api/tasks/{task_id}",
                                 params={"include_result": "true"},
                                 timeout=DEFAULT_TIMEOUT).json()
            if on_progress:
                on_progress(task)
            if task.get("status") in ("done", "failed"):
                return task
            time.sleep(interval)

    # ---- 尺度反推 ----
    def infer_scale(self, task_id: str, point_a: list, point_b: list,
                    real_distance: float) -> dict:
        return self._request(
            "POST", f"/api/tasks/{task_id}/scale",
            params={"client_id": self.client_id},
            json={"point_a": list(point_a), "point_b": list(point_b),
                  "real_distance": float(real_distance)},
            timeout=DEFAULT_TIMEOUT).json()
