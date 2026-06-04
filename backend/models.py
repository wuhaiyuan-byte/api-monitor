from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, Float, Enum as SQLEnum
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime
import enum

Base = declarative_base()


class AlertType(str, enum.Enum):
    status = "status"
    latency = "latency"
    body_mismatch = "body_mismatch"
    timeout = "timeout"


class Monitor(Base):
    __tablename__ = "monitors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    url = Column(String, nullable=False)
    method = Column(String, default="GET")
    headers = Column(Text, nullable=True)
    body = Column(Text, nullable=True)
    expected_status = Column(Integer, default=200)
    expected_body_regex = Column(String, nullable=True)
    latency_threshold_ms = Column(Float, nullable=True)
    interval_seconds = Column(Integer, default=60)
    timeout_seconds = Column(Integer, default=30)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Consecutive-failure thresholds per anomaly type. Default 2 = "need 2 in a row to fire".
    failure_threshold_status = Column(Integer, default=2)
    failure_threshold_latency = Column(Integer, default=2)
    failure_threshold_body = Column(Integer, default=2)
    failure_threshold_timeout = Column(Integer, default=2)

    check_results = relationship("CheckResult", back_populates="monitor", cascade="all, delete-orphan")
    alerts = relationship("Alert", back_populates="monitor", cascade="all, delete-orphan")


class CheckResult(Base):
    __tablename__ = "check_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    monitor_id = Column(Integer, ForeignKey("monitors.id"), nullable=False)
    status_code = Column(Integer, nullable=True)
    response_time_ms = Column(Float, nullable=True)
    body_snippet = Column(String(200), nullable=True)
    error_message = Column(Text, nullable=True)
    is_anomaly = Column(Boolean, default=False)
    # Which anomaly type this check produced (status / latency / body_mismatch / timeout). NULL if normal.
    # Using String(32) instead of SQLEnum so the column type stays VARCHAR on PostgreSQL,
    # matching what the runtime migration (ALTER TABLE ADD COLUMN VARCHAR(32)) produces.
    anomaly_type = Column(String(32), nullable=True)
    checked_at = Column(DateTime, default=datetime.utcnow)

    monitor = relationship("Monitor", back_populates="check_results")


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    monitor_id = Column(Integer, ForeignKey("monitors.id"), nullable=False)
    alert_type = Column(SQLEnum(AlertType), nullable=False)
    description = Column(String, nullable=False)
    is_resolved = Column(Boolean, default=False)
    # Whether this alert reached the threshold and triggered an email/notification.
    # Accumulating alerts (is_fired=False) are recorded but not yet notified.
    is_fired = Column(Boolean, default=True)
    # Number of consecutive failures at the time this alert was created.
    consecutive_failures = Column(Integer, default=1)
    # The threshold value at the time of the alert (snapshot for historical accuracy).
    threshold = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)

    monitor = relationship("Monitor", back_populates="alerts")


class Settings(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String, nullable=False, unique=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)