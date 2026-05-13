from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

from models import Settings
from database import get_session

router = APIRouter()


@router.get("/settings")
async def get_settings(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Settings))
    settings = {s.key: s.value for s in result.scalars().all()}

    email_config = {
        "enabled": settings.get("email_enabled", "false") == "true",
        "smtp_host": settings.get("smtp_host", "smtp.gmail.com"),
        "smtp_port": int(settings.get("smtp_port", "587")),
        "smtp_user": settings.get("smtp_user", ""),
        "smtp_password": settings.get("smtp_password", ""),
        "from_email": settings.get("from_email", ""),
        "to_emails": settings.get("to_emails", ""),
        "use_tls": settings.get("use_tls", "true") == "true",
    }

    return {
        "email_enabled": email_config["enabled"],
        "email_config": email_config
    }


@router.post("/settings/email")
async def save_email_settings(
    email_config: dict,
    session: AsyncSession = Depends(get_session)
):
    settings_to_save = {
        "email_enabled": str(email_config.get("enabled", False)).lower(),
        "smtp_host": email_config.get("smtp_host", "smtp.gmail.com"),
        "smtp_port": str(email_config.get("smtp_port", 587)),
        "smtp_user": email_config.get("smtp_user", ""),
        "smtp_password": email_config.get("smtp_password", ""),
        "from_email": email_config.get("from_email", ""),
        "to_emails": email_config.get("to_emails", ""),
        "use_tls": str(email_config.get("use_tls", True)).lower(),
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
    try:
        smtp_host = email_config.get("smtp_host", "smtp.gmail.com")
        smtp_port = email_config.get("smtp_port", 587)
        smtp_user = email_config.get("smtp_user", "")
        smtp_password = email_config.get("smtp_password", "")
        from_email = email_config.get("from_email", "")
        to_emails = email_config.get("to_emails", "")
        use_tls = email_config.get("use_tls", True)

        msg = MIMEMultipart()
        msg['From'] = from_email
        msg['To'] = to_emails
        msg['Subject'] = "API Monitor 邮件通知测试"

        body = "这是一封来自 API Monitor 的测试邮件。如果收到此邮件，说明邮件配置正确。"
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        if use_tls:
            server = smtplib.SMTP(smtp_host, smtp_port)
            server.starttls()
        else:
            server = smtplib.SMTP(smtp_host, smtp_port)

        server.login(smtp_user, smtp_password)
        server.sendmail(from_email, to_emails.split(','), msg.as_string())
        server.quit()

        return {"message": "Test email sent successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send test email: {str(e)}")
