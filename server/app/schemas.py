import json
import time
import uuid
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class DeployFileItem(BaseModel):
    path: str = Field(..., min_length=1, max_length=1000)
    status: str = Field(default="modified", max_length=50)
    added_ranges: List[List[int]] = Field(default_factory=list)
    authors: List[str] = Field(default_factory=list)
    content: Optional[str] = Field(None, max_length=25000)
    is_dependency_file: bool = False
    is_config_file: bool = False


class DeployCreate(BaseModel):
    commit_sha: str = Field(..., min_length=1, max_length=64)
    author: Optional[str] = Field(None, max_length=200)
    message: Optional[str] = Field(None, max_length=2000)
    diff: Optional[str] = Field(None, max_length=20000)
    environment: str = Field(default="prod", max_length=50)
    codeowners: Optional[str] = Field(None, max_length=25000)
    files: Optional[List[DeployFileItem]] = Field(default_factory=list)
    # "owner/name" on GitHub: lets the investigator read a file at this commit in ephemeral mode
    repository: Optional[str] = Field(None, max_length=200, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")



class DeployResponse(BaseModel):
    ok: bool = True
    deploy_id: int


class LogLine(BaseModel):
    """One app log line written before an error (SDK option capture_logs)."""
    ts: float = Field(allow_inf_nan=False)
    level: str = Field("INFO", max_length=10)
    logger: str = Field("", max_length=100)
    message: str = Field("", max_length=500)


class EventItem(BaseModel):
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex, max_length=64)
    type: Literal["error", "slow_request", "heartbeat"]
    message: Optional[str] = Field(None, max_length=2000)
    exc_type: Optional[str] = Field(None, max_length=200)
    stack_trace: Optional[str] = Field(None, max_length=20000)
    request_path: Optional[str] = Field(None, max_length=500)
    duration_ms: Optional[float] = Field(None, ge=0, allow_inf_nan=False)
    release: Optional[str] = Field(None, max_length=64)
    # Same limit as deploys: a longer name would never match its deploy
    environment: str = Field(default="prod", max_length=50)
    occurred_at: float = Field(allow_inf_nan=False)  # Unix epoch timestamp in seconds
    extra: Dict[str, Any] = Field(default_factory=dict)
    # The app's last log lines before this error, oldest first (errors only)
    logs: Optional[List[LogLine]] = Field(None, max_length=50)

    # Heartbeat specific fields (Addendum C1)
    # Stored as Postgres integer
    requests: Optional[int] = Field(None, ge=0, le=2_147_483_647)
    errors: Optional[int] = Field(None, ge=0, le=2_147_483_647)

    @field_validator("extra")
    @classmethod
    def validate_extra_size(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        """Ensure extra payload does not exceed 10 KB when serialized."""
        serialized = json.dumps(v)
        if len(serialized.encode("utf-8")) > 10240:
            raise ValueError("extra payload exceeds maximum allowed size of 10 KB")
        return v

    @model_validator(mode="after")
    def validate_timestamp(self) -> "EventItem":
        """Reject events with occurred_at > 60s in future or > 24 hours old."""
        now = time.time()
        if self.occurred_at > now + 60.0:
            raise ValueError(
                f"occurred_at is too far in the future: {self.occurred_at} vs current {now}"
            )
        if self.occurred_at < now - 86400.0:
            raise ValueError(
                f"occurred_at is older than 24 hours: {self.occurred_at} vs current {now}"
            )
        return self


class EventsBatch(BaseModel):
    events: List[EventItem] = Field(..., min_length=1, max_length=100)


class EventsResponse(BaseModel):
    accepted: int
    dropped: int = 0


class VerdictResponse(BaseModel):
    deploy_id: int
    commit_sha: str
    environment: str = "prod"
    author: Optional[str] = None
    message: Optional[str] = None
    deployed_at: Optional[str] = None
    verdict: str = "no_data"
    verdict_reasons: List[str] = Field(default_factory=list)
    verdict_final: bool = False
    verdict_updated_at: Optional[str] = None
    likely_cause: Optional[str] = None
    new_issues: int = 0
    errors_before: int = 0
    errors_after: int = 0
    flagged: bool = False


class DeployItem(BaseModel):
    id: int
    commit_sha: str
    author: Optional[str] = None
    message: Optional[str] = None
    diff: Optional[str] = None
    environment: str = "prod"
    deployed_at: Optional[str] = None
    verdict: str = "no_data"
    verdict_reasons: List[str] = Field(default_factory=list)
    verdict_final: bool = False
    verdict_updated_at: Optional[str] = None
    likely_cause: Optional[str] = None
    new_issues: int = 0
    errors_before: int = 0
    errors_after: int = 0
    flagged: bool = False


class IssueItem(BaseModel):
    id: int
    fingerprint: str
    title: str
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    status: str = "open"
    event_count: int = 0


class IssueEventItem(BaseModel):
    id: int
    occurred_at: str
    stack_trace: Optional[str] = None
    request_path: Optional[str] = None
    duration_ms: Optional[float] = None
    extra: Dict[str, Any] = Field(default_factory=dict)
    deploy_id: Optional[int] = None
    commit_sha: Optional[str] = None


class IssueDetailResponse(BaseModel):
    id: int
    fingerprint: str
    title: str
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    status: str = "open"
    event_count: int = 0
    events: List[IssueEventItem] = Field(default_factory=list)


class TimelineDeploy(BaseModel):
    id: int
    commit_sha: str
    environment: str = "prod"
    verdict: str = "no_data"
    deployed_at: Optional[str] = None


class TimelineBucket(BaseModel):
    bucket: str
    timestamp: str
    requests: int = 0
    errors: int = 0
    deploys: List[TimelineDeploy] = Field(default_factory=list)


class TimelineDictResponse(BaseModel):
    hours: int
    start: str
    end: str
    timeline: List[TimelineBucket] = Field(default_factory=list)
    buckets: List[TimelineBucket] = Field(default_factory=list)
    deploys: List[TimelineDeploy] = Field(default_factory=list)


class IncidentItem(BaseModel):
    id: int
    deploy_id: int
    issue_id: int
    status: str = "possible"
    title: Optional[str] = None
    where_file: Optional[str] = None
    where_line: Optional[int] = None
    where_function: Optional[str] = None
    in_diff: Optional[bool] = None
    owners: List[str] = Field(default_factory=list)
    event_count: int = 0
    category: Optional[str] = None
    first_alert_ms: Optional[int] = None
    created_at: Optional[str] = None
    confirmed_at: Optional[str] = None
    resolved_at: Optional[str] = None


class IncidentDetailResponse(BaseModel):
    id: int
    deploy_id: int
    issue_id: int
    status: str
    title: Optional[str] = None
    where_file: Optional[str] = None
    where_line: Optional[int] = None
    where_function: Optional[str] = None
    in_diff: Optional[bool] = None
    owners: List[str] = Field(default_factory=list)
    event_count: int = 0
    category: Optional[str] = None
    first_alert_ms: Optional[int] = None
    created_at: Optional[str] = None
    confirmed_at: Optional[str] = None
    resolved_at: Optional[str] = None
    commit_sha: Optional[str] = None
    commit_author: Optional[str] = None
    commit_message: Optional[str] = None
    code_excerpt: Optional[str] = None
    code_excerpt_start: Optional[int] = None  # line number of the excerpt's first line
    ai_investigation: Dict[str, Any] = Field(default_factory=dict)
    timeline: List[Dict[str, Any]] = Field(default_factory=list)
    # Past incidents like this one and how they ended (app/trust/memory.py)
    similar_incidents: List[Dict[str, Any]] = Field(default_factory=list)
    # The app's log lines before the latest occurrence (SDK option capture_logs)
    logs_before_error: List[Dict[str, Any]] = Field(default_factory=list)


class ProjectSettingsResponse(BaseModel):
    id: int
    name: str
    critical_routes: List[str] = Field(default_factory=list)
    notify_quiet: bool = False
    channels: List[Dict[str, Any]] = Field(default_factory=list)
    team_members: List[Dict[str, Any]] = Field(default_factory=list)



