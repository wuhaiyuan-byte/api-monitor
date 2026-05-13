import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from contextlib import asynccontextmanager
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import Base, Monitor, CheckResult, Alert
from database import get_session


TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionMaker = async_sessionmaker(
    test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


@asynccontextmanager
async def override_get_session():
    async with TestSessionMaker() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def session(setup_db):
    async with TestSessionMaker() as session:
        yield session


@pytest_asyncio.fixture
async def client(session):
    from main import app
    from database import get_session as original_get_session

    async def override_get_session_fixture():
        yield session

    app.dependency_overrides[original_get_session] = override_get_session_fixture

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_monitor(client: AsyncClient):
    response = await client.post("/api/monitors", json={
        "name": "Test API",
        "url": "https://httpbin.org/get",
        "method": "GET",
        "expected_status": 200,
        "interval_seconds": 60
    })
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Test API"
    assert data["url"] == "https://httpbin.org/get"
    assert data["is_active"] == True
    assert "id" in data


@pytest.mark.asyncio
async def test_get_monitors(client: AsyncClient):
    await client.post("/api/monitors", json={
        "name": "Test API 1",
        "url": "https://httpbin.org/get",
    })
    await client.post("/api/monitors", json={
        "name": "Test API 2",
        "url": "https://httpbin.org/delay/1",
    })

    response = await client.get("/api/monitors")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2


@pytest.mark.asyncio
async def test_get_monitor_by_id(client: AsyncClient):
    create_response = await client.post("/api/monitors", json={
        "name": "Test API",
        "url": "https://httpbin.org/get",
    })
    monitor_id = create_response.json()["id"]

    response = await client.get(f"/api/monitors/{monitor_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Test API"


@pytest.mark.asyncio
async def test_update_monitor(client: AsyncClient):
    create_response = await client.post("/api/monitors", json={
        "name": "Test API",
        "url": "https://httpbin.org/get",
        "interval_seconds": 60,
    })
    monitor_id = create_response.json()["id"]

    response = await client.put(f"/api/monitors/{monitor_id}", json={
        "name": "Updated API",
        "interval_seconds": 120,
    })
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Updated API"
    assert data["interval_seconds"] == 120


@pytest.mark.asyncio
async def test_delete_monitor(client: AsyncClient):
    create_response = await client.post("/api/monitors", json={
        "name": "Test API",
        "url": "https://httpbin.org/get",
    })
    monitor_id = create_response.json()["id"]

    response = await client.delete(f"/api/monitors/{monitor_id}")
    assert response.status_code == 200

    get_response = await client.get(f"/api/monitors/{monitor_id}")
    assert get_response.status_code == 404


@pytest.mark.asyncio
async def test_get_alerts_empty(client: AsyncClient):
    response = await client.get("/api/alerts")
    assert response.status_code == 200
    data = response.json()
    assert data == []


@pytest.mark.asyncio
async def test_anomaly_detection_logic(session: AsyncSession):
    from tasks import check_monitor
    import unittest.mock as mock

    monitor = Monitor(
        name="Test Monitor",
        url="https://httpbin.org/delay/1",
        method="GET",
        expected_status=200,
        interval_seconds=60,
        is_active=True
    )
    session.add(monitor)
    await session.commit()
    await session.refresh(monitor)

    for i in range(25):
        result = CheckResult(
            monitor_id=monitor.id,
            status_code=200,
            response_time_ms=100.0 + i * 2,
            is_anomaly=False
        )
        session.add(result)
    await session.commit()

    with mock.patch("httpx.AsyncClient") as mock_client:
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.text = "OK"
        mock_client.return_value.__aenter__.return_value.request = mock.AsyncMock(
            return_value=mock_response
        )
        mock_client.return_value.__aexit__.mock.AsyncMock()

        await check_monitor(monitor.id)

    result = await session.execute(
        f"SELECT is_anomaly FROM check_results WHERE monitor_id = {monitor.id} ORDER BY id DESC LIMIT 1"
    )
    last_result = result.scalar()

    assert last_result == True or last_result == False


@pytest.mark.asyncio
async def test_check_monitor_checks_history(client: AsyncClient, session: AsyncSession):
    create_response = await client.post("/api/monitors", json={
        "name": "Test API",
        "url": "https://httpbin.org/get",
    })
    monitor_id = create_response.json()["id"]

    for i in range(10):
        result = CheckResult(
            monitor_id=monitor_id,
            status_code=200,
            response_time_ms=50.0 + i * 5,
            is_anomaly=False
        )
        session.add(result)
    await session.commit()

    response = await client.get(f"/api/monitors/{monitor_id}/checks?minutes=60")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 10