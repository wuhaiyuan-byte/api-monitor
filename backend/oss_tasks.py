"""OSS file monitor scheduled task module.

Reuses the global APScheduler instance, the WebSocket manager, the
async DB session factory, and the existing send_alert_email pipeline
from tasks.py. Does not import the HTTP monitor's check_monitor or any
of its private helpers. Broadcast event types are namespaced
'oss_status_update' / 'oss_new_alert' so the frontend can route them
separately without colliding with existing 'status_update' / 'new_alert'.
"""
import re
import json
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
def _to_datetime(value):
    """Normalize oss2's last_modified which can be datetime OR unix timestamp
    int depending on version / response format. Return naive UTC datetime or
    None.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.utcfromtimestamp(float(value))
        except (ValueError, OSError):
            return None
    if isinstance(value, str):
        # Some oss2 responses return ISO-ish strings; try to parse.
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


async def _scan(
    bucket: oss2.Bucket,
    prefix: str,
    keyword: str,
    match_mode: str,
    recursive: bool = True,
    debug_verbose: bool = True,
    debug_file_cap: int = 200,
) -> Tuple[bool, Optional["oss2.ObjectSummary"], int, bool, Optional[str], list, list, list]:
    """Run a single scan pass.

    Returns:
        (matched, first_match_summary, scanned_count, truncated, error_message,
         all_matches, sample_scanned, all_scanned)
        all_matches: list of (key, last_modified) for every key that hit
                     the keyword filter, capped at 20.
        sample_scanned: list of (key, last_modified) for the first 5
                        objects iterated regardless of match, so callers
                        can show the user "what's actually in the prefix".
        all_scanned: list of (key, last_modified, matched) for every file
                     visited, capped at debug_file_cap, so the caller can
                     persist a full per-file diagnostic in the DB.

    Args:
        recursive: if False, pass delimiter='/' to the SDK so only direct
                   children of `prefix` are returned (no subdirectory
                   recursion). Default True for backward compat.
        debug_verbose: if True, log every file scanned via [OSS-DEBUG] SCAN
                       so the operator sees one log line per file. Default
                       True to make the scan transparent.
        debug_file_cap: maximum entries to retain in all_scanned. Default 200.
    """
    try:
        scanned = 0
        truncated = False
        all_matches: list = []
        sample_scanned: list = []
        all_scanned: list = []
        first: Optional["oss2.ObjectSummary"] = None
        kwargs = {"prefix": prefix or "", "max_keys": SCAN_LIMIT + 1}
        if not recursive:
            kwargs["delimiter"] = "/"
        for obj in oss2.ObjectIteratorV2(bucket, **kwargs):
            scanned += 1
            if scanned > SCAN_LIMIT:
                truncated = True
                break
            key = obj.key
            fm = _to_datetime(obj.last_modified)
            if match_mode == "regex":
                hit = bool(re.search(keyword, key))
            else:  # 'contains'
                hit = keyword in key
            if debug_verbose:
                print(
                    f"[OSS-DEBUG]   SCAN | idx={scanned} | key={key} | "
                    f"matched={hit} | fm={fm.isoformat() if fm else 'None'}",
                    flush=True,
                )
            if hit:
                if first is None:
                    first = obj
                if len(all_matches) < 20:
                    all_matches.append((key, fm))
            if len(all_scanned) < debug_file_cap:
                all_scanned.append((key, fm, hit))
            if len(sample_scanned) < 5:
                sample_scanned.append((key, fm))
        if all_matches:
            return True, first, scanned, truncated, None, all_matches, sample_scanned, all_scanned
        return False, None, scanned, truncated, None, [], sample_scanned, all_scanned
    except oss2.exceptions.OssError as e:
        return False, None, 0, False, f"OSS API error: {e.code} {e.message}", [], [], []
    except Exception as e:  # network, auth, etc.
        return False, None, 0, False, str(e)[:500], [], [], []


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
    debug_info_json: Optional[str] = None,
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
        debug_info=debug_info_json,
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
        # Debug log: record scan inputs so we can correlate with downstream
        # freshness decisions when something looks wrong. Use print() so the
        # line shows up in `docker compose logs backend` regardless of the
        # uvicorn/Python logging level configuration. Grep for [OSS-DEBUG].
        print(
            f"[OSS-DEBUG] monitor={monitor.id} bucket={monitor.bucket} "
            f"prefix={monitor.prefix!r} keyword={monitor.keyword!r} "
            f"match_mode={monitor.match_mode} recursive={getattr(monitor, 'recursive', True)} "
            f"max_age_hours={monitor.max_age_hours} "
            f"expected_present={monitor.expected_present} "
            f"now_utc={datetime.utcnow().isoformat()}",
            flush=True,
        )
        matched, first, scanned, truncated, err, all_matches, sample_scanned, all_scanned = await _scan(
            bucket,
            monitor.prefix or "",
            monitor.keyword,
            monitor.match_mode,
            recursive=bool(getattr(monitor, "recursive", True)),
            debug_verbose=True,
            debug_file_cap=200,
        )
        # Per-file debug: log every matched key with the raw and normalized
        # last_modified so the operator can see exactly what oss2 returned
        # and how the freshness comparison was evaluated.
        for k, fm in all_matches:
            print(
                f"[OSS-DEBUG]   matched: {k}  "
                f"last_modified={fm.isoformat() if fm else 'None'}",
                flush=True,
            )
        if not all_matches:
            print(
                f"[OSS-DEBUG]   (no matches found in {scanned} scanned objects)",
                flush=True,
            )
        if truncated:
            print(
                f"[OSS-DEBUG]   scan truncated at {SCAN_LIMIT} objects",
                flush=True,
            )
        # Always show the first few files actually in the prefix so the
        # user can verify what's there even when there are 0 matches.
        for k, fm in sample_scanned:
            print(
                f"[OSS-DEBUG]   sample(前 5 个对象): {k}  last_modified={fm.isoformat() if fm else 'None'}",
                flush=True,
            )

        # Determine if any/all matched files are "stale" (older than max_age_hours).
        # If any match is fresh, the check passes. If all matches are stale (or
        # there are no matches at all), the result is "not matched".
        # IMPORTANT: iterate over all matched files via all_scanned, not the
        # 20-capped all_matches. all_matches is only kept short so the
        # error_message blob doesn't get too long; the freshness decision
        # must consider every match, otherwise a fresh file at position 21+
        # gets ignored and the check stays "not_matched" even when a fresh
        # file exists.
        is_stale = False
        fresh_matches: list = []
        stale_matches: list = []
        cutoff = None
        all_matched_full = [(k, fm) for k, fm, hit in all_scanned if hit]
        if all_matched_full and monitor.max_age_hours:
            cutoff = datetime.utcnow() - timedelta(hours=monitor.max_age_hours)
            # Per-file decision log: SINGLE LINE per file with all comparison
            # fields joined by ' | ' so docker's log driver doesn't fragment
            # a multi-line print() into separate log entries.
            for key, fm in all_matched_full:
                if fm is None:
                    print(
                        f"[OSS-DEBUG]   CHECK | key={key} | fm=None | cutoff={cutoff.isoformat()} "
                        f"| age=n/a | max_age_hours={monitor.max_age_hours} | decision=stale(unparseable)",
                        flush=True,
                    )
                    stale_matches.append((key, fm))
                else:
                    is_stale_this = fm < cutoff
                    decision = "stale" if is_stale_this else "fresh"
                    age_h = (datetime.utcnow() - fm).total_seconds() / 3600
                    print(
                        f"[OSS-DEBUG]   CHECK | key={key} | fm={fm.isoformat()} "
                        f"| cutoff={cutoff.isoformat()} | age={age_h:.2f}h "
                        f"| max_age_hours={monitor.max_age_hours} "
                        f"| fm<cutoff={is_stale_this} | decision={decision}",
                        flush=True,
                    )
                    if is_stale_this:
                        stale_matches.append((key, fm))
                    else:
                        fresh_matches.append((key, fm))
            # A match exists in OSS but all of them are stale.
            if not fresh_matches and stale_matches:
                is_stale = True
        if all_matched_full and monitor.max_age_hours:
            print(
                f"[OSS-DEBUG]   ===> cutoff_utc={cutoff.isoformat() if cutoff else 'None'} "
                f"fresh={len(fresh_matches)} stale={len(stale_matches)} "
                f"is_stale={is_stale} -> status={'matched' if fresh_matches else 'not_matched'}",
                flush=True,
            )

        if err:
            status = "error"
            matched_key = None
            file_size = None
            file_modified_dt = None
            error_msg = err
        elif monitor.expected_present:
            effective = bool(fresh_matches)
            status = "matched" if effective else "not_matched"
            if effective:
                k, fm = fresh_matches[0]
                matched_key = k
                file_modified_dt = fm
                file_size = first.size if first and first.key == k else None
            else:
                matched_key = None
                file_size = None
                file_modified_dt = None
            error_msg = None
        else:
            # expected_present = False: alert only if a FRESH file exists.
            # A stale file does not count as "unexpectedly present".
            fresh_match = bool(fresh_matches)
            status = "not_matched" if fresh_match else "matched"
            if fresh_match:
                k, fm = fresh_matches[0]
                matched_key = k
                file_modified_dt = fm
                file_size = first.size if first and first.key == k else None
            else:
                matched_key = None
                file_size = None
                file_modified_dt = None
            error_msg = None

        if truncated:
            error_msg = (
                (error_msg + "; " if error_msg else "")
                + f"扫描超过 {SCAN_LIMIT} 条提前终止，结果可能不完整"
            )

        # If stale was the cause, surface that into error_msg so the dashboard
        # shows the freshness reason AND lists the stale files (so the user
        # can see what was found and how old it is). Show all of them (up
        # to all_matches cap of 20) sorted newest-first so the user can see
        # whether their today file is in the list.
        if is_stale and status == "not_matched" and monitor.expected_present:
            sorted_stale = sorted(
                stale_matches, key=lambda x: x[1] or datetime.min, reverse=True
            )
            lines = [f"匹配项已陈旧 (max_age_hours={monitor.max_age_hours}, 扫描 {scanned} 个对象，找到 {len(stale_matches)} 个匹配项全部超期):"]
            cutoff_str = (datetime.utcnow() - timedelta(hours=monitor.max_age_hours)).strftime("%Y-%m-%d %H:%M:%S")
            for k, fm in sorted_stale:
                fm_str = fm.strftime("%Y-%m-%d %H:%M:%S") if fm else "unknown"
                lines.append(f"  - {k} (last_modified={fm_str}, 早于 {cutoff_str})")
            error_msg = (
                (error_msg + "; " if error_msg else "")
                + "\n".join(lines)
            )

        # Build a per-file diagnostic blob. We attach freshness decisions
        # to each file so the UI can show a clickable debug table.
        cutoff_iso = cutoff.isoformat() if cutoff else None
        debug_files = []
        now_utc = datetime.utcnow()
        for key, fm, hit in all_scanned:
            entry = {
                "key": key,
                "fm": fm.isoformat() if fm else None,
                "matched": bool(hit),
            }
            if fm and cutoff is not None and monitor.max_age_hours:
                age_h = (now_utc - fm).total_seconds() / 3600
                entry["age_h"] = round(age_h, 2)
                entry["cutoff"] = cutoff_iso
                if fm < cutoff:
                    entry["decision"] = "stale"
                else:
                    entry["decision"] = "fresh"
            elif hit:
                # No freshness configured; if matched we just call it fresh.
                entry["decision"] = "fresh"
            else:
                entry["decision"] = "n/a"
            debug_files.append(entry)
        debug_payload = {
            "now_utc": now_utc.isoformat(),
            "cutoff_utc": cutoff_iso,
            "recursive": bool(getattr(monitor, "recursive", True)),
            "scanned": scanned,
            "truncated": truncated,
            "max_age_hours": monitor.max_age_hours,
            "keyword": monitor.keyword,
            "match_mode": monitor.match_mode,
            "files": debug_files,
        }
        try:
            debug_info_json = json.dumps(debug_payload, ensure_ascii=False)
            print(
                f"json size={len(debug_info_json)} chars",
                flush=True,
            )
        except Exception as e:
            debug_info_json = None

        # Persist history (kept forever per spec).
        cr = _record_check(
            session, monitor, status, matched_key, file_size, file_modified_dt,
            scanned, truncated, error_msg, debug_info_json,
        )
        await session.commit()
        await session.refresh(cr)
        print(
            f"[OSS-DEBUG]   check_result id={cr.id} persisted with "
            f"debug_info={'set (' + str(len(cr.debug_info or '')) + ' chars)' if cr.debug_info else 'NULL'}",
            flush=True,
        )

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
