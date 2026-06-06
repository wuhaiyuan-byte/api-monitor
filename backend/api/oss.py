"""OSS monitor CRUD + ad-hoc check + test-connection routes.

Mounted under /api by main.py. Does not import or modify the existing
monitors / alerts / settings routes.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
from datetime import datetime, timedelta
from pydantic import SecretStr

from oss_models import OssMonitor, OssCheckResult
from oss_schemas import (
    OssMonitorCreate, OssMonitorUpdate, OssMonitorResponse,
    OssCheckResultResponse, OssTestConnectionRequest,
)
from oss_crypto import encrypt_secret, decrypt_secret, mask_secret
from oss_tasks import (
    add_oss_monitor_job, remove_oss_monitor_job, check_oss_monitor,
    test_connection as oss_test_connection,
)
from database import get_session

router = APIRouter()


def _to_response(m: OssMonitor) -> OssMonitorResponse:
    # If the Fernet key has rotated (e.g. ephemeral key on a previous run),
    # decryption fails with InvalidToken. Return a locked marker instead of
    # raising 500 on the list endpoint.
    try:
        plain = decrypt_secret(m.access_key_secret_enc) or ""
    except Exception:
        return OssMonitorResponse(
            id=m.id,
            name=m.name,
            provider=m.provider,
            endpoint=m.endpoint,
            bucket=m.bucket,
            region=m.region,
            prefix=m.prefix or "",
            keyword=m.keyword,
            match_mode=m.match_mode,
            expected_present=m.expected_present,
            max_age_hours=m.max_age_hours,
            failure_threshold=m.failure_threshold,
            interval_seconds=m.interval_seconds,
            is_active=m.is_active,
            access_key_id=m.access_key_id,
            access_key_secret_masked="<unreadable — set OSS_ENC_KEY or re-save>",
            last_status=m.last_status,
            last_checked_at=m.last_checked_at,
            last_matched_key=m.last_matched_key,
            last_matched_size=m.last_matched_size,
            last_matched_modified=m.last_matched_modified,
            last_error=m.last_error,
            consecutive_failures=m.consecutive_failures or 0,
            created_at=m.created_at,
        )
    return OssMonitorResponse(
        id=m.id,
        name=m.name,
        provider=m.provider,
        endpoint=m.endpoint,
        bucket=m.bucket,
        region=m.region,
        prefix=m.prefix or "",
        keyword=m.keyword,
        match_mode=m.match_mode,
        expected_present=m.expected_present,
        max_age_hours=m.max_age_hours,
        failure_threshold=m.failure_threshold,
        interval_seconds=m.interval_seconds,
        is_active=m.is_active,
        access_key_id=m.access_key_id,
        access_key_secret_masked=mask_secret(plain),
        last_status=m.last_status,
        last_checked_at=m.last_checked_at,
        last_matched_key=m.last_matched_key,
        last_matched_size=m.last_matched_size,
        last_matched_modified=m.last_matched_modified,
        last_error=m.last_error,
        consecutive_failures=m.consecutive_failures or 0,
        created_at=m.created_at,
    )


@router.post("/oss-monitors", response_model=OssMonitorResponse)
async def create_oss_monitor(
    body: OssMonitorCreate,
    session: AsyncSession = Depends(get_session),
):
    secret_plain = body.access_key_secret.get_secret_value()
    db_obj = OssMonitor(
        name=body.name,
        provider=body.provider,
        endpoint=body.endpoint,
        bucket=body.bucket,
        region=body.region,
        prefix=body.prefix or "",
        keyword=body.keyword,
        match_mode=body.match_mode,
        expected_present=body.expected_present,
        max_age_hours=body.max_age_hours,
        failure_threshold=body.failure_threshold,
        interval_seconds=body.interval_seconds,
        is_active=body.is_active,
        access_key_id=body.access_key_id,
        access_key_secret_enc=encrypt_secret(secret_plain),
    )
    session.add(db_obj)
    await session.commit()
    await session.refresh(db_obj)

    if db_obj.is_active:
        add_oss_monitor_job(db_obj.id, db_obj.interval_seconds)

    return _to_response(db_obj)


@router.get("/oss-monitors", response_model=List[OssMonitorResponse])
async def list_oss_monitors(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(OssMonitor).order_by(OssMonitor.created_at.desc())
    )
    return [_to_response(m) for m in result.scalars().all()]


@router.get("/oss-monitors/{oss_monitor_id}", response_model=OssMonitorResponse)
async def get_oss_monitor(
    oss_monitor_id: int, session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(OssMonitor).where(OssMonitor.id == oss_monitor_id)
    )
    m = result.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="OSS monitor not found")
    return _to_response(m)


@router.put("/oss-monitors/{oss_monitor_id}", response_model=OssMonitorResponse)
async def update_oss_monitor(
    oss_monitor_id: int,
    body: OssMonitorUpdate,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(OssMonitor).where(OssMonitor.id == oss_monitor_id)
    )
    m = result.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="OSS monitor not found")

    update_data = body.model_dump(exclude_unset=True)
    new_secret: Optional[SecretStr] = update_data.pop("access_key_secret", None)
    for key, value in update_data.items():
        setattr(m, key, value)
    if new_secret is not None:
        m.access_key_secret_enc = encrypt_secret(new_secret.get_secret_value())

    await session.commit()
    await session.refresh(m)

    if "is_active" in update_data or "interval_seconds" in update_data:
        remove_oss_monitor_job(oss_monitor_id)
        if m.is_active:
            add_oss_monitor_job(m.id, m.interval_seconds)

    return _to_response(m)


@router.delete("/oss-monitors/{oss_monitor_id}")
async def delete_oss_monitor(
    oss_monitor_id: int, session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(OssMonitor).where(OssMonitor.id == oss_monitor_id)
    )
    m = result.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="OSS monitor not found")

    remove_oss_monitor_job(oss_monitor_id)
    await session.execute(
        delete(OssCheckResult).where(OssCheckResult.oss_monitor_id == oss_monitor_id)
    )
    await session.execute(
        delete(OssMonitor).where(OssMonitor.id == oss_monitor_id)
    )
    await session.commit()
    return {"message": "OSS monitor deleted"}


@router.get(
    "/oss-monitors/{oss_monitor_id}/checks",
    response_model=List[OssCheckResultResponse],
)
async def get_oss_monitor_checks(
    oss_monitor_id: int,
    minutes: int = Query(default=60, ge=1, le=43200),
    session: AsyncSession = Depends(get_session),
):
    since = datetime.utcnow() - timedelta(minutes=minutes)
    result = await session.execute(
        select(OssCheckResult)
        .where(OssCheckResult.oss_monitor_id == oss_monitor_id)
        .where(OssCheckResult.checked_at >= since)
        .order_by(OssCheckResult.checked_at.desc())
    )
    return result.scalars().all()


@router.post("/oss-monitors/{oss_monitor_id}/check-now")
async def trigger_check_now(
    oss_monitor_id: int, session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(OssMonitor).where(OssMonitor.id == oss_monitor_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="OSS monitor not found")
    await check_oss_monitor(oss_monitor_id)
    return {"message": "check triggered"}


@router.post("/oss-monitors/test-connection")
async def test_oss_connection(body: OssTestConnectionRequest):
    result = await oss_test_connection(
        endpoint=body.endpoint,
        bucket_name=body.bucket,
        access_key_id=body.access_key_id,
        access_key_secret=body.access_key_secret.get_secret_value(),
        prefix=body.prefix or "",
    )
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result
