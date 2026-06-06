"""OSS file monitor scheduled task module.

Reuses the global APScheduler instance, the WebSocket manager, the
async DB session factory, and the existing send_alert_email pipeline
from tasks.py. Does not import the HTTP monitor's check_monitor or any
of its private helpers. Broadcast event types are namespaced
'oss_status_update' / 'oss_new_alert' so the frontend can route them
separately without colliding with existing 'status_update' / 'new_alert'.
"""
import re
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

import oss2

from sqlalchemy import select
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from oss_models import OssMonitor, OssCheckResult, SCAN_LIMIT
from oss_crypto import decrypt_secret
from database import async_session_maker
from ws_manager import manager

# Reuse the existing scheduler, not a new one.
from tasks import scheduler, send_alert_email


logger = logging.getLogger("oss_tasks")


# ---------------------------------------------------------------------------
# OSS bucket construction
# ---------------------------------------------------------------------------
def _build_bucket(monitor: OssMonitor) -> oss2.Bucket:
    ak = monitor.access_key_id
    sk = decrypt_secret(monitor.access_key_secret_enc)
    auth = oss2.Auth(ak, sk)
    return oss2.Bucket(auth, monitor.endpoint, monitor.bucket)


def _build_bucket_from(
    endpoint: str,
    bucket: str,
    access_key_id: str,
    access_key_secret: str,
) -> oss2.Bucket:
    auth = oss2.Auth(access_key_id, access_key_secret)
    return oss2.Bucket(auth, endpoint, bucket)


# ---------------------------------------------------------------------------
# Scan core
# ---------------------------------------------------------------------------
async def _scan(
    bucket: oss2.Bucket,
    prefix: str,
    keyword: str,
    match_mode: str,
) -> Tuple[bool, Optional["oss2.ObjectSummary"], int, bool, Optional[str]]:
    """Run a single scan pass.

    Returns:
        (matched, first_match_summary, scanned_count, truncated, error_message)
    """
    try:
        scanned = 0
        truncated = False
        # oss2.ObjectIteratorV2 supports max_keys for safety.
        for obj in oss2.ObjectIteratorV2(bucket, prefix=prefix or "", max_keys=SCAN_LIMIT + 1):
            scanned += 1
            if scanned > SCAN_LIMIT:
                truncated = True
                break
            key = obj.key
            if match_mode == "regex":
                if re.search(keyword, key):
                    return True, obj, scanned, truncated, None
            else:  # 'contains'
                if keyword in key:
                    return True, obj, scanned, truncated, None
        return False, None, scanned, truncated, None
    except oss2.exceptions.OssError as e:
        return False, None, 0, False, f"OSS API error: {e.code} {e.message}"
    except Exception as e:  # network, auth, etc.
        return False, None, 0, False, str(e)[:500]


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def _record_check(
    session,
    monitor: OssMonitor,
    status: str,
    matched_key: Optional[str],
    file_size: Optional[float],
    file_last_modified: Optional[datetime],
    scanned_count: int,
    scan_truncated: bool,
    error_message: Optional[str],
):
    cr = OssCheckResult(
        oss_monitor_id=monitor.id,
        status=status,
        matched_key=matched_key,
        file_size=file_size,
        file_last_modified=file_last_modified,
        scanned_count=scanned_count,
        scan_truncated=scan_truncated,
        error_message=error_message,
        checked_at=datetime.utcnow(),
    )
    session.add(cr)
    return cr


def _alert_type_for(monitor: OssMonitor) -> str:
    return "oss_missing" if monitor.expected_present else "oss_unexpected"


def _alert_description(monitor: OssMonitor, status: str, stale: bool = False) -> str:
    if status == "not_matched":
        if stale:
            # File exists but its last_modified is older than max_age_hours.
            last_mod = monitor.last_matched_modified
            last_mod_str = (
                last_mod.strftime("%Y-%m-%d %H:%M:%S") if last_mod else "unknown"
            )
            return (
                f"OSS 文件陈旧: bucket={monitor.bucket} prefix={monitor.prefix} "
                f"keyword='{monitor.keyword}' 文件 last_modified={last_mod_str} "
                f"超过 {monitor.max_age_hours} 小时新鲜度窗口"
            )
        if monitor.expected_present:
            return (
                f"OSS 文件缺失: bucket={monitor.bucket} prefix={monitor.prefix} "
                f"keyword='{monitor.keyword}' 扫描 {SCAN_LIMIT} 条内未找到匹配"
            )
        else:
            return (
                f"OSS 文件异常出现: bucket={monitor.bucket} prefix={monitor.prefix} "
                f"keyword='{monitor.keyword}' 不应存在但扫描到"
            )
    if status == "error":
        return f"OSS 检查错误: {monitor.last_error or 'unknown'}"
    return "OSS 检查正常"


# ---------------------------------------------------------------------------
# Main scheduler entry point
# ---------------------------------------------------------------------------
async def check_oss_monitor(oss_monitor_id: int):
    async with async_session_maker() as session:
        result = await session.execute(
            select(OssMonitor).where(OssMonitor.id == oss_monitor_id)
        )
        monitor = result.scalar_one_or_none()
        if not monitor:
            return

        bucket = _build_bucket(monitor)
        matched, first, scanned, truncated, err = await _scan(
            bucket, monitor.prefix or "", monitor.keyword, monitor.match_mode
        )

        # Determine if the matched file is "stale" (older than max_age_hours).
        # Semantics: stale files are treated as "not present" for the purpose
        # of this check, regardless of expected_present direction.
        is_stale = False
        if matched and first and monitor.max_age_hours:
            cutoff = datetime.utcnow() - timedelta(hours=monitor.max_age_hours)
            fm = first.last_modified
            # oss2 returns naive datetime in UTC; compare in UTC.
            if fm is not None and fm < cutoff:
                is_stale = True

        if err:
            status = "error"
            matched_key = None
            file_size = None
            file_modified_dt = None
            error_msg = err
        elif monitor.expected_present:
            effective = matched and not is_stale
            status = "matched" if effective else "not_matched"
            matched_key = first.key if effective else None
            file_size = first.size if effective else None
            file_modified_dt = first.last_modified if effective else None
            error_msg = None
        else:
            # expected_present = False: alert only if a FRESH file exists.
            # A stale file does not count as "unexpectedly present".
            fresh_match = matched and not is_stale
            status = "not_matched" if fresh_match else "matched"
            matched_key = first.key if fresh_match else None
            file_size = first.size if fresh_match else None
            file_modified_dt = first.last_modified if fresh_match else None
            error_msg = None

        if truncated:
            error_msg = (
                (error_msg + "; " if error_msg else "")
                + f"扫描超过 {SCAN_LIMIT} 条提前终止，结果可能不完整"
            )

        # If stale was the cause, surface that into error_msg so the dashboard
        # shows the freshness reason even before any alert is fired.
        if is_stale and status == "not_matched" and monitor.expected_present:
            error_msg = (
                (error_msg + "; " if error_msg else "")
                + f"匹配项已陈旧 (max_age_hours={monitor.max_age_hours})"
            )

        # Persist history (kept forever per spec).
        cr = _record_check(
            session, monitor, status, matched_key, file_size, file_modified_dt,
            scanned, truncated, error_msg,
        )
        await session.commit()
        await session.refresh(cr)

        # Update denormalized snapshot.
        monitor.last_status = status
        monitor.last_checked_at = cr.checked_at
        monitor.last_matched_key = matched_key
        monitor.last_matched_size = file_size
        monitor.last_matched_modified = file_modified_dt
        monitor.last_error = error_msg

        # Anomaly = not the "good" outcome.
        # expected_present=True  -> good='matched'
        # expected_present=False -> good='matched' (no unexpected files)
        good = status == "matched"
        prev_consecutive = monitor.consecutive_failures or 0

        if good:
            monitor.consecutive_failures = 0
            # Recovery: previous run was a consecutive failure that hit threshold.
            if prev_consecutive >= (monitor.failure_threshold or 2):
                await send_alert_email(
                    monitor.name, "oss_recovery", "已恢复正常",
                    monitor_id=oss_monitor_id, is_recovery=True,
                )
        else:
            monitor.consecutive_failures = prev_consecutive + 1
            threshold = monitor.failure_threshold or 2
            is_fired = monitor.consecutive_failures >= threshold
            alert_type = _alert_type_for(monitor)
            base_desc = _alert_description(monitor, status, stale=is_stale)
            if is_fired:
                desc = base_desc
            else:
                desc = f"{base_desc} (累积 {monitor.consecutive_failures}/{threshold})"

            await manager.broadcast({
                "type": "oss_new_alert",
                "alert": {
                    "id": cr.id,
                    "oss_monitor_id": oss_monitor_id,
                    "alert_type": alert_type,
                    "description": desc,
                    "is_fired": is_fired,
                    "consecutive_failures": monitor.consecutive_failures,
                    "threshold": threshold,
                    "status": status,
                    "monitor_name": monitor.name,
                    "checked_at": cr.checked_at.isoformat(),
                },
            })

            if is_fired:
                await send_alert_email(
                    monitor.name, alert_type, base_desc,
                    monitor_id=oss_monitor_id,
                )

        await session.commit()

        await manager.broadcast({
            "type": "oss_status_update",
            "oss_monitor_id": oss_monitor_id,
            "status": status,
            "matched_key": matched_key,
            "scanned_count": scanned,
            "scan_truncated": truncated,
            "consecutive_failures": monitor.consecutive_failures,
            "last_check": cr.checked_at.isoformat(),
            "error": error_msg,
        })


# ---------------------------------------------------------------------------
# Job management
# ---------------------------------------------------------------------------
def add_oss_monitor_job(oss_monitor_id: int, interval: int):
    job_id = f"oss-{oss_monitor_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        check_oss_monitor,
        "interval",
        seconds=max(interval, 10),
        args=[oss_monitor_id],
        id=job_id,
    )


def remove_oss_monitor_job(oss_monitor_id: int):
    job_id = f"oss-{oss_monitor_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


async def load_active_oss_monitors():
    async with async_session_maker() as session:
        result = await session.execute(
            select(OssMonitor).where(OssMonitor.is_active == True)
        )
        monitors = result.scalars().all()
        for m in monitors:
            add_oss_monitor_job(m.id, m.interval_seconds)


# ---------------------------------------------------------------------------
# Public ad-hoc test connection
# ---------------------------------------------------------------------------
async def test_connection(
    endpoint: str,
    bucket_name: str,
    access_key_id: str,
    access_key_secret: str,
    prefix: str = "",
) -> dict:
    """List up to 1 object under prefix. Returns a result dict."""
    try:
        b = _build_bucket_from(endpoint, bucket_name, access_key_id, access_key_secret)
        # Pull one object to verify read access.
        it = oss2.ObjectIteratorV2(b, prefix=prefix or "", max_keys=1)
        first = None
        for obj in it:
            first = obj
            break
        return {
            "ok": True,
            "sample_key": first.key if first else None,
            "sample_size": first.size if first else None,
            "message": "连接成功" if first else "连接成功（prefix 下无对象）",
        }
    except oss2.exceptions.OssError as e:
        return {
            "ok": False,
            "message": f"OSS API error: {e.code} {e.message}",
        }
    except Exception as e:
        return {"ok": False, "message": str(e)[:500]}
