from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Optional, List

from models import Settings
from database import get_session

router = APIRouter()


DEFAULT_TEST_SUBJECT = "API Monitor 邮件通知测试"
DEFAULT_TEST_BODY = "这是一封来自 API Monitor 的测试邮件。\n如果收到此邮件，说明邮件配置正确。\n\n时间: {timestamp}"


@router.get("/settings")
async def get_settings(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Settings))
    settings = {s.key: s.value for s in result.scalars().all()}

    # Recipient groups: stored as JSON list
    raw_groups = settings.get("email_recipient_groups", "")
    if raw_groups:
        try:
            groups = json.loads(raw_groups)
        except json.JSONDecodeError:
            groups = []
    else:
        groups = []

    # Migrate: if no groups but old to_emails exists, create default group
    if not groups:
        legacy_to = settings.get("to_emails", "")
        if legacy_to:
            legacy_list = [e.strip() for e in legacy_to.split(",") if e.strip()]
            if legacy_list:
                groups = [{"name": "默认", "emails": legacy_list, "is_default": True}]

    if not groups:
        groups = [{"name": "默认", "emails": [], "is_default": True}]

    email_config = {
        "enabled": settings.get("email_enabled", "false") == "true",
        "smtp_host": settings.get("smtp_host", "smtp.gmail.com"),
        "smtp_port": int(settings.get("smtp_port", "587")),
        "smtp_user": settings.get("smtp_user", ""),
        "smtp_password": settings.get("smtp_password", ""),
        "from_email": settings.get("from_email", ""),
        "to_emails": settings.get("to_emails", ""),
        "use_tls": settings.get("use_tls", "true") == "true",
        # Alert trigger conditions
        "alert_on_status": settings.get("email_alert_on_status", "true") == "true",
        "alert_on_latency": settings.get("email_alert_on_latency", "true") == "true",
        "alert_on_body": settings.get("email_alert_on_body", "false") == "true",
        "alert_on_timeout": settings.get("email_alert_on_timeout", "true") == "true",
        "alert_on_recovery": settings.get("email_alert_on_recovery", "false") == "true",
        "repeat_suppress_minutes": int(settings.get("email_repeat_suppress_minutes", "10")),
        # Test email config
        "test_subject": settings.get("email_test_subject", DEFAULT_TEST_SUBJECT),
        "test_body": settings.get("email_test_body", DEFAULT_TEST_BODY),
        "test_last_at": settings.get("email_test_last_at", ""),
        "test_last_status": settings.get("email_test_last_status", ""),
        "test_last_error": settings.get("email_test_last_error", ""),
        # Recipient groups
        "recipient_groups": groups,
    }

    feishu_config = {
        "enabled": settings.get("feishu_enabled", "false") == "true",
        "webhook_url": settings.get("feishu_webhook_url", ""),
        "alert_on_status": settings.get("feishu_alert_on_status", "true") == "true",
        "alert_on_latency": settings.get("feishu_alert_on_latency", "true") == "true",
        "alert_on_body": settings.get("feishu_alert_on_body", "false") == "true",
        "alert_on_timeout": settings.get("feishu_alert_on_timeout", "true") == "true",
        "alert_on_recovery": settings.get("feishu_alert_on_recovery", "false") == "true",
        "repeat_suppress_minutes": int(settings.get("feishu_repeat_suppress_minutes", "10")),
        "test_last_at": settings.get("feishu_test_last_at", ""),
        "test_last_status": settings.get("feishu_test_last_status", ""),
        "test_last_error": settings.get("feishu_test_last_error", ""),
    }

    return {
        "email_enabled": email_config["enabled"],
        "email_config": email_config,
        "feishu_enabled": feishu_config["enabled"],
        "feishu_config": feishu_config,
    }


@router.post("/settings/email")
async def save_email_settings(
    email_config: dict,
    session: AsyncSession = Depends(get_session)
):
    settings_to_save = {
        # SMTP
        "email_enabled": str(email_config.get("enabled", False)).lower(),
        "smtp_host": email_config.get("smtp_host", "smtp.gmail.com"),
        "smtp_port": str(email_config.get("smtp_port", 587)),
        "smtp_user": email_config.get("smtp_user", ""),
        "smtp_password": email_config.get("smtp_password", ""),
        "from_email": email_config.get("from_email", ""),
        "to_emails": email_config.get("to_emails", ""),
        "use_tls": str(email_config.get("use_tls", True)).lower(),
        # Alert triggers
        "email_alert_on_status": str(email_config.get("alert_on_status", True)).lower(),
        "email_alert_on_latency": str(email_config.get("alert_on_latency", True)).lower(),
        "email_alert_on_body": str(email_config.get("alert_on_body", False)).lower(),
        "email_alert_on_timeout": str(email_config.get("alert_on_timeout", True)).lower(),
        "email_alert_on_recovery": str(email_config.get("alert_on_recovery", False)).lower(),
        "email_repeat_suppress_minutes": str(email_config.get("repeat_suppress_minutes", 10)),
        # Test config
        "email_test_subject": email_config.get("test_subject", DEFAULT_TEST_SUBJECT),
        "email_test_body": email_config.get("test_body", DEFAULT_TEST_BODY),
        # Recipient groups
        "email_recipient_groups": json.dumps(
            email_config.get("recipient_groups", []),
            ensure_ascii=False
        ),
    }

    for key, value in settings_to_save.items():
        result = await session.execute(select(Settings).where(Settings.key == key))
        existing = result.scalar_one_or_none()
        if existing:
            existing.value = value
            existing.updated_at = datetime.utcnow()
        else:
            setting = Settings(key=key, value=value)
            session.add(setting)

    await session.commit()
    return {"message": "Email settings saved"}


@router.post("/settings/email/test")
async def test_email_settings(
    email_config: dict,
    session: AsyncSession = Depends(get_session)
):
    smtp_host = email_config.get("smtp_host", "smtp.gmail.com")
    smtp_port = email_config.get("smtp_port", 587)
    smtp_user = email_config.get("smtp_user", "")
    smtp_password = email_config.get("smtp_password", "")
    from_email = email_config.get("from_email", "")
    use_tls = email_config.get("use_tls", True)
    test_subject = email_config.get("test_subject", DEFAULT_TEST_SUBJECT)
    test_body = email_config.get("test_body", DEFAULT_TEST_BODY)

    # Collect recipients from groups
    raw_groups = email_config.get("recipient_groups", [])
    recipients = []
    if raw_groups:
        for g in raw_groups:
            for e in g.get("emails", []):
                if e and e.strip():
                    recipients.append(e.strip())
    # Fallback to legacy to_emails
    if not recipients:
        legacy = email_config.get("to_emails", "")
        if legacy:
            recipients = [e.strip() for e in legacy.split(",") if e.strip()]

    if not recipients:
        await _record_test_result(session, "failed", "收件人列表为空")
        raise HTTPException(status_code=400, detail="收件人列表为空，请先在收件人分组中添加邮箱")

    try:
        msg = MIMEMultipart()
        msg['From'] = from_email
        msg['To'] = ", ".join(recipients)
        msg['Subject'] = test_subject

        # Replace {timestamp} placeholder
        body = test_body.replace("{timestamp}", datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'))
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10)
        elif use_tls:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
            server.starttls()
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)

        server.login(smtp_user, smtp_password)
        server.sendmail(from_email, recipients, msg.as_string())
        server.quit()

        await _record_test_result(session, "success", "")
        return {
            "message": "Test email sent successfully",
            "recipients": recipients,
        }
    except Exception as e:
        await _record_test_result(session, "failed", str(e))
        raise HTTPException(status_code=500, detail=f"Failed to send test email: {str(e)}")


async def _record_test_result(session: AsyncSession, status: str, error: str):
    """Write last test result into Settings."""
    now_iso = datetime.utcnow().isoformat()
    fields = {
        "email_test_last_at": now_iso,
        "email_test_last_status": status,
        "email_test_last_error": error,
    }
    for key, value in fields.items():
        result = await session.execute(select(Settings).where(Settings.key == key))
        existing = result.scalar_one_or_none()
        if existing:
            existing.value = value
            existing.updated_at = datetime.utcnow()
        else:
            session.add(Settings(key=key, value=value))
    await session.commit()


# ---------------------------------------------------------------------------
# Feishu (Lark) custom bot webhook
# ---------------------------------------------------------------------------
from tasks import _send_feishu_sync  # reuse the sync sender


@router.post("/settings/feishu")
async def save_feishu_settings(
    feishu_config: dict,
    session: AsyncSession = Depends(get_session),
):
    settings_to_save = {
        "feishu_enabled": str(feishu_config.get("enabled", False)).lower(),
        "feishu_webhook_url": feishu_config.get("webhook_url", ""),
        "feishu_alert_on_status": str(feishu_config.get("alert_on_status", True)).lower(),
        "feishu_alert_on_latency": str(feishu_config.get("alert_on_latency", True)).lower(),
        "feishu_alert_on_body": str(feishu_config.get("alert_on_body", False)).lower(),
        "feishu_alert_on_timeout": str(feishu_config.get("alert_on_timeout", True)).lower(),
        "feishu_alert_on_recovery": str(feishu_config.get("alert_on_recovery", False)).lower(),
        "feishu_repeat_suppress_minutes": str(feishu_config.get("repeat_suppress_minutes", 10)),
    }
    for key, value in settings_to_save.items():
        result = await session.execute(select(Settings).where(Settings.key == key))
        existing = result.scalar_one_or_none()
        if existing:
            existing.value = value
            existing.updated_at = datetime.utcnow()
        else:
            session.add(Settings(key=key, value=value))
    await session.commit()
    return {"message": "Feishu settings saved"}


@router.post("/settings/feishu/test")
async def test_feishu_settings(
    feishu_config: dict,
    session: AsyncSession = Depends(get_session),
):
    url = feishu_config.get("webhook_url", "")
    if not url:
        await _record_feishu_test_result(session, "failed", "webhook_url 为空")
        raise HTTPException(status_code=400, detail="webhook_url 为空")
    ok, err = _send_feishu_sync(
        url,
        "✅ API Monitor 飞书测试消息",
        "**这是一条测试消息。**\n\n"
        "如果你在飞书里看到这条卡片，说明 webhook 配置正确。\n\n"
        "时间: " + datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S') + " UTC",
    )
    if ok:
        await _record_feishu_test_result(session, "success", "")
        return {"message": "Feishu test message sent successfully"}
    else:
        await _record_feishu_test_result(session, "failed", err)
        raise HTTPException(status_code=400, detail=f"Feishu test failed: {err}")


async def _record_feishu_test_result(session: AsyncSession, status: str, error: str):
    now_iso = datetime.utcnow().isoformat()
    fields = {
        "feishu_test_last_at": now_iso,
        "feishu_test_last_status": status,
        "feishu_test_last_error": error,
    }
    for key, value in fields.items():
        result = await session.execute(select(Settings).where(Settings.key == key))
        existing = result.scalar_one_or_none()
        if existing:
            existing.value = value
            existing.updated_at = datetime.utcnow()
        else:
            session.add(Settings(key=key, value=value))
    await session.commit()
