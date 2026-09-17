"""官网门户（控制面）：API Key 签发 / 校验 / 吊销 + 额度（计划包 / 用量包 / 余额）。

覆盖：
- Key 只存哈希（库里**没有**明文），前缀用于展示；
- 校验走哈希、吊销后立刻失效、跨账号不可用；
- 三种计量（点云 / 体素 / 网格）的单价折算；
- 扣减顺序：计划包 → 用量包 → 余额；用量包不过期、每包独立；
- 充值 / 流水 / 计划重置 / 改密；
- ``api_keys.username_for`` 能解析官网签发的 Key（服务商侧校验入口）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import pytest

# torch 必须最先导入（本机 fbgemm.dll 加载顺序冲突）
import torch  # noqa: F401,I001

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = os.path.join(_ROOT, "web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)

import api_keys  # noqa: E402
import portal  # noqa: E402
import portal_store  # noqa: E402
from portal_store import (  # noqa: E402
    BETA_FREE,
    MAX_KEYS_PER_USER,
    METRICS,
    PACKS,
    PLAN_PERIOD_DAYS,
    POINTS_PER_CENT,
    TOPUP_TIERS_CENTS,
    PortalStore,
    hash_key,
    metric_by_id,
    pack_by_id,
    points_to_cents,
    units_to_cents,
)

_SECRET = "sk-do-not-store-me"


@pytest.fixture()
def store(tmp_path) -> PortalStore:
    return PortalStore(str(tmp_path / "portal.db"))


class TestApiKeys:
    def test_plaintext_is_never_stored(self, store):
        created = store.create_key("alice", "笔记本")
        raw = created["key"]
        assert raw.startswith("omni3d_")
        assert store.verify_key(raw) == "alice"
        # 整库扫一遍：明文不能出现，哈希必须在
        with sqlite3.connect(store.db_path) as conn:
            dump = "\n".join(
                str(row) for row in conn.execute("SELECT * FROM api_keys").fetchall()
            )
        assert raw not in dump
        assert hash_key(raw) in dump
        assert created["prefix"].endswith("…")

    def test_prefix_is_stable_and_short(self, store):
        created = store.create_key("alice")
        assert created["key"].startswith(created["prefix"][:-1])

    def test_unknown_and_empty_keys(self, store):
        assert store.verify_key(None) is None
        assert store.verify_key("") is None
        assert store.verify_key("omni3d_nope") is None

    def test_revoke_kills_the_key(self, store):
        created = store.create_key("alice")
        assert store.revoke_key("alice", created["key_id"]) is True
        assert store.verify_key(created["key"]) is None
        assert store.list_keys("alice")[0]["revoked"] is True

    def test_cannot_revoke_someone_elses_key(self, store):
        created = store.create_key("alice")
        assert store.revoke_key("bob", created["key_id"]) is False
        assert store.verify_key(created["key"]) == "alice"

    def test_last_used_is_recorded(self, store):
        created = store.create_key("alice")
        assert store.list_keys("alice")[0]["last_used_at"] is None
        store.verify_key(created["key"])
        assert store.list_keys("alice")[0]["last_used_at"] is not None

    def test_key_limit(self, store):
        for _ in range(MAX_KEYS_PER_USER):
            store.create_key("alice")
        with pytest.raises(ValueError):
            store.create_key("alice")
        # 吊销一个就能再建
        keys = store.list_keys("alice")
        store.revoke_key("alice", keys[0]["key_id"])
        assert store.create_key("alice")["key"]


class TestBilling:
    def test_new_account_has_no_prepaid_calls(self, store):
        account = store.ensure_account("alice")
        assert account["credits"] == 0
        assert account["plan"] == portal_store.DEFAULT_PLAN
        # 幂等：再调一次不会变
        assert store.ensure_account("alice")["credits"] == 0

    def test_points_to_cents(self):
        assert points_to_cents(0) == 0
        assert points_to_cents(1) == 1  # 不足 200 点也算 1 分
        assert points_to_cents(POINTS_PER_CENT) == 1
        assert points_to_cents(POINTS_PER_CENT + 1) == 2
        assert points_to_cents(2_000_000) == 10_000

    def test_usage_is_recorded_and_summed(self, store):
        store.ensure_account("alice")
        store.record_usage("alice", "t1", 400)  # 2 分
        store.record_usage("alice", "t2", 200)  # 1 分
        store.record_usage("alice", "t1", 400)  # 幂等：同 task 不重记
        usage = store.usage_summary("alice")
        assert usage["calls"] == 2
        assert usage["points"] == 600
        assert usage["cents"] == 3
        assert usage["usd"] == 0.03

    def test_beta_is_free_so_nothing_blocks(self, store):
        assert BETA_FREE is True
        assert store.billing_enforced() is False

    def test_plan_purchase_grants_calls(self, store):
        store.ensure_account("alice")
        account = store.purchase("alice", "personal")
        assert account["credits"] == 100
        assert account["plan"] == "personal"
        orders = store.orders("alice")
        assert len(orders) == 1
        assert orders[0]["list_amount"] == 8.99
        # 验证阶段实付 0，标价照记
        assert orders[0]["amount"] == 0.0
        assert "验证阶段" in orders[0]["note"]

    def test_professional_plan_price_and_calls(self, store):
        account = store.purchase("alice", "professional")
        assert account["credits"] == 600
        plan = portal_store.plan_by_id("professional")
        assert plan["price"] == 18.99 and plan["calls"] == 600

    def test_metered_plan_switch_costs_nothing(self, store):
        store.purchase("alice", "personal")
        account = store.purchase("alice", "points")
        assert account["plan"] == "points"
        # 切计费方式不加次数
        assert account["credits"] == 100

    def test_unsupported_plans_are_rejected(self, store):
        """旧版的「按体素 / 其他方案」已改成按用量 SKU，不再是计划。"""
        for plan_id in ("voxel", "other"):
            with pytest.raises(ValueError):
                store.purchase("alice", plan_id)
            assert portal_store.plan_supported(plan_id) is False

    def test_purchase_unknown_plan(self, store):
        with pytest.raises(ValueError):
            store.purchase("alice", "nope")

    def test_plan_sets_reset_window(self, store):
        account = store.purchase("alice", "personal")
        assert account["plan_expires_at"] is not None
        assert 0 < account["plan_days_left"] <= PLAN_PERIOD_DAYS
        assert store.plan_pack("alice")["period_days"] == PLAN_PERIOD_DAYS

    def test_expired_plan_resets_its_calls(self, store):
        """到点就重置：额度回到本月总量，并记一笔流水。"""
        store.purchase("alice", "personal")
        assert store.charge("alice", "points", 500)["covered_by"] == "plan"
        assert store.account("alice")["credits"] == 99
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE accounts SET plan_expires_at = ? WHERE username = ?",
                (time.time() - 1, "alice"),
            )
        account = store.account("alice")
        assert account["credits"] == 100  # 重置回满
        assert account["plan_days_left"] <= PLAN_PERIOD_DAYS
        assert any(e["kind"] == "grant" for e in store.ledger("alice"))


class TestMetering:
    """按用量的量化计费：三种计量 + 用量包 + 余额。"""

    def test_unit_prices(self):
        assert units_to_cents("points", 200) == 1
        assert units_to_cents("voxels", 1_000) == 1
        assert units_to_cents("mesh", 1_000) == 1
        assert units_to_cents("voxels", 1_001) == 2
        assert units_to_cents("mesh", 0) == 0
        assert {m["id"] for m in METRICS} == {"points", "voxels", "mesh"}
        assert metric_by_id("mesh")["unit"] == "三角面"

    def test_pack_skus_are_discounted_and_never_expire(self):
        for sku in PACKS:
            assert sku["units"] == portal_store.PACK_UNITS
            assert sku["price"] < sku["list_amount"]
        assert pack_by_id("pack_points")["metric"] == "points"
        assert pack_by_id("nope") is None

    def test_buy_pack_grants_a_standalone_bucket(self, store):
        out = store.buy_pack("alice", "pack_points")
        assert out["pack_id"]
        packs = store.packs("alice")
        assert len(packs) == 1
        assert packs[0]["remaining"] == packs[0]["total"]
        assert packs[0]["infinite"] is True  # 不过期
        assert packs[0]["expires_at"] is None

    def test_packs_are_independent_and_consumed_in_order(self, store):
        first = store.buy_pack("alice", "pack_points")["pack_id"]
        second = store.buy_pack("alice", "pack_points")["pack_id"]
        # 一个包只留 300 点 → 先扣它，再扣下一个包
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE packs SET remaining = 300 WHERE pack_id = ?", (first,)
            )
        out = store.charge("alice", "points", 400)
        assert out["covered_by"] == "packs"
        by_id = {p["pack_id"]: p for p in store.packs("alice")}
        assert by_id[first]["remaining"] == 0
        assert by_id[second]["remaining"] == portal_store.PACK_UNITS - 100

    def test_a_pack_only_covers_its_own_metric(self, store):
        store.buy_pack("alice", "pack_points")
        out = store.charge("alice", "voxels", 1_000)
        assert out["covered_by"] == "free"  # 验证阶段：不动点云包、不扣余额
        assert store.packs("alice")[0]["remaining"] == portal_store.PACK_UNITS

    def test_pack_shortfall_is_paid_from_balance(self, store, monkeypatch):
        monkeypatch.setattr(portal_store, "BETA_FREE", False)
        store.buy_pack("alice", "pack_points")
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("UPDATE packs SET remaining = 100")
        store.topup("alice", 500)  # 余额 $5
        out = store.charge("alice", "points", 300)  # 100 包 + 200 点 = 1 分
        assert out["covered_by"] == "packs+balance"
        assert out["paid_cents"] == 1
        assert store.account("alice")["balance_cents"] == 499

    def test_balance_is_not_touched_during_beta(self, store):
        assert BETA_FREE is True
        store.topup("alice", 500)
        store.charge("alice", "points", 2_000)
        assert store.account("alice")["balance_cents"] == 500

    def test_topup_rejects_unknown_amount(self, store):
        with pytest.raises(ValueError):
            store.topup("alice", 123)
        assert store.topup("alice", TOPUP_TIERS_CENTS[0])["balance_cents"] == 500

    def test_ledger_records_everything(self, store):
        store.topup("alice", 1_000)
        store.buy_pack("alice", "pack_voxels")
        store.purchase("alice", "personal")
        store.record_usage("alice", "t42", 400)
        kinds = [e["kind"] for e in store.ledger("alice")]
        assert kinds[0] == "usage"  # 倒序：最新在最前
        assert {"topup", "buy_pack", "buy_plan", "usage"} <= set(kinds)
        topup_entry = [e for e in store.ledger("alice") if e["kind"] == "topup"][0]
        assert topup_entry["amount_cents"] == 1_000
        assert topup_entry["balance_after"] == 1_000

    def test_usage_by_metric_is_summarised(self, store):
        store.record_usage("alice", "t1", 400)  # 点云
        store.record_usage("alice", "t2", 3_000, "voxels")
        usage = store.usage_summary("alice")
        assert usage["calls"] == 2
        assert usage["by_metric"]["voxels"]["units"] == 3_000
        assert usage["by_metric"]["voxels"]["cents"] == 3
        assert usage["cents"] == 2 + 3

    def test_can_start_needs_some_budget(self, store, monkeypatch):
        monkeypatch.setattr(portal_store, "BETA_FREE", False)
        store.ensure_account("alice")
        assert store.can_start("alice") is False
        store.buy_pack("alice", "pack_mesh")
        assert store.can_start("alice") is True

    def test_charge_with_no_budget_is_free_during_beta(self, store):
        out = store.charge("ghost", "points", 10_000)
        assert out["covered_by"] == "free"
        assert out["paid_cents"] == 0
        assert out["list_cents"] == 50

    def test_profile_can_be_updated(self, store):
        store.set_profile("alice", "小艾", "a@example.com")
        account = store.account("alice")
        assert account["display_name"] == "小艾"
        assert account["email"] == "a@example.com"
        with pytest.raises(ValueError):
            store.set_profile("alice", "x", "not-an-email")


class TestProviderResolution:
    def test_issued_key_resolves_to_username(self, store, monkeypatch):
        """服务商侧的解析入口要认官网签发的 Key。"""
        monkeypatch.setattr(api_keys, "_store", store)
        created = store.create_key("alice")
        assert api_keys.username_for(created["key"]) == "alice"
        store.revoke_key("alice", created["key_id"])
        assert api_keys.username_for(created["key"]) is None

    def test_static_env_key_still_works(self, store, monkeypatch):
        """老部署用环境变量配的静态 Key 不能被官网这套挤掉。"""
        monkeypatch.setattr(api_keys, "_store", store)
        monkeypatch.setenv(api_keys.ENV_NAME, "sk-static=carol")
        assert api_keys.username_for("sk-static") == "carol"

    def test_store_key_wins_over_static(self, store, monkeypatch):
        monkeypatch.setattr(api_keys, "_store", store)
        monkeypatch.setenv(api_keys.ENV_NAME, f"{_SECRET}=carol")
        created = store.create_key("alice")
        assert created["key"] != _SECRET
        assert api_keys.username_for(created["key"]) == "alice"
        assert api_keys.username_for(_SECRET) == "carol"


class TestPortalApp:
    def test_landing_and_config_routes(self):
        paths = {getattr(r, "path", None) for r in portal.app.routes}
        assert "/" in paths
        assert "/api/p/config" in paths
        assert "/api/p/keys" in paths
        assert "/api/p/packs" in paths
        assert "/api/p/topup" in paths
        assert "/api/p/ledger" in paths
        assert "/api/p/profile" in paths
        # 账号端点与服务商**共用同一份实现**
        assert "/api/auth/login" in paths
        assert "/api/auth/me" in paths
        assert "/api/auth/password" in paths

    def test_config_exposes_pricing_and_ports(self):
        cfg = portal.portal_config()
        assert {p["id"] for p in cfg["plans"]} == {"personal", "professional"}
        assert {m["id"] for m in cfg["metered"]} == {"points", "voxels", "mesh"}
        assert {s["id"] for s in cfg["packs"]} == {
            "pack_points",
            "pack_voxels",
            "pack_mesh",
        }
        assert cfg["topup_tiers"] == TOPUP_TIERS_CENTS
        assert cfg["plan_period_days"] == PLAN_PERIOD_DAYS
        assert cfg["beta_free"] is True
        assert cfg["points_per_cent"] == POINTS_PER_CENT
        assert cfg["pages_port"]
        assert cfg["api_port"]
        assert cfg["portal_port"]

    def test_plan_prices(self):
        cfg = portal.portal_config()
        by_id = {p["id"]: p for p in cfg["plans"]}
        assert by_id["personal"]["price"] == 8.99
        assert by_id["personal"]["calls"] == 100
        assert by_id["professional"]["price"] == 18.99
        assert by_id["professional"]["calls"] == 600

    def test_metered_skus_are_priced(self):
        cfg = portal.portal_config()
        by_id = {m["id"]: m for m in cfg["metered"]}
        assert by_id["points"]["units_per_cent"] == 200
        assert by_id["voxels"]["units_per_cent"] == 1_000
        assert by_id["mesh"]["units_per_cent"] == 1_000

    def test_every_card_shows_free_badge(self):
        """验证阶段：所有卡片右上角都标 FREE。"""
        html = portal.index()
        assert "badge-free" in html
        assert 'class="badge-free">FREE' in html

    def test_page_has_the_three_tabs(self):
        html = portal.index()
        for tab in ('data-tab="pricing"', 'data-tab="console"', 'data-tab="account"'):
            assert tab in html
        for panel in (
            'id="tab-pricing"',
            'id="tab-console"',
            'id="tab-account"',
            'data-sub="metered"',
            'data-sub="plan"',
        ):
            assert panel in html

    def test_index_is_a_login_page_first(self):
        """未登录只能看到登录卡；定价 / 控制台 / 账户都在 viewApp 里。"""
        html = portal.index()
        assert 'id="viewLogin"' in html
        assert 'class="view hidden" id="viewApp"' in html
        assert 'id="authCard"' in html
        assert 'id="username"' in html and 'id="password"' in html

    def test_console_surfaces_balance_packs_and_ledger(self):
        html = portal.index()
        for el in (
            'id="summaryCards"',
            'id="packList"',
            'id="ledgerRows"',
            'id="orderRows"',
            'id="keyRows"',
            'id="topupRow"',
            'id="displayName"',
            'id="email"',
            'id="changePassword"',
        ):
            assert el in html, f"页面缺少：{el}"

    def test_index_serves_the_portal_page(self):
        html = portal.index()
        assert "Omni3D 官网" in html
        assert "/api/p/keys" in html

    def test_account_endpoints_need_a_token(self):
        resp = portal.portal_me(x_auth_token=None)
        assert resp.status_code == 401
        assert portal.portal_keys(x_auth_token=None).status_code == 401
        assert portal.portal_orders(x_auth_token=None).status_code == 401
        assert portal.portal_purchase({}, x_auth_token=None).status_code == 401
        assert portal.portal_create_key({}, x_auth_token=None).status_code == 401
        assert portal.portal_ledger(x_auth_token=None).status_code == 401
        assert portal.portal_topup({}, x_auth_token=None).status_code == 401
        assert portal.portal_packs(x_auth_token=None).status_code == 401
        assert portal.portal_buy_pack({}, x_auth_token=None).status_code == 401
        assert portal.portal_profile({}, x_auth_token=None).status_code == 401


class TestProviderCreditHook:
    """服务商侧：用官网 Key 提交重建要有额度，没额度就 402。"""

    def test_api_key_submission_requires_budget_when_enforced(self, store, monkeypatch):
        """正式计费（billing_enforced）时：有额度才放行。

        注意现在**不在入队时扣**：一次重建的实际用量只有跑完才知道，
        所以入队前只看 ``can_start``，扣减统一在 ``record_usage`` 里做。
        """
        import asyncio

        import server

        monkeypatch.setattr(api_keys, "_store", store)
        monkeypatch.setattr(store, "billing_enforced", lambda: True)
        store.purchase("alice", "personal")
        submitted = {}

        class _FakeQueue:
            def submit(self, **kwargs):
                submitted.update(kwargs)
                return type(
                    "T", (), {"task_id": "t1", "status": "queued", "queue_pos": 0}
                )()

        monkeypatch.setattr(server, "task_queue", _FakeQueue())
        token = server._REQUEST_API_KEY_USER.set("alice")
        try:
            resp = asyncio.run(
                server.create_task(files=[_FakeUpload()], **_FORM_ARGS)
            )
        finally:
            server._REQUEST_API_KEY_USER.reset(token)
        assert resp.status_code == 202
        assert submitted["owner"] == "user:alice"
        assert submitted["metered"] is True
        assert store.account("alice")["credits"] == 100  # 入队不预扣

    def test_out_of_credits_is_402(self, store, monkeypatch):
        import asyncio

        import server

        monkeypatch.setattr(api_keys, "_store", store)
        monkeypatch.setattr(store, "billing_enforced", lambda: True)
        store.ensure_account("bob")
        called = {"submit": False}

        class _FakeQueue:
            def submit(self, **kwargs):
                called["submit"] = True
                raise AssertionError("不该入队")

        monkeypatch.setattr(server, "task_queue", _FakeQueue())
        token = server._REQUEST_API_KEY_USER.set("bob")
        try:
            resp = asyncio.run(
                server.create_task(files=[_FakeUpload()], **_FORM_ARGS)
            )
        finally:
            server._REQUEST_API_KEY_USER.reset(token)
        assert resp.status_code == 402
        assert called["submit"] is False

    def test_beta_free_does_not_block(self, store, monkeypatch):
        """验证阶段：没次数也放行（用量照记）。"""
        import asyncio

        import server

        monkeypatch.setattr(api_keys, "_store", store)
        store.ensure_account("carol")

        class _FakeQueue:
            def submit(self, **kwargs):
                return type(
                    "T", (), {"task_id": "t3", "status": "queued", "queue_pos": 0}
                )()

        monkeypatch.setattr(server, "task_queue", _FakeQueue())
        token = server._REQUEST_API_KEY_USER.set("carol")
        try:
            resp = asyncio.run(
                server.create_task(files=[_FakeUpload()], **_FORM_ARGS)
            )
        finally:
            server._REQUEST_API_KEY_USER.reset(token)
        assert resp.status_code == 202

    def test_metering_records_points_and_cents(self, store, monkeypatch):
        """重建完成后按点数记账（200 点 = 1 分）。"""
        import server

        monkeypatch.setattr(api_keys, "_store", store)
        task = type(
            "T",
            (),
            {
                "task_id": "t9",
                "owner": "user:alice",
                "metered": True,
                "is_video": False,
                "created_at": 0.0,
                "result": {"num_points": 450},
                "ply_bytes": None,
            },
        )()
        monkeypatch.setattr(
            server, "_task_processor_impl", lambda task, update: None
        )
        server._task_processor(task, lambda *a: None)
        usage = store.usage_summary("alice")
        assert usage["calls"] == 1
        assert usage["points"] == 450
        assert usage["cents"] == 3

    def test_anonymous_is_not_charged(self, store, monkeypatch):
        import asyncio

        import server

        monkeypatch.setattr(api_keys, "_store", store)
        submitted = {}

        class _FakeQueue:
            def submit(self, **kwargs):
                submitted.update(kwargs)
                return type(
                    "T", (), {"task_id": "t2", "status": "queued", "queue_pos": 0}
                )()

        monkeypatch.setattr(server, "task_queue", _FakeQueue())
        token = server._REQUEST_API_KEY_USER.set(None)
        try:
            resp = asyncio.run(
                server.create_task(
                    files=[_FakeUpload()], client_id="c9", **_FORM_ARGS
                )
            )
        finally:
            server._REQUEST_API_KEY_USER.reset(token)
        assert resp.status_code == 202
        assert submitted["owner"] == "anon:c9"
        assert submitted["metered"] is False

    def test_credit_check_happens_before_queueing(self):
        """顺序：文件读好 → 查额度 → 入队（参数不对不消耗额度）。"""
        import inspect

        import server

        src = inspect.getsource(server.create_task)
        assert src.index("can_start") < src.index("task_queue.submit")
        assert src.index("未收到文件") < src.index("can_start")

    def test_metering_charges_the_plan_pack_first(self, store, monkeypatch):
        """重建完成后：优先扣计划包（整包 1 次），并写进流水。"""
        import server

        monkeypatch.setattr(api_keys, "_store", store)
        store.purchase("alice", "personal")
        task = type(
            "T",
            (),
            {
                "task_id": "t50",
                "owner": "user:alice",
                "metered": True,
                "is_video": False,
                "created_at": 0.0,
                "result": {"num_points": 50_000},
                "ply_bytes": None,
            },
        )()
        monkeypatch.setattr(
            server, "_task_processor_impl", lambda task, update: None
        )
        server._task_processor(task, lambda *a: None)
        assert store.account("alice")["credits"] == 99
        usage = store.usage_summary("alice")
        assert usage["points"] == 50_000
        assert usage["paid_cents"] == 0  # 计划包盖住了，没动余额
        entry = store.ledger("alice")[0]
        assert entry["kind"] == "usage" and "计划包" in entry["note"]


class _FakeUpload:
    """最小可用的 UploadFile 替身（只需要 .read() 与 .filename）。"""

    filename = "frame.png"

    async def read(self) -> bytes:
        return b"not-a-real-png"


# 直接调用端点函数时，Form 参数必须显式给（默认值是 FastAPI 的 Form 对象）
_FORM_ARGS = {
    "resolution": 512,
    "intrinsics": "null",
    "extrinsics": "null",
    "is_video": "false",
    "frame_count": 16,
}
