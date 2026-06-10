from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from models import AlertType


class MonitorBase(BaseModel):
    name: str
    url: str
    method: str = "GET"
    headers: Optional[str] = None
    body: Optional[str] = None
    expected_status: int = 200
    expected_body_regex: Optional[str] = None
    latency_threshold_ms: Optional[float] = None
    interval_seconds: int = 60
    timeout_seconds: int = 30
    is_active: bool = True
    failure_threshold_status: int = 2
    failure_threshold_latency: int = 2
    failure_threshold_body: int = 2
    failure_threshold_timeout: int = 2


class MonitorCreate(MonitorBase):
    pass


class MonitorUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    method: Optional[str] = None
    headers: Optional[str] = None
    body: Optional[str] = None
    expected_status: Optional[int] = None
    expected_body_regex: Optional[str] = None
    latency_threshold_ms: Optional[float] = None
    interval_seconds: Optional[int] = None
    timeout_seconds: Optional[int] = None
    is_active: Optional[bool] = None
    failure_threshold_status: Optional[int] = None
    failure_threshold_latency: Optional[int] = None
    failure_threshold_body: Optional[int] = None
    failure_threshold_timeout: Optional[int] = None


class MonitorResponse(MonitorBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class CheckResultBase(BaseModel):
    monitor_id: int
    status_code: Optional[int] = None
    response_time_ms: Optional[float] = None
    body_snippet: Optional[str] = None
    error_message: Optional[str] = None
    is_anomaly: bool = False
    anomaly_type: Optional[AlertType] = None
    checked_at: datetime


class CheckResultResponse(CheckResultBase):
    id: int

    class Config:
        from_attributes = True


class AlertBase(BaseModel):
    monitor_id: int
    alert_type: AlertType
    description: str
    is_resolved: bool = False
    created_at: datetime


class AlertResponse(AlertBase):
    id: int
    monitor_name: Optional[str] = None
    is_fired: bool = True
    consecutive_failures: int = 1
    threshold: int = 1

    class Config:
        from_attributes = True


class StatusUpdate(BaseModel):
    type: str = "status_update"
    monitor_id: int
    status: str
    response_time_ms: Optional[float] = None
    status_code: Optional[int] = None
    is_anomaly: bool = False
    last_check: datetime


class NewAlert(BaseModel):
    type: str = "new_alert"
    alert: AlertResponse


class EmailSettings(BaseModel):
    enabled: bool = False
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_email: str = ""
    to_emails: str = ""
    use_tls: bool = True


class FeishuSettings(BaseModel):
    enabled: bool = False
    webhook_url: str = ""
    alert_on_status: bool = True
    alert_on_latency: bool = True
    alert_on_body: bool = False
    alert_on_timeout: bool = True
    alert_on_recovery: bool = False
    repeat_suppress_minutes: int = 10
    test_last_at: str = ""
    test_last_status: str = ""
    test_last_error: str = ""


class SettingsResponse(BaseModel):
    email_enabled: bool = False
    email_config: EmailSettings = EmailSettings()
    feishu_enabled: bool = False
    feishu_config: FeishuSettings = FeishuSettings()