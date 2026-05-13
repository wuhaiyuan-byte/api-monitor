from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, Float, Enum as SQLEnum
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime
import enum

Base = declarative_base()


class AlertType(str, enum.Enum):
    status = "status"
    latency = "latency"
    body_mismatch = "body_mismatch"


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
    checked_at = Column(DateTime, default=datetime.utcnow)

    monitor = relationship("Monitor", back_populates="check_results")


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    monitor_id = Column(Integer, ForeignKey("monitors.id"), nullable=False)
    alert_type = Column(SQLEnum(AlertType), nullable=False)
    description = Column(String, nullable=False)
    is_resolved = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    monitor = relationship("Monitor", back_populates="alerts")


class Settings(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String, nullable=False, unique=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)