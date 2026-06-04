import httpx
import re
import json
import statistics
import smtplib
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from models import Monitor, CheckResult, Alert, AlertType, Settings
from database import async_session_maker
from ws_manager import manager


scheduler = AsyncIOScheduler()


# In-memory repeat-suppression cache: {(monitor_id, alert_type): last_sent_ts}
_last_alert_sent: dict = {}


def _is_alert_type_enabled(settings: dict, alert_type_value: str) -> bool:
    """Check if email is enabled for a given alert type."""
    type_to_key = {
        "status": "email_alert_on_status",
        "latency": "email_alert_on_latency",
        "body_mismatch": "email_alert_on_body",
        "timeout": "email_alert_on_timeout",
    }
    key = type_to_key.get(alert_type_value)
    if not key:
        return True
    return settings.get(key, "true") == "true"


def _is_in_cooldown(monitor_id: int, alert_type_value: str, settings: dict) -> bool:
    """Check if we're still in the repeat-suppression window."""
    try:
        cooldown = int(settings.get("email_repeat_suppress_minutes", "10"))
    except (ValueError, TypeError):
        cooldown = 10
    if cooldown <= 0:
        return False
    key = (monitor_id, alert_type_value)
    last_ts = _last_alert_sent.get(key)
    if not last_ts:
        return False
    elapsed = (datetime.utcnow() - last_ts).total_seconds()
    return elapsed < cooldown * 60


def _mark_alert_sent(monitor_id: int, alert_type_value: str):
    _last_alert_sent[(monitor_id, alert_type_value)] = datetime.utcnow()


def _collect_recipients(settings: dict) -> list:
    """Resolve the final recipient list from groups (or legacy to_emails)."""
    raw_groups = settings.get("email_recipient_groups", "")
    recipients: list = []
    if raw_groups:
        try:
            groups = json.loads(raw_groups)
            for g in groups:
                for e in g.get("emails", []):
                    if e and e.strip():
                        recipients.append(e.strip())
        except json.JSONDecodeError:
            pass
    if not recipients:
        legacy = settings.get("to_emails", "")
        if legacy:
            recipients = [e.strip() for e in legacy.split(",") if e.strip()]
    seen = set()
    deduped = []
    for r in recipients:
        if r not in seen:
            seen.add(r)
            deduped.append(r)
    return deduped


def _send_email_sync(settings: dict, subject: str, body: str):
    """Synchronous SMTP send. Returns (ok: bool, error: str, recipients: list)."""
    smtp_host = settings.get("smtp_host", "smtp.gmail.com")
    try:
        smtp_port = int(settings.get("smtp_port", "587"))
    except (ValueError, TypeError):
        smtp_port = 587
    smtp_user = settings.get("smtp_user", "")
    smtp_password = settings.get("smtp_password", "")
    from_email = settings.get("from_email", "")
    use_tls = settings.get("use_tls", "true") == "true"
    recipients = _collect_recipients(settings)

    if not recipients:
        return False, "收件人列表为空", []

    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = ", ".join(recipients)
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain', 'utf-8'))

    try:
        if use_tls:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
            server.starttls()
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
        server.login(smtp_user, smtp_password)
        server.sendmail(from_email, recipients, msg.as_string())
        server.quit()
        return True, "", recipients
    except Exception as e:
        return False, str(e), []


async def send_alert_email(monitor_name: str, alert_type: str, description: str,
                           monitor_id: int = None, is_recovery: bool = False):
    async with async_session_maker() as session:
        result = await session.execute(select(Settings).where(Settings.key == "email_enabled"))
        enabled_setting = result.scalar_one_or_none()
        if not enabled_setting or enabled_setting.value != "true":
            return

        result = await session.execute(select(Settings))
        settings = {s.key: s.value for s in result.scalars().all()}

        if is_recovery:
            if settings.get("email_alert_on_recovery", "false") != "true":
                return
        else:
            if not _is_alert_type_enabled(settings, alert_type):
                print(f"Alert email skipped: type {alert_type} disabled")
                return
            if monitor_id is not None and _is_in_cooldown(monitor_id, alert_type, settings):
                print(f"Alert email skipped: {monitor_name} {alert_type} in cooldown")
                return

        if is_recovery:
            subject = f"[API Monitor] 恢复通知 - {monitor_name}"
            body = f"""API Monitor 恢复通知

监控项: {monitor_name}
状态: 已恢复正常
时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}

告警已解除，无需处理。
"""
        else:
            alert_type_map = {
                "status": "状态异常",
                "latency": "延迟异常",
                "body_mismatch": "内容异常",
                "timeout": "请求超时",
            }
            subject = f"[API Monitor] 告警通知 - {monitor_name}"
            body = f"""API Monitor 告警通知

监控项: {monitor_name}
告警类型: {alert_type_map.get(alert_type, alert_type)}
告警描述: {description}
时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}

请及时处理。
"""

        ok, err, _ = _send_email_sync(settings, subject, body)
        if ok:
            print(f"Alert email sent for {monitor_name} ({alert_type})")
            if not is_recovery and monitor_id is not None:
                _mark_alert_sent(monitor_id, alert_type)
        else:
            print(f"Failed to send alert email: {err}")


def _get_threshold_for(monitor, alert_type: AlertType) -> int:
    """Map AlertType to the monitor's corresponding threshold field. Default 2."""
    mapping = {
        AlertType.status: monitor.failure_threshold_status,
        AlertType.latency: monitor.failure_threshold_latency,
        AlertType.body_mismatch: monitor.failure_threshold_body,
        AlertType.timeout: monitor.failure_threshold_timeout,
    }
    val = mapping.get(alert_type)
    if not val or val < 1:
        return 2
    return val


async def _count_recent_consecutive_anomalies(
    session, monitor_id: int, anomaly_type: AlertType, limit: int
) -> int:
    """Count how many of the most recent <limit> check_results for this monitor
    were anomalies of the same type. Used to drive the consecutive-failure logic.
    """
    if limit <= 0:
        return 0
    type_value = anomaly_type.value if hasattr(anomaly_type, "value") else str(anomaly_type)
    result = await session.execute(
        select(CheckResult)
        .where(CheckResult.monitor_id == monitor_id)
        .where(CheckResult.is_anomaly == True)
        .where(CheckResult.anomaly_type == type_value)
        .order_by(CheckResult.checked_at.desc())
        .limit(limit)
    )
    rows = result.scalars().all()
    return len(rows)


async def check_monitor(monitor_id: int):
    async with async_session_maker() as session:
        result = await session.execute(select(Monitor).where(Monitor.id == monitor_id))
        monitor = result.scalar_one_or_none()
        if not monitor:
            return

        headers = {}
        if monitor.headers:
            try:
                headers = json.loads(monitor.headers)
            except json.JSONDecodeError:
                pass

        check_result = CheckResult(monitor_id=monitor_id, checked_at=datetime.utcnow())

        try:
            start_time = datetime.utcnow()
            timeout = monitor.timeout_seconds or 30
            async with httpx.AsyncClient(timeout=float(timeout)) as client:
                request_kwargs = {
                    "method": monitor.method,
                    "url": monitor.url,
                    "headers": headers,
                }
                if monitor.body and monitor.method in ["POST", "PUT", "PATCH"]:
                    request_kwargs["content"] = monitor.body.encode() if isinstance(monitor.body, str) else monitor.body
                response = await client.request(**request_kwargs)
            elapsed_ms = (datetime.utcnow() - start_time).total_seconds() * 1000

            check_result.status_code = response.status_code
            check_result.response_time_ms = elapsed_ms
            check_result.body_snippet = response.text[:200] if response.text else None

            is_anomaly = False
            alert_description = None
            alert_type = None

            if response.status_code != monitor.expected_status:
                is_anomaly = True
                alert_type = AlertType.status
                alert_description = f"状态码异常: 预期 {monitor.expected_status}, 实际 {response.status_code}"

            if monitor.expected_body_regex:
                if not re.search(monitor.expected_body_regex, response.text or ""):
                    is_anomaly = True
                    if not alert_description:
                        alert_type = AlertType.body_mismatch
                        alert_description = f"响应内容不符合预期正则: {monitor.expected_body_regex}"

            recent_results = await session.execute(
                select(CheckResult)
                .where(CheckResult.monitor_id == monitor_id)
                .where(CheckResult.is_anomaly == False)
                .order_by(CheckResult.checked_at.desc())
                .limit(30)
            )
            recent_list = recent_results.scalars().all()
            response_times = [r.response_time_ms for r in recent_list if r.response_time_ms is not None]

            if len(response_times) >= 5:
                mean = statistics.mean(response_times)
                stdev = statistics.stdev(response_times) if len(response_times) > 1 else 0
                auto_threshold = mean + 3 * stdev
                threshold = monitor.latency_threshold_ms if monitor.latency_threshold_ms else auto_threshold
                threshold_type = "自定义" if monitor.latency_threshold_ms else "统计"
                if elapsed_ms > threshold:
                    is_anomaly = True
                    alert_type = AlertType.latency
                    alert_description = f"响应时间异常: {elapsed_ms:.2f}ms > {threshold_type}阈值 {threshold:.2f}ms"

            check_result.is_anomaly = is_anomaly
            check_result.anomaly_type = alert_type if is_anomaly else None
            session.add(check_result)
            await session.commit()
            await session.refresh(check_result)

            # Recovery detection: previous check was anomaly, this one is normal -> recovery.
            if not is_anomaly:
                prev_check_result = await session.execute(
                    select(CheckResult)
                    .where(CheckResult.monitor_id == monitor_id)
                    .where(CheckResult.id != check_result.id)
                    .order_by(CheckResult.checked_at.desc())
                    .limit(1)
                )
                prev = prev_check_result.scalar_one_or_none()
                if prev and prev.is_anomaly:
                    await send_alert_email(
                        monitor.name, "recovery", "已恢复正常",
                        monitor_id=monitor_id, is_recovery=True
                    )
                    await session.execute(
                        update(Alert)
                        .where(Alert.monitor_id == monitor_id, Alert.is_resolved == False)
                        .values(is_resolved=True)
                    )
                    await session.commit()

            if is_anomaly and alert_type:
                threshold_value = _get_threshold_for(monitor, alert_type)
                # Count how many recent anomalies of this type we have (including this one).
                recent_count = await _count_recent_consecutive_anomalies(
                    session, monitor_id, alert_type, threshold_value
                )
                # is_fired = True only when threshold reached.
                is_fired = recent_count >= threshold_value
                if is_fired:
                    desc = alert_description
                else:
                    desc = (
                        f"{alert_description} "
                        f"(累积 {recent_count}/{threshold_value})"
                    )
                alert = Alert(
                    monitor_id=monitor_id,
                    alert_type=alert_type,
                    description=desc,
                    is_resolved=False,
                    is_fired=is_fired,
                    consecutive_failures=recent_count,
                    threshold=threshold_value,
                    created_at=datetime.utcnow(),
                )
                session.add(alert)
                await session.commit()
                await session.refresh(alert)

                await manager.broadcast({
                    "type": "new_alert",
                    "alert": {
                        "id": alert.id,
                        "monitor_id": monitor_id,
                        "alert_type": alert_type.value,
                        "description": desc,
                        "is_resolved": False,
                        "is_fired": is_fired,
                        "consecutive_failures": recent_count,
                        "threshold": threshold_value,
                        "created_at": alert.created_at.isoformat(),
                        "monitor_name": monitor.name
                    }
                })

                # Only send email / push WebSocket "fired" notification when threshold reached.
                if is_fired:
                    await send_alert_email(
                        monitor.name, alert_type.value, alert_description,
                        monitor_id=monitor_id
                    )

            status = "anomaly" if is_anomaly else "normal"
            await manager.broadcast({
                "type": "status_update",
                "monitor_id": monitor_id,
                "status": status,
                "response_time_ms": elapsed_ms,
                "status_code": response.status_code,
                "is_anomaly": is_anomaly,
                "last_check": check_result.checked_at.isoformat()
            })

        except httpx.TimeoutException:
            timeout_value = monitor.timeout_seconds or 30
            check_result.error_message = f"请求超时 ({timeout_value}秒)"
            check_result.is_anomaly = True
            check_result.anomaly_type = AlertType.timeout
            session.add(check_result)
            await session.commit()

            threshold_value = _get_threshold_for(monitor, AlertType.timeout)
            recent_count = await _count_recent_consecutive_anomalies(
                session, monitor_id, AlertType.timeout, threshold_value
            )
            is_fired = recent_count >= threshold_value
            if is_fired:
                desc = "请求超时"
            else:
                desc = f"请求超时 (累积 {recent_count}/{threshold_value})"
            alert = Alert(
                monitor_id=monitor_id,
                alert_type=AlertType.timeout,
                description=desc,
                is_resolved=False,
                is_fired=is_fired,
                consecutive_failures=recent_count,
                threshold=threshold_value,
                created_at=datetime.utcnow(),
            )
            session.add(alert)
            await session.commit()
            await session.refresh(alert)

            await manager.broadcast({
                "type": "new_alert",
                "alert": {
                    "id": alert.id,
                    "monitor_id": monitor_id,
                    "alert_type": AlertType.timeout.value,
                    "description": desc,
                    "is_resolved": False,
                    "is_fired": is_fired,
                    "consecutive_failures": recent_count,
                    "threshold": threshold_value,
                    "created_at": alert.created_at.isoformat(),
                    "monitor_name": monitor.name
                }
            })

            if is_fired:
                await send_alert_email(monitor.name, AlertType.timeout.value, "请求超时", monitor_id=monitor_id)

            await manager.broadcast({
                "type": "status_update",
                "monitor_id": monitor_id,
                "status": "anomaly",
                "response_time_ms": None,
                "status_code": None,
                "is_anomaly": True,
                "last_check": check_result.checked_at.isoformat()
            })

        except Exception as e:
            check_result.error_message = str(e)
            check_result.is_anomaly = True
            check_result.anomaly_type = AlertType.status
            session.add(check_result)
            await session.commit()

            threshold_value = _get_threshold_for(monitor, AlertType.status)
            recent_count = await _count_recent_consecutive_anomalies(
                session, monitor_id, AlertType.status, threshold_value
            )
            is_fired = recent_count >= threshold_value
            if is_fired:
                desc = f"请求错误: {str(e)[:120]}"
            else:
                desc = f"请求错误: {str(e)[:120]} (累积 {recent_count}/{threshold_value})"
            alert = Alert(
                monitor_id=monitor_id,
                alert_type=AlertType.status,
                description=desc,
                is_resolved=False,
                is_fired=is_fired,
                consecutive_failures=recent_count,
                threshold=threshold_value,
                created_at=datetime.utcnow(),
            )
            session.add(alert)
            await session.commit()
            await session.refresh(alert)

            await manager.broadcast({
                "type": "new_alert",
                "alert": {
                    "id": alert.id,
                    "monitor_id": monitor_id,
                    "alert_type": AlertType.status.value,
                    "description": desc,
                    "is_resolved": False,
                    "is_fired": is_fired,
                    "consecutive_failures": recent_count,
                    "threshold": threshold_value,
                    "created_at": alert.created_at.isoformat(),
                    "monitor_name": monitor.name
                }
            })

            if is_fired:
                await send_alert_email(
                    monitor.name, AlertType.status.value, f"请求错误: {str(e)[:120]}",
                    monitor_id=monitor_id
                )

            await manager.broadcast({
                "type": "status_update",
                "monitor_id": monitor_id,
                "status": "error",
                "response_time_ms": None,
                "status_code": None,
                "is_anomaly": True,
                "last_check": check_result.checked_at.isoformat()
            })


def add_monitor_job(monitor_id: int, interval: int):
    job_id = str(monitor_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(check_monitor, 'interval', seconds=interval, args=[monitor_id], id=job_id)


def remove_monitor_job(monitor_id: int):
    job_id = str(monitor_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


async def load_active_monitors():
    async with async_session_maker() as session:
        result = await session.execute(select(Monitor).where(Monitor.is_active == True))
        monitors = result.scalars().all()
        for monitor in monitors:
            add_monitor_job(monitor.id, monitor.interval_seconds)
