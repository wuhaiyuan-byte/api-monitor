"""Tests for the OSS file monitor module.

Covers:
- CRUD endpoints (/api/oss-monitors)
- Check history endpoint
- Check-now + ad-hoc test-connection endpoints
- check_oss_monitor core: matched / not_matched / expected_present=False hit / scan_truncated / error
- Recovery flow (good after N consecutive failures)
- Fernet encrypt/decrypt round-trip

Strategy:
- Isolated in-memory SQLite engine.
- Stub out the global scheduler / email sender / broadcast to avoid
  touching real OSS / SMTP / WebSocket.
- Monkeypatch oss2.Bucket inside oss_tasks so no real network call is made.
"""
import sys
import os
import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import (
    create_async_engine, async_sessionmaker, AsyncSession,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oss_models import Base as OssBase, OssMonitor, OssCheckResult, SCAN_LIMIT
from oss_crypto import encrypt_secret, decrypt_secret, mask_secret


TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionMaker = async_sessionmaker(
    test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


# ---------------------------------------------------------------------------
# Build a small app that exposes the OSS router only, so the test does not
# pull in the real HTTP-monitor app (which would try to start its own
# scheduler / lifespan). The OSS router shares async_session_maker at module
# import time, so we patch it BEFORE we import the router.
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture(scope="function")
async def setup_db():
    # Force a fresh DB module whose async_session_maker points at the test engine.
    import database as db_module
    db_module.engine = test_engine
    db_module.async_session_maker = TestSessionMaker

    # Recreate OSS tables in the test engine.
    async with test_engine.begin() as conn:
        await conn.run_sync(OssBase.metadata.drop_all)
        await conn.run_sync(OssBase.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(OssBase.metadata.drop_all)


@pytest_asyncio.fixture
async def session(setup_db):
    async with TestSessionMaker() as s:
        yield s


@pytest_asyncio.fixture
async def app_with_oss():
    """Minimal FastAPI app exposing the OSS router, with the real
    get_session dependency overridden to use the in-memory engine.
    """
    from fastapi import FastAPI, Depends
    from api.oss import router as oss_router
    from database import get_session

    app = FastAPI()
    app.include_router(oss_router, prefix="/api")

    async def _override():
        async with TestSessionMaker() as s:
            yield s

    app.dependency_overrides[get_session] = _override

    # Stub the global scheduler to a no-op so adding/removing jobs is harmless.
    import oss_tasks as ot
    fake_sched = MagicMock()
    fake_sched.get_job.return_value = None
    ot.scheduler = fake_sched

    # Stub send_alert_email to a no-op async so we don't hit SMTP.
    import tasks as t_mod
    t_mod.send_alert_email = MagicMock(
        return_value=asyncio.sleep(0, result=None)
    )

    yield app

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app_with_oss):
    async with AsyncClient(
        transport=ASGITransport(app=app_with_oss),
        base_url="http://test",
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Crypto round-trip
# ---------------------------------------------------------------------------
def test_crypto_round_trip():
    plain = "my-very-secret-access-key-12345678"
    cipher = encrypt_secret(plain)
    assert cipher != plain
    assert decrypt_secret(cipher) == plain


def test_mask_secret():
    assert mask_secret("abcdefgh") == "***efgh"
    assert mask_secret("ab") == "***"
    assert mask_secret("") == ""
    assert mask_secret(None) == ""


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_oss_monitor(client: AsyncClient):
    r = await client.post("/api/oss-monitors", json={
        "name": "Daily Export",
        "provider": "aliyun",
        "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
        "bucket": "my-bucket",
        "region": "cn-hangzhou",
        "prefix": "exports/daily/",
        "keyword": "report",
        "match_mode": "contains",
        "expected_present": True,
        "failure_threshold": 2,
        "interval_seconds": 300,
        "is_active": True,
        "access_key_id": "AKIDTEST",
        "access_key_secret": "sk-secret-1234567890",
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["name"] == "Daily Export"
    assert data["access_key_id"] == "AKIDTEST"
    # The plaintext secret must never appear in the response.
    assert "sk-secret-1234567890" not in r.text
    assert "***" in data["access_key_secret_masked"]


@pytest.mark.asyncio
async def test_list_oss_monitors(client: AsyncClient):
    for i in range(3):
        await client.post("/api/oss-monitors", json={
            "name": f"M{i}",
            "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
            "bucket": "b",
            "keyword": "k",
            "access_key_id": "ak",
            "access_key_secret": "sk-1234567890",
        })
    r = await client.get("/api/oss-monitors")
    assert r.status_code == 200
    assert len(r.json()) == 3


@pytest.mark.asyncio
async def test_update_oss_monitor(client: AsyncClient):
    r = await client.post("/api/oss-monitors", json={
        "name": "Old",
        "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
        "bucket": "b",
        "keyword": "k",
        "access_key_id": "ak",
        "access_key_secret": "sk-1234567890",
    })
    mid = r.json()["id"]
    r2 = await client.put(f"/api/oss-monitors/{mid}", json={"name": "New", "interval_seconds": 60})
    assert r2.status_code == 200
    assert r2.json()["name"] == "New"
    assert r2.json()["interval_seconds"] == 60


@pytest.mark.asyncio
async def test_delete_oss_monitor(client: AsyncClient):
    r = await client.post("/api/oss-monitors", json={
        "name": "X",
        "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
        "bucket": "b",
        "keyword": "k",
        "access_key_id": "ak",
        "access_key_secret": "sk-1234567890",
    })
    mid = r.json()["id"]
    rd = await client.delete(f"/api/oss-monitors/{mid}")
    assert rd.status_code == 200
    rg = await client.get(f"/api/oss-monitors/{mid}")
    assert rg.status_code == 404


# ---------------------------------------------------------------------------
# check_oss_monitor core (mocked bucket)
# ---------------------------------------------------------------------------
def _make_obj(key, size=10, last_modified=None):
    if last_modified is None:
        last_modified = datetime(2024, 1, 1, 12, 0, 0)
    return SimpleNamespace(
        key=key, size=size, last_modified=last_modified, etag="x",
    )


class _FakeIterator:
    """Async-iterable wrapper that mimics oss2.ObjectIteratorV2 with
    a max_keys cap.
    """
    def __init__(self, objs, max_keys):
        self._objs = objs
        self._max = max_keys

    def __aiter__(self):
        self._i = 0
        return self

    async def __anext__(self):
        if self._i >= self._max or self._i >= len(self._objs):
            raise StopAsyncIteration
        o = self._objs[self._i]
        self._i += 1
        return o


def _patched_bucket(objs, max_keys=SCAN_LIMIT + 1):
    fake_bucket = MagicMock()
    fake_bucket.__iter__ = lambda self: _FakeIterator(objs, max_keys).__aiter__()
    return fake_bucket


@pytest.mark.asyncio
async def test_check_monitor_matched(session):
    import oss_tasks as ot
    import tasks as t_mod
    t_mod.send_alert_email = MagicMock(return_value=asyncio.sleep(0, result=None))
    ot.scheduler = MagicMock()

    m = OssMonitor(
        name="M", provider="aliyun",
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        bucket="b", keyword="report",
        access_key_id="ak",
        access_key_secret_enc=encrypt_secret("sk"),
        is_active=True, interval_seconds=60,
        expected_present=True, failure_threshold=2,
    )
    session.add(m)
    await session.commit()
    await session.refresh(m)

    objs = [
        _make_obj("exports/daily/foo.txt"),
        _make_obj("exports/daily/report_2024.csv"),
    ]
    with patch.object(ot.oss2, "Bucket", return_value=_patched_bucket(objs)):
        await ot.check_oss_monitor(m.id)

    res = await session.execute(
        __import__("sqlalchemy").select(OssCheckResult).where(
            OssCheckResult.oss_monitor_id == m.id
        )
    )
    rows = res.scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "matched"
    assert "report" in rows[0].matched_key
    assert rows[0].scan_truncated is False

    # Snapshot updated
    await session.refresh(m)
    assert m.last_status == "matched"
    assert m.consecutive_failures == 0


@pytest.mark.asyncio
async def test_check_monitor_not_matched_threshold(session):
    import oss_tasks as ot
    import tasks as t_mod
    t_mod.send_alert_email = MagicMock(return_value=asyncio.sleep(0, result=None))
    ot.scheduler = MagicMock()

    m = OssMonitor(
        name="M", provider="aliyun",
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        bucket="b", keyword="missing",
        access_key_id="ak",
        access_key_secret_enc=encrypt_secret("sk"),
        is_active=True, interval_seconds=60,
        expected_present=True, failure_threshold=2,
    )
    session.add(m)
    await session.commit()
    await session.refresh(m)

    objs = [_make_obj("foo.txt"), _make_obj("bar.txt")]

    # First check: not matched, consecutive=1, threshold=2 -> NOT fired.
    with patch.object(ot.oss2, "Bucket", return_value=_patched_bucket(objs)):
        await ot.check_oss_monitor(m.id)
    # Second check: not matched, consecutive=2, threshold=2 -> fired.
    with patch.object(ot.oss2, "Bucket", return_value=_patched_bucket(objs)):
        await ot.check_oss_monitor(m.id)

    await session.refresh(m)
    assert m.consecutive_failures == 2
    # Email is invoked when threshold is reached.
    assert t_mod.send_alert_email.call_count >= 1
    # The alert_type arg should be 'oss_missing'.
    types_called = [c.args[1] for c in t_mod.send_alert_email.call_args_list]
    assert "oss_missing" in types_called


@pytest.mark.asyncio
async def test_check_monitor_recovery(session):
    import oss_tasks as ot
    import tasks as t_mod
    t_mod.send_alert_email = MagicMock(return_value=asyncio.sleep(0, result=None))
    ot.scheduler = MagicMock()

    m = OssMonitor(
        name="M", provider="aliyun",
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        bucket="b", keyword="k",
        access_key_id="ak",
        access_key_secret_enc=encrypt_secret("sk"),
        is_active=True, interval_seconds=60,
        expected_present=True, failure_threshold=1,
    )
    session.add(m)
    await session.commit()
    await session.refresh(m)

    # First: failure (threshold=1, fires immediately)
    with patch.object(ot.oss2, "Bucket", return_value=_patched_bucket([_make_obj("nothere.txt")])):
        await ot.check_oss_monitor(m.id)
    # Second: success
    with patch.object(ot.oss2, "Bucket", return_value=_patched_bucket([_make_obj("k.txt")])):
        await ot.check_oss_monitor(m.id)

    await session.refresh(m)
    assert m.consecutive_failures == 0
    # Recovery email sent with is_recovery=True
    recovery_calls = [c for c in t_mod.send_alert_email.call_args_list if c.kwargs.get("is_recovery")]
    assert len(recovery_calls) >= 1


@pytest.mark.asyncio
async def test_check_monitor_max_age_hours_freshness_fail(session):
    """When max_age_hours is set, a matched file whose last_modified is older
    than the window should be treated as not_matched with stale= True."""
    import oss_tasks as ot
    import tasks as t_mod
    t_mod.send_alert_email = MagicMock(return_value=asyncio.sleep(0, result=None))
    ot.scheduler = MagicMock()

    m = OssMonitor(
        name="M", provider="aliyun",
        endpoint="https://oss-cn-shanghai.aliyuncs.com",
        bucket="b", keyword="report",
        access_key_id="ak",
        access_key_secret_enc=encrypt_secret("sk"),
        is_active=True, interval_seconds=60,
        expected_present=True, failure_threshold=1,
        prefix="exports/",
        max_age_hours=24,
    )
    session.add(m)
    await session.commit()
    await session.refresh(m)

    # File is 3 days old, freshness window is 24h → must be stale
    from datetime import timedelta
    old_dt = datetime.utcnow() - timedelta(days=3)
    objs = [_make_obj("exports/report_2024.csv", last_modified=old_dt)]
    with patch.object(ot.oss2, "Bucket", return_value=_patched_bucket(objs)):
        await ot.check_oss_monitor(m.id)

    await session.refresh(m)
    assert m.last_status == "not_matched"
    assert "陈旧" in (m.last_error or "")
    types_called = [c.args[1] for c in t_mod.send_alert_email.call_args_list]
    assert "oss_missing" in types_called


@pytest.mark.asyncio
async def test_check_monitor_max_age_hours_within_window_passes(session):
    """When max_age_hours is set and the file IS fresh, status should be matched."""
    import oss_tasks as ot
    import tasks as t_mod
    t_mod.send_alert_email = MagicMock(return_value=asyncio.sleep(0, result=None))
    ot.scheduler = MagicMock()

    m = OssMonitor(
        name="M", provider="aliyun",
        endpoint="https://oss-cn-shanghai.aliyuncs.com",
        bucket="b", keyword="report",
        access_key_id="ak",
        access_key_secret_enc=encrypt_secret("sk"),
        is_active=True, interval_seconds=60,
        expected_present=True, failure_threshold=2,
        prefix="exports/",
        max_age_hours=24,
    )
    session.add(m)
    await session.commit()
    await session.refresh(m)

    from datetime import timedelta
    fresh_dt = datetime.utcnow() - timedelta(hours=1)
    objs = [_make_obj("exports/report_today.csv", last_modified=fresh_dt)]
    with patch.object(ot.oss2, "Bucket", return_value=_patched_bucket(objs)):
        await ot.check_oss_monitor(m.id)

    await session.refresh(m)
    assert m.last_status == "matched"
    assert m.consecutive_failures == 0


@pytest.mark.asyncio
async def test_create_oss_monitor_requires_prefix(client: AsyncClient):
    """Empty prefix must be rejected by Pydantic validation."""
    r = await client.post("/api/oss-monitors", json={
        "name": "no-prefix",
        "endpoint": "https://oss-cn-shanghai.aliyuncs.com",
        "bucket": "b",
        "keyword": "k",
        "prefix": "",
        "access_key_id": "ak",
        "access_key_secret": "sk-1234567890",
    })
    assert r.status_code == 422
    # The error mentions prefix in the validation message
    body = r.text
    assert "prefix" in body.lower()


@pytest.mark.asyncio
async def test_check_monitor_expected_absent(session):
    import oss_tasks as ot
    import tasks as t_mod
    t_mod.send_alert_email = MagicMock(return_value=asyncio.sleep(0, result=None))
    ot.scheduler = MagicMock()

    m = OssMonitor(
        name="M", provider="aliyun",
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        bucket="b", keyword="secret",
        access_key_id="ak",
        access_key_secret_enc=encrypt_secret("sk"),
        is_active=True, interval_seconds=60,
        expected_present=False, failure_threshold=1,
    )
    session.add(m)
    await session.commit()
    await session.refresh(m)

    # An unexpected file shows up -> not_matched (anomaly) -> fired immediately.
    objs = [_make_obj("leaked_secret.txt")]
    with patch.object(ot.oss2, "Bucket", return_value=_patched_bucket(objs)):
        await ot.check_oss_monitor(m.id)

    await session.refresh(m)
    assert m.consecutive_failures == 1
    types_called = [c.args[1] for c in t_mod.send_alert_email.call_args_list]
    assert "oss_unexpected" in types_called


@pytest.mark.asyncio
async def test_check_monitor_scan_truncated(session):
    import oss_tasks as ot
    import tasks as t_mod
    t_mod.send_alert_email = MagicMock(return_value=asyncio.sleep(0, result=None))
    ot.scheduler = MagicMock()

    m = OssMonitor(
        name="M", provider="aliyun",
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        bucket="b", keyword="never_matches_xyz",
        access_key_id="ak",
        access_key_secret_enc=encrypt_secret("sk"),
        is_active=True, interval_seconds=60,
        expected_present=True, failure_threshold=1,
    )
    session.add(m)
    await session.commit()
    await session.refresh(m)

    # Build SCAN_LIMIT+5 objects so the loop bails.
    objs = [_make_obj(f"file_{i}.txt") for i in range(SCAN_LIMIT + 5)]
    with patch.object(ot.oss2, "Bucket", return_value=_patched_bucket(objs)):
        await ot.check_oss_monitor(m.id)

    res = await session.execute(
        __import__("sqlalchemy").select(OssCheckResult).where(
            OssCheckResult.oss_monitor_id == m.id
        )
    )
    row = res.scalar_one()
    assert row.scan_truncated is True
    assert row.scanned_count == SCAN_LIMIT
    assert row.error_message and "扫描超过" in row.error_message


@pytest.mark.asyncio
async def test_check_monitor_oss_error(session):
    import oss_tasks as ot
    import tasks as t_mod
    t_mod.send_alert_email = MagicMock(return_value=asyncio.sleep(0, result=None))
    ot.scheduler = MagicMock()

    m = OssMonitor(
        name="M", provider="aliyun",
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        bucket="b", keyword="k",
        access_key_id="ak",
        access_key_secret_enc=encrypt_secret("sk"),
        is_active=True, interval_seconds=60,
        expected_present=True, failure_threshold=1,
    )
    session.add(m)
    await session.commit()
    await session.refresh(m)

    fake_bucket = MagicMock()
    fake_bucket.__iter__ = lambda self: (_ for _ in ()).throw(
        Exception("network boom")
    )
    with patch.object(ot.oss2, "Bucket", return_value=fake_bucket):
        await ot.check_oss_monitor(m.id)

    res = await session.execute(
        __import__("sqlalchemy").select(OssCheckResult).where(
            OssCheckResult.oss_monitor_id == m.id
        )
    )
    row = res.scalar_one()
    assert row.status == "error"
    assert "network boom" in (row.error_message or "")


# ---------------------------------------------------------------------------
# Test connection endpoint
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_test_connection_ok(client: AsyncClient):
    import oss_tasks as ot
    fake_bucket = MagicMock()
    fake_bucket.__iter__ = lambda self: iter([_make_obj("hello.txt")])
    with patch.object(ot.oss2, "Bucket", return_value=fake_bucket):
        r = await client.post("/api/oss-monitors/test-connection", json={
            "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
            "bucket": "b",
            "access_key_id": "ak",
            "access_key_secret": "sk-1234567890",
            "prefix": "",
        })
    assert r.status_code == 200
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_test_connection_fail(client: AsyncClient):
    import oss_tasks as ot
    fake_bucket = MagicMock()
    fake_bucket.__iter__ = lambda self: (_ for _ in ()).throw(Exception("auth failed"))
    with patch.object(ot.oss2, "Bucket", return_value=fake_bucket):
        r = await client.post("/api/oss-monitors/test-connection", json={
            "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
            "bucket": "b",
            "access_key_id": "ak",
            "access_key_secret": "sk-1234567890",
            "prefix": "",
        })
    assert r.status_code == 400
    assert "auth failed" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Check-now + history endpoint
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_check_now_and_history(client: AsyncClient):
    import oss_tasks as ot
    import tasks as t_mod
    t_mod.send_alert_email = MagicMock(return_value=asyncio.sleep(0, result=None))
    ot.scheduler = MagicMock()

    r = await client.post("/api/oss-monitors", json={
        "name": "M",
        "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
        "bucket": "b",
        "keyword": "k",
        "access_key_id": "ak",
        "access_key_secret": "sk-1234567890",
    })
    mid = r.json()["id"]

    fake_bucket = MagicMock()
    fake_bucket.__iter__ = lambda self: iter([_make_obj("k_2024.txt")])
    with patch.object(ot.oss2, "Bucket", return_value=fake_bucket):
        rc = await client.post(f"/api/oss-monitors/{mid}/check-now")
    assert rc.status_code == 200

    rh = await client.get(f"/api/oss-monitors/{mid}/checks?minutes=60")
    assert rh.status_code == 200
    assert len(rh.json()) == 1
    assert rh.json()[0]["status"] == "matched"
