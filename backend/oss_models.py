"""OSS file monitor models. Independent declarative_base so the new
oss_monitors / oss_check_results tables never collide with existing
models.py tables. Existing monitor / check_result / alert tables are
not touched.
"""
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, ForeignKey, Float
)
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime

Base = declarative_base()

SCAN_LIMIT = 200


class OssMonitor(Base):
    __tablename__ = "oss_monitors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)

    # Connection
    provider = Column(String, nullable=False, default="aliyun")  # 'aliyun' | 's3'
    endpoint = Column(String, nullable=False)
    bucket = Column(String, nullable=False)
    region = Column(String, nullable=True)
    access_key_id = Column(String, nullable=False)
    # Fernet-encrypted ciphertext of the access key secret.
    access_key_secret_enc = Column(Text, nullable=False)

    # Matching rule
    prefix = Column(String, nullable=True, default="")
    keyword = Column(String, nullable=False)
    match_mode = Column(String, nullable=False, default="contains")  # 'contains' | 'regex'
    expected_present = Column(Boolean, nullable=False, default=True)
    failure_threshold = Column(Integer, nullable=False, default=2)

    # Scheduling
    interval_seconds = Column(Integer, nullable=False, default=300)
    is_active = Column(Boolean, nullable=False, default=True)

    # Latest snapshot (denormalized for fast dashboard render)
    last_status = Column(String, nullable=True)  # 'matched' | 'not_matched' | 'error'
    last_checked_at = Column(DateTime, nullable=True)
    last_matched_key = Column(String, nullable=True)
    last_matched_size = Column(Float, nullable=True)
    last_matched_modified = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)

    check_results = relationship(
        "OssCheckResult",
        back_populates="monitor",
        cascade="all, delete-orphan",
    )


class OssCheckResult(Base):
    __tablename__ = "oss_check_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    oss_monitor_id = Column(
        Integer, ForeignKey("oss_monitors.id"), nullable=False
    )
    status = Column(String, nullable=False)  # 'matched' | 'not_matched' | 'error'
    matched_key = Column(String, nullable=True)
    file_size = Column(Float, nullable=True)
    file_last_modified = Column(DateTime, nullable=True)
    scanned_count = Column(Integer, nullable=True)
    scan_truncated = Column(Boolean, nullable=False, default=False)
    error_message = Column(Text, nullable=True)
    checked_at = Column(DateTime, default=datetime.utcnow)

    monitor = relationship("OssMonitor", back_populates="check_results")
