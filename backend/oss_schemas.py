"""Pydantic schemas for the OSS monitor module. Kept in its own file so
schemas.py (the existing HTTP monitor schemas) is never touched.
"""
from pydantic import BaseModel, Field, SecretStr, field_validator
from typing import Optional, List
from datetime import datetime


class OssMonitorBase(BaseModel):
    name: str
    provider: str = "aliyun"
    endpoint: str
    bucket: str
    region: Optional[str] = None
    # Access path inside the bucket. Required (non-empty): operators without
    # whole-bucket List permission must point at the specific path they own.
    prefix: str
    keyword: str
    match_mode: str = "contains"
    expected_present: bool = True
    # Optional freshness window. If set, the matched file's last_modified
    # must be >= now() - max_age_hours. NULL disables freshness check.
    max_age_hours: Optional[int] = Field(default=None, ge=1, le=8760)
    failure_threshold: int = 2
    interval_seconds: int = 300
    is_active: bool = True

    @field_validator("prefix")
    @classmethod
    def _prefix_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("prefix is required and must be non-empty")
        return v.strip().rstrip("/") + "/"  # normalize: no leading/trailing slash mess


class OssMonitorCreate(OssMonitorBase):
    access_key_id: str
    access_key_secret: SecretStr


class OssMonitorUpdate(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    endpoint: Optional[str] = None
    bucket: Optional[str] = None
    region: Optional[str] = None
    prefix: Optional[str] = None
    keyword: Optional[str] = None
    match_mode: Optional[str] = None
    expected_present: Optional[bool] = None
    max_age_hours: Optional[int] = Field(default=None, ge=1, le=8760)
    failure_threshold: Optional[int] = None
    interval_seconds: Optional[int] = None
    is_active: Optional[bool] = None
    access_key_id: Optional[str] = None
    # Optional: only update if provided.
    access_key_secret: Optional[SecretStr] = None

    @field_validator("prefix")
    @classmethod
    def _prefix_not_empty(cls, v):
        if v is None:
            return v
        if not v or not v.strip():
            raise ValueError("prefix must be non-empty when provided")
        return v.strip().rstrip("/") + "/"


class OssMonitorResponse(BaseModel):
    id: int
    name: str
    provider: str
    endpoint: str
    bucket: str
    region: Optional[str] = None
    prefix: str
    keyword: str
    match_mode: str
    expected_present: bool
    max_age_hours: Optional[int] = None
    failure_threshold: int
    interval_seconds: int
    is_active: bool
    access_key_id: str
    access_key_secret_masked: str
    last_status: Optional[str] = None
    last_checked_at: Optional[datetime] = None
    last_matched_key: Optional[str] = None
    last_matched_size: Optional[float] = None
    last_matched_modified: Optional[datetime] = None
    last_error: Optional[str] = None
    consecutive_failures: int
    created_at: datetime

    class Config:
        from_attributes = True


class OssCheckResultResponse(BaseModel):
    id: int
    oss_monitor_id: int
    status: str
    matched_key: Optional[str] = None
    file_size: Optional[float] = None
    file_last_modified: Optional[datetime] = None
    scanned_count: Optional[int] = None
    scan_truncated: bool
    error_message: Optional[str] = None
    checked_at: datetime

    class Config:
        from_attributes = True


class OssTestConnectionRequest(BaseModel):
    provider: str = "aliyun"
    endpoint: str
    bucket: str
    region: Optional[str] = None
    access_key_id: str
    access_key_secret: SecretStr
    prefix: str = ""
