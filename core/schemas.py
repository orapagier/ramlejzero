from pydantic import BaseModel, Field
from typing import Any
from enum import Enum
from datetime import datetime


# ── Tool Schemas ──

class ToolResult(BaseModel):
    success: bool
    text: str
    file_bytes: bytes | None = None
    filename: str | None = None
    error: str | None = None

    @classmethod
    def ok(cls, text: str, file_bytes: bytes = None, filename: str = None):
        return cls(success=True, text=text, file_bytes=file_bytes, filename=filename)

    @classmethod
    def fail(cls, error: str):
        return cls(success=False, text=f"Error: {error}", error=error)


class ToolCallLog(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    tool_name: str
    input_params: dict[str, Any]
    result_text: str
    success: bool
    duration_ms: float
    error: str | None = None


# ── Agent Schemas ──

class AgentResponse(BaseModel):
    text: str
    file_bytes: bytes | None = None
    filename: str | None = None
    model_used: str
    iterations: int
    total_input_tokens: int
    total_output_tokens: int
    duration_ms: float
    tool_calls: list[ToolCallLog] = []
    success: bool = True
    error: str | None = None


class AgentRunLog(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    user_id: int
    user_message: str
    response: AgentResponse


# ── Model Schemas ──

class ModelStatus(str, Enum):
    available = "available"
    rate_limited = "rate_limited"
    unavailable = "unavailable"


class ModelRecord(BaseModel):
    name: str
    provider: str
    api_key: str
    base_url: str | None = None      # optional — overrides provider default base URL
    max_tokens: int
    priority: int
    status: ModelStatus = ModelStatus.available
    rate_limited_at: datetime | None = None
    rate_limit_reset_at: datetime | None = None
    consecutive_errors: int = 0
    total_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    last_used_at: datetime | None = None

    def is_available(self) -> bool:
        if self.status == ModelStatus.available:
            return True
        if self.status == ModelStatus.rate_limited and self.rate_limit_reset_at:
            if datetime.utcnow() >= self.rate_limit_reset_at:
                return True  # cooldown expired
        return False

    def mark_rate_limited(self, cooldown_minutes: int):
        from datetime import timedelta
        self.status = ModelStatus.rate_limited
        self.rate_limited_at = datetime.utcnow()
        self.rate_limit_reset_at = datetime.utcnow() + timedelta(minutes=cooldown_minutes)

    def mark_available(self):
        self.status = ModelStatus.available
        self.consecutive_errors = 0
        self.rate_limited_at = None
        self.rate_limit_reset_at = None

    def mark_error(self, error_threshold: int):
        self.consecutive_errors += 1
        if self.consecutive_errors >= error_threshold:
            self.status = ModelStatus.unavailable


# ── Rate Limit Schemas ──

class RateLimitState(BaseModel):
    api_name: str
    calls_this_minute: int = 0
    window_start: datetime = Field(default_factory=datetime.utcnow)
    max_calls_per_minute: int = 60
    total_calls: int = 0
    total_blocked: int = 0

    def is_allowed(self) -> bool:
        now = datetime.utcnow()
        elapsed = (now - self.window_start).total_seconds()
        if elapsed >= 60:
            self.calls_this_minute = 0
            self.window_start = now
        return self.calls_this_minute < self.max_calls_per_minute

    def record_call(self):
        self.calls_this_minute += 1
        self.total_calls += 1

    def record_blocked(self):
        self.total_blocked += 1

    @property
    def seconds_until_reset(self) -> float:
        elapsed = (datetime.utcnow() - self.window_start).total_seconds()
        return max(0, 60 - elapsed)
