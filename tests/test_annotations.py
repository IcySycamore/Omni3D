"""标注持久化（#27）：幂等整体替换、按会话隔离、级联删除、归属迁移。

标注是**产物**（元素 = 基础点 + 派生形），必须可保存 / 可回看 / 随会话一起删除。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

# torch 必须最先导入（本机 fbgemm.dll 加载顺序冲突）
import torch  # noqa: F401,I001

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = os.path.join(_ROOT, "web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)

import server  # noqa: E402
from session_store import SessionStore, anon_owner  # noqa: E402

_SAMPLE = {
    "version": 1,
    "elements": [
        {"id": "e1", "kind": "point", "points": [[0.0, 0.0, 0.0]]},
        {"id": "e2", "kind": "point", "points": [[1.0, 0.0, 0.0]]},
        {"id": "e3", "kind": "segment", "points": [], "refs": ["e1", "e2"]},
    ],
}


@pytest.fixture()
def store(tmp_path):
    s = SessionStore(
        db_path=str(tmp_path / "sessions.db"),
        sessions_dir=str(tmp_path / "ply"),
    )
    yield s
    s.close()


def _save_session(store: SessionStore, session_id: str, owner: str) -> None:
    store.save_session(
        session_id=session_id,
        owner=owner,
        result={"num_views": 2, "num_points": 3, "points": []},
    )


class TestAnnotationStore:
    def test_roundtrip(self, store):
        owner = anon_owner("alice")
        _save_session(store, "s1", owner)
        assert store.save_annotations("s1", owner, _SAMPLE) is True
        assert store.get_annotations("s1", owner) == _SAMPLE

    def test_missing_returns_none(self, store):
        owner = anon_owner("alice")
        _save_session(store, "s1", owner)
        assert store.get_annotations("s1", owner) is None

    def test_repeated_put_is_idempotent(self, store):
        """验收要求：重复提交不产生重复数据。"""
        owner = anon_owner("alice")
        _save_session(store, "s1", owner)
        for _ in range(3):
            store.save_annotations("s1", owner, _SAMPLE)
        assert store.count_annotations(owner) == 1
        assert store.get_annotations("s1", owner) == _SAMPLE

    def test_put_replaces_wholesale(self, store):
        """整体替换：新载荷里没有的元素必须消失（不是合并）。"""
        owner = anon_owner("alice")
        _save_session(store, "s1", owner)
        store.save_annotations("s1", owner, _SAMPLE)
        smaller = {"version": 1, "elements": [_SAMPLE["elements"][0]]}
        store.save_annotations("s1", owner, smaller)
        assert store.get_annotations("s1", owner) == smaller

    def test_unknown_session_is_rejected(self, store):
        assert store.save_annotations("nope", anon_owner("alice"), _SAMPLE) is False

    def test_other_owner_cannot_write(self, store):
        _save_session(store, "s1", anon_owner("alice"))
        assert store.save_annotations("s1", anon_owner("bob"), _SAMPLE) is False
        assert store.get_annotations("s1", anon_owner("bob")) is None

    def test_get_session_includes_annotations(self, store):
        owner = anon_owner("alice")
        _save_session(store, "s1", owner)
        store.save_annotations("s1", owner, _SAMPLE)
        data = store.get_session("s1", owner)
        assert data["annotations"] == _SAMPLE
        # 没标注时是 None（而不是缺字段）
        _save_session(store, "s2", owner)
        assert store.get_session("s2", owner)["annotations"] is None

    def test_delete_session_cascades(self, store):
        """验收要求：删除会话后标注一并消失。"""
        owner = anon_owner("alice")
        _save_session(store, "s1", owner)
        store.save_annotations("s1", owner, _SAMPLE)
        assert store.delete_session("s1", owner) is True
        assert store.get_annotations("s1", owner) is None
        assert store.count_annotations(owner) == 0

    def test_rename_owner_moves_annotations(self, store):
        """匿名历史并入账号时，标注必须跟着走。"""
        anon, user = anon_owner("alice"), "user:alice"
        _save_session(store, "s1", anon)
        store.save_annotations("s1", anon, _SAMPLE)
        store.rename_owner(anon, user)
        assert store.get_annotations("s1", user) == _SAMPLE
        assert store.get_annotations("s1", anon) is None

    def test_two_sessions_are_isolated(self, store):
        owner = anon_owner("alice")
        _save_session(store, "s1", owner)
        _save_session(store, "s2", owner)
        store.save_annotations("s1", owner, _SAMPLE)
        store.save_annotations("s2", owner, {"version": 1, "elements": []})
        assert store.get_annotations("s1", owner) == _SAMPLE
        assert store.get_annotations("s2", owner) == {"version": 1, "elements": []}


@pytest.fixture()
def endpoint_env(tmp_path, monkeypatch):
    s = SessionStore(
        db_path=str(tmp_path / "sessions.db"),
        sessions_dir=str(tmp_path / "ply"),
    )
    monkeypatch.setattr(server, "session_store", s)
    yield s
    s.close()


def _body(resp) -> dict:
    return json.loads(bytes(resp.body).decode("utf-8"))


class TestAnnotationEndpoint:
    def test_put_then_read_back(self, endpoint_env):
        owner = anon_owner("alice")
        _save_session(endpoint_env, "s1", owner)
        resp = server.put_annotations("s1", _SAMPLE, client_id="alice",
                                      x_auth_token=None)
        assert resp.status_code == 200
        assert _body(resp)["annotations"] == _SAMPLE

        # GET /api/history/{id} 必须附带标注（验收要求）
        got = _body(server.get_history("s1", client_id="alice", x_auth_token=None))
        assert got["annotations"] == _SAMPLE

    def test_repeated_put_204_style(self, endpoint_env):
        owner = anon_owner("alice")
        _save_session(endpoint_env, "s1", owner)
        for _ in range(3):
            server.put_annotations("s1", _SAMPLE, client_id="alice",
                                   x_auth_token=None)
        assert endpoint_env.count_annotations(owner) == 1

    def test_owner_mismatch_is_404(self, endpoint_env):
        _save_session(endpoint_env, "s1", anon_owner("alice"))
        resp = server.put_annotations("s1", _SAMPLE, client_id="bob",
                                      x_auth_token=None)
        assert resp.status_code == 404

    def test_unknown_session_is_404(self, endpoint_env):
        resp = server.put_annotations("nope", _SAMPLE, client_id="alice",
                                      x_auth_token=None)
        assert resp.status_code == 404

    def test_non_object_body_is_400(self, endpoint_env):
        _save_session(endpoint_env, "s1", anon_owner("alice"))
        for bad in ([], "x", 3):
            resp = server.put_annotations("s1", bad, client_id="alice",
                                          x_auth_token=None)
            assert resp.status_code == 400

    def test_deleted_session_annotations_gone(self, endpoint_env):
        owner = anon_owner("alice")
        _save_session(endpoint_env, "s1", owner)
        server.put_annotations("s1", _SAMPLE, client_id="alice", x_auth_token=None)
        assert _body(server.delete_history("s1", client_id="alice",
                                           x_auth_token=None))["ok"] is True
        assert endpoint_env.get_annotations("s1", owner) is None
