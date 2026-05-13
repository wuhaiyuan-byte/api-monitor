import httpx
import re
import json
import statistics
import smtplib
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from models import Monitor, CheckResult, Alert, AlertType, Settings
from database import async_session_maker
from ws_manager import manager


scheduler = AsyncIOScheduler()


async def send_alert_email(monitor_name: str, alert_type: str, description: str):
    async with async_session_maker() as session:
        result = await session.execute(select(Settings).where(Settings.key == "email_enabled"))
        enabled_setting = result.scalar_one_or_none()

        if not enabled_setting or enabled_setting.value != "true":
            return

        result = await session.execute(select(Settings))
        settings = {s.key: s.value for s in result.scalars().all()}

        smtp_host = settings.get("smtp_host", "smtp.gmail.com")
        smtp_port = int(settings.get("smtp_port", "587"))
        smtp_user = settings.get("smtp_user", "")
        smtp_password = settings.get("smtp_password", "")
        from_email = settings.get("from_email", "")
        to_emails = settings.get("to_emails", "")
        use_tls = settings.get("use_tls", "true") == "true"

        if not to_emails:
            return

        try:
            alert_type_map = {
                "status": "状态异常",
                "latency": "延迟异常",
                "body_mismatch": "内容异常"
            }

            msg = MIMEMultipart()
            msg['From'] = from_email
            msg['To'] = to_emails
            msg['Subject'] = f"[API Monitor] 告警通知 - {monitor_name}"

            body = f"""API Monitor 告警通知

监控项: {monitor_name}
告警类型: {alert_type_map.get(alert_type, alert_type)}
告警描述: {description}
时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}

请及时处理。
"""
            msg.attach(MIMEText(body, 'plain', 'utf-8'))

            if use_tls:
                server = smtplib.SMTP(smtp_host, smtp_port)
                server.starttls()
            else:
                server = smtplib.SMTP(smtp_host, smtp_port)

            server.login(smtp_user, smtp_password)
            server.sendmail(from_email, to_emails.split(','), msg.as_string())
            server.quit()
            print(f"Alert email sent for {monitor_name}")
        except Exception as e:
            print(f"Failed to send alert email: {e}")


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
            session.add(check_result)
            await session.commit()
            await session.refresh(check_result)

            if is_anomaly and alert_type:
                alert = Alert(
                    monitor_id=monitor_id,
                    alert_type=alert_type,
                    description=alert_description,
                    is_resolved=False,
                    created_at=datetime.utcnow()
                )
                session.add(alert)
                await session.commit()

                await manager.broadcast({
                    "type": "new_alert",
                    "alert": {
                        "id": alert.id,
                        "monitor_id": monitor_id,
                        "alert_type": alert_type.value,
                        "description": alert_description,
                        "is_resolved": False,
                        "created_at": alert.created_at.isoformat(),
                        "monitor_name": monitor.name
                    }
                })

                await send_alert_email(monitor.name, alert_type.value, alert_description)

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
            check_result.error_message = "请求超时 (30秒)"
            check_result.is_anomaly = True
            session.add(check_result)
            await session.commit()

            alert = Alert(
                monitor_id=monitor_id,
                alert_type=AlertType.status,
                description="请求超时",
                is_resolved=False,
                created_at=datetime.utcnow()
            )
            session.add(alert)
            await session.commit()

            await manager.broadcast({
                "type": "new_alert",
                "alert": {
                    "monitor_id": monitor_id,
                    "alert_type": "status",
                    "description": "请求超时",
                    "is_resolved": False,
                    "monitor_name": monitor.name
                }
            })

            await send_alert_email(monitor.name, "status", "请求超时")

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
            session.add(check_result)
            await session.commit()

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