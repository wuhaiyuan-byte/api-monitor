from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional

from models import Alert, Monitor
from schemas import AlertResponse
from database import get_session

router = APIRouter()


@router.get("/alerts", response_model=List[AlertResponse])
async def get_alerts(
    is_resolved: Optional[bool] = Query(default=None),
    session: AsyncSession = Depends(get_session)
):
    query = select(Alert, Monitor.name).join(Monitor).order_by(Alert.created_at.desc())
    if is_resolved is not None:
        query = query.where(Alert.is_resolved == is_resolved)

    result = await session.execute(query)
    rows = result.all()

    alerts = []
    for alert, monitor_name in rows:
        alert_dict = {
            "id": alert.id,
            "monitor_id": alert.monitor_id,
            "alert_type": alert.alert_type,
            "description": alert.description,
            "is_resolved": alert.is_resolved,
            "created_at": alert.created_at,
            "monitor_name": monitor_name
        }
        alerts.append(AlertResponse(**alert_dict))

    return alerts


@router.put("/alerts/{alert_id}/resolve")
async def resolve_alert(alert_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    alert.is_resolved = True
    await session.commit()

    return {"message": "Alert resolved successfully"}