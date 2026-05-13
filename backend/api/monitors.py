from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
from datetime import datetime, timedelta
import httpx
import json

from models import Monitor, CheckResult, Alert
from schemas import MonitorCreate, MonitorUpdate, MonitorResponse, CheckResultResponse
from database import get_session
from tasks import add_monitor_job, remove_monitor_job

router = APIRouter()


@router.post("/monitors", response_model=MonitorResponse)
async def create_monitor(monitor: MonitorCreate, session: AsyncSession = Depends(get_session)):
    db_monitor = Monitor(**monitor.model_dump())
    session.add(db_monitor)
    await session.commit()
    await session.refresh(db_monitor)

    if db_monitor.is_active:
        add_monitor_job(db_monitor.id, db_monitor.interval_seconds)

    return db_monitor


@router.get("/monitors", response_model=List[MonitorResponse])
async def get_monitors(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Monitor).order_by(Monitor.created_at.desc()))
    monitors = result.scalars().all()
    return monitors


@router.get("/monitors/{monitor_id}", response_model=MonitorResponse)
async def get_monitor(monitor_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Monitor).where(Monitor.id == monitor_id))
    monitor = result.scalar_one_or_none()
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return monitor


@router.put("/monitors/{monitor_id}", response_model=MonitorResponse)
async def update_monitor(monitor_id: int, monitor_update: MonitorUpdate, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Monitor).where(Monitor.id == monitor_id))
    monitor = result.scalar_one_or_none()
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")

    update_data = monitor_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(monitor, key, value)

    await session.commit()
    await session.refresh(monitor)

    if "is_active" in update_data or "interval_seconds" in update_data:
        remove_monitor_job(monitor_id)
        if monitor.is_active:
            add_monitor_job(monitor.id, monitor.interval_seconds)

    return monitor


@router.delete("/monitors/{monitor_id}")
async def delete_monitor(monitor_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Monitor).where(Monitor.id == monitor_id))
    monitor = result.scalar_one_or_none()
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")

    remove_monitor_job(monitor_id)

    await session.execute(delete(CheckResult).where(CheckResult.monitor_id == monitor_id))
    await session.execute(delete(Alert).where(Alert.monitor_id == monitor_id))
    await session.execute(delete(Monitor).where(Monitor.id == monitor_id))
    await session.commit()

    return {"message": "Monitor deleted successfully"}


@router.get("/monitors/{monitor_id}/checks", response_model=List[CheckResultResponse])
async def get_monitor_checks(
    monitor_id: int,
    minutes: int = Query(default=60, ge=1, le=43200),
    session: AsyncSession = Depends(get_session)
):
    since = datetime.utcnow() - timedelta(minutes=minutes)
    result = await session.execute(
        select(CheckResult)
        .where(CheckResult.monitor_id == monitor_id)
        .where(CheckResult.checked_at >= since)
        .order_by(CheckResult.checked_at.desc())
    )
    checks = result.scalars().all()
    return checks


@router.post("/test-request")
async def test_request(request: Request):
    body = await request.json()
    url = body.get("url")
    method = body.get("method", "GET")
    headers = body.get("headers", {})
    req_body = body.get("body")
    timeout = body.get("timeout", 30)

    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    try:
        async with httpx.AsyncClient(timeout=float(timeout)) as client:
            start_time = datetime.utcnow()
            kwargs = {
                "method": method,
                "url": url,
                "headers": headers if headers else None,
            }
            if req_body and method in ["POST", "PUT", "PATCH"]:
                kwargs["content"] = req_body.encode() if isinstance(req_body, str) else req_body

            response = await client.request(**kwargs)
            elapsed_ms = (datetime.utcnow() - start_time).total_seconds() * 1000

            response_headers = {}
            for key, value in response.headers.items():
                response_headers[key] = value

            return JSONResponse({
                "status": response.status_code,
                "status_text": response.reason_phrase,
                "headers": response_headers,
                "body": response.text,
                "time_ms": round(elapsed_ms, 2),
                "size": len(response.content),
            })
    except httpx.TimeoutException:
        raise HTTPException(status_code=408, detail="Request timeout")
    except httpx.RequestError as e:
        raise HTTPException(status_code=500, detail=f"Request failed: {str(e)}")