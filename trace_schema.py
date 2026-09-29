"""
Pydantic schema for MAW trace events and run metadata.

Single source of truth for the shape of trace.json. trace_logger.py validates
every event against these models before appending, and validates the full
payload again right before writing it to disk. Analysis scripts (phase 1/2
scoring, ablation comparisons) can import this module to validate any trace
file produced by a run.
"""

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field


# __ Per-event models __________________________________________________________
# Every event carries elapsed_s (seconds since run start) and an absolute ISO
# timestamp, so events can be ordered either relatively or against external
# logs (PBS job logs, LangSmith, etc).

class BaseEvent(BaseModel):
    elapsed_s: float
    timestamp: str


class AgentStartEvent(BaseEvent):
    type: Literal["agent_start"] = "agent_start"
    agent: str
    metadata: Optional[dict] = None


class AgentInputEvent(BaseEvent):
    type: Literal["agent_input"] = "agent_input"
    agent: str
    data: dict


class AgentOutputEvent(BaseEvent):
    type: Literal["agent_output"] = "agent_output"
    agent: str
    data: dict


class AgentEndEvent(BaseEvent):
    type: Literal["agent_end"] = "agent_end"
    agent: str
    duration_s: float


class RoutingEvent(BaseEvent):
    type: Literal["routing"] = "routing"
    from_agent: str = Field(alias="from")
    to: str
    reasoning: str
    feedback: str = ""
    payload_size: int = 0

    model_config = {"populate_by_name": True}


class ToolCallEvent(BaseEvent):
    type: Literal["tool_call"] = "tool_call"
    agent: str
    tool: str
    args: dict
    result: str
    succeeded: bool
    duration_s: float = 0
    iteration: Optional[int] = None
    # only populated for submit_task/submit_shell_task/submit_mpi_task. engine_backend
    # is the raw "engine" string the MCP server reports back (e.g "parsl-fallback").
    # engine_verified: for parsl/pycompss, did the runtime actually dispatch it (not
    # fallback). for adios, did the code call a real adios2 API, not just import it.
    # None when it doesn't apply
    engine_backend: Optional[str] = None
    engine_verified: Optional[bool] = None


class SkillLoadEvent(BaseEvent):
    type: Literal["skill_load"] = "skill_load"
    agent: str
    skill_path: str
    found: bool
    suppressed: bool = False  # True when the file exists but condition A withheld its content


class LLMCallEvent(BaseEvent):
    type: Literal["llm_call"] = "llm_call"
    agent: str
    model: str
    messages: list[dict]
    response: str
    tool_calls: list[dict] = []
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_s: float = 0
    attempt: int = 1


class ReplanEvent(BaseEvent):
    type: Literal["replan"] = "replan"
    agent: str
    revision: int


class RunErrorEvent(BaseEvent):
    type: Literal["run_error"] = "run_error"
    error_type: str
    message: str
    traceback: str


class ArtifactManifestEvent(BaseEvent):
    type: Literal["artifact_manifest"] = "artifact_manifest"
    artifacts: list[dict]


class MessageEvent(BaseEvent):
    type: Literal["message"] = "message"
    from_agent: str = Field(alias="from")
    to: str
    message: str

    model_config = {"populate_by_name": True}


class TokenUsageEvent(BaseEvent):
    type: Literal["token_usage"] = "token_usage"
    agent: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model: str = ""


class UserVerificationEvent(BaseEvent):
    """The human's answer to 'has the workflow executed properly?' at end of run."""
    type: Literal["user_verification"] = "user_verification"
    succeeded: bool
    reported_issue: str = ""


class SkillRecommendationEvent(BaseEvent):
    """Post-run diagnosis: proposed skill-file additions to prevent a repeat."""
    type: Literal["skill_recommendation"] = "skill_recommendation"
    root_cause: str
    recommendations: list[dict] = []


Event = Annotated[
    Union[
        AgentStartEvent, AgentInputEvent, AgentOutputEvent, AgentEndEvent,
        RoutingEvent, ToolCallEvent, SkillLoadEvent, LLMCallEvent,
        ReplanEvent, RunErrorEvent, ArtifactManifestEvent, MessageEvent,
        TokenUsageEvent, UserVerificationEvent, SkillRecommendationEvent,
    ],
    Field(discriminator="type"),
]


# __ Run-level metadata _________________________________________________________

class RunMetadata(BaseModel):
    run_id: str
    condition: Literal["A", "B", "C"] = "B"
    combination: Literal["a", "b", "c", "d", ""] = ""
    trial: int = 1
    paper_id: str = ""
    paper_path: str = ""
    domain: str = ""
    framework: str = ""
    env: str = ""
    goal: str = ""
    model: str = ""
    seed: Optional[int] = None
    start_time: str
    end_time: Optional[str] = None
    final_status: Literal["completed", "failed", "timeout", "unknown"] = "unknown"
    # The human's end-of-run verdict, separate from whether the graph ran to completion.
    # None when the run never reached the verification prompt (e.g. it crashed).
    user_verified: Optional[bool] = None
    reported_issue: str = ""
    config_spec_ref: Optional[str] = None
    code_commit: Optional[str] = None


class TraceSummary(BaseModel):
    total_events: int
    total_duration_s: float
    agents_involved: list[str]
    tool_calls: int
    tool_successes: int
    routing_path: list[str]
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    tokens_by_agent: dict[str, int] = {}


class TraceFile(BaseModel):
    run_metadata: RunMetadata
    trace: list[Event]
    summary: TraceSummary
