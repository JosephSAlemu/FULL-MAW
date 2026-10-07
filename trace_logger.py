"""
Trace Logger -- Records all agent interactions for visualization and analysis.

Captures every message passed between agents (orchestrator, planner, installer,
explorer), every LLM call (full prompt + response, tokens, latency), every tool
call, every skill file load, and run-level metadata (paper, condition, model,
final status). Every event is validated against trace_schema.py before being
recorded, and the full payload is re-validated immediately before it's written
to disk.

Usage:
    from trace_logger import tracer

    tracer.reset()
    tracer.start_run(run_id="...", paper_id="...", goal="...", model="...")

    tracer.log_agent_start("planner")
    tracer.log_agent_input("planner", {"goal": "...", "pdf_path": "..."})
    tracer.log_agent_output("planner", {"tasks": [...], "findings": [...]})
    tracer.log_agent_end("planner")

    tracer.log_llm_call("planner", "claudeopus48", messages, response_text, ...)
    tracer.log_tool_call("explorer", "run_workflow", {"filename": "workflow.py"}, result, True)
    tracer.log_skill_load("planner", "agents/planner", found=True)

    tracer.finalize_run("completed")
    tracer.save("trace.json")
"""

import json
import os
import subprocess
import time
from datetime import datetime
from typing import Any, Optional

from trace_schema import (
    AgentStartEvent, AgentInputEvent, AgentOutputEvent, AgentEndEvent,
    RoutingEvent, ToolCallEvent, SkillLoadEvent, LLMCallEvent,
    ReplanEvent, RunErrorEvent, ArtifactManifestEvent, MessageEvent,
    TokenUsageEvent, UserVerificationEvent, SkillRecommendationEvent,
    RunMetadata, TraceFile,
)

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def _git_commit() -> Optional[str]:
    """Best-effort git SHA of the repo at run time -- None if unavailable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT,
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def message_to_dict(m: Any) -> dict:
    """Convert a LangChain BaseMessage into a JSON-safe {"role", "content"} dict.

    Image data URLs are summarized by size rather than embedded -- raw base64
    bytes have no debugging value and would dominate trace file size.
    """
    role = getattr(m, "type", m.__class__.__name__)
    content = getattr(m, "content", m)
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                url = block.get("image_url", {}).get("url", "")
                size_kb = round(len(url) * 3 / 4 / 1024, 1)
                parts.append({"type": "image_url", "image_summary": f"<image, ~{size_kb}KB>"})
            else:
                parts.append(block)
        content = parts
    return {"role": role, "content": content}


class TraceLogger:
    def __init__(self):
        self.events: list[dict] = []
        self.start_time: float = time.time()
        self._agent_starts: dict[str, float] = {}
        self.run_metadata: Optional[RunMetadata] = None

    def _elapsed(self) -> float:
        return round(time.time() - self.start_time, 2)

    def _now(self) -> str:
        return datetime.now().isoformat()

    # __ Run metadata ____________________________________________________________

    def start_run(self, **kwargs):
        """Initialize run-level metadata. Call once at the start of a run."""
        kwargs.setdefault("start_time", self._now())
        kwargs.setdefault("code_commit", _git_commit())
        self.run_metadata = RunMetadata(**kwargs)

    def finalize_run(self, final_status: str):
        """Record end-of-run status. Call exactly once, on success or failure."""
        if self.run_metadata is None:
            self.start_run(run_id="unknown")
        self.run_metadata.end_time = self._now()
        self.run_metadata.final_status = final_status

    # __ Event logging ____________________________________________________________

    def log_agent_start(self, agent_name: str, metadata: Optional[dict] = None):
        self._agent_starts[agent_name] = time.time()
        event = AgentStartEvent(agent=agent_name, metadata=metadata,
                                 elapsed_s=self._elapsed(), timestamp=self._now())
        self.events.append(event.model_dump())

    def log_agent_input(self, agent_name: str, data: dict):
        event = AgentInputEvent(agent=agent_name, data=data,
                                 elapsed_s=self._elapsed(), timestamp=self._now())
        self.events.append(event.model_dump())

    def log_agent_output(self, agent_name: str, data: dict):
        event = AgentOutputEvent(agent=agent_name, data=data,
                                  elapsed_s=self._elapsed(), timestamp=self._now())
        self.events.append(event.model_dump())

    def log_agent_end(self, agent_name: str):
        duration = 0
        if agent_name in self._agent_starts:
            duration = round(time.time() - self._agent_starts[agent_name], 2)
        event = AgentEndEvent(agent=agent_name, duration_s=duration,
                               elapsed_s=self._elapsed(), timestamp=self._now())
        self.events.append(event.model_dump())

    def log_routing(self, from_agent: str, to_agent: str, reasoning: str,
                     feedback: str = "", payload_size: int = 0):
        event = RoutingEvent.model_validate({
            "from": from_agent, "to": to_agent, "reasoning": reasoning,
            "feedback": feedback or "", "payload_size": payload_size,
            "elapsed_s": self._elapsed(), "timestamp": self._now(),
        })
        self.events.append(event.model_dump(by_alias=True))

    def log_tool_call(self, agent_name: str, tool_name: str, args: dict,
                       result: str, succeeded: bool, duration_s: float = 0,
                       iteration: Optional[int] = None,
                       engine_backend: Optional[str] = None,
                       engine_verified: Optional[bool] = None):
        event = ToolCallEvent(agent=agent_name, tool=tool_name, args=args,
                               result=result if isinstance(result, str) else str(result),
                               succeeded=succeeded, duration_s=duration_s, iteration=iteration,
                               engine_backend=engine_backend, engine_verified=engine_verified,
                               elapsed_s=self._elapsed(), timestamp=self._now())
        self.events.append(event.model_dump())

    def log_skill_load(self, agent_name: str, skill_path: str, found: bool, suppressed: bool = False):
        event = SkillLoadEvent(agent=agent_name, skill_path=skill_path, found=found,
                                suppressed=suppressed,
                                elapsed_s=self._elapsed(), timestamp=self._now())
        self.events.append(event.model_dump())

    def log_llm_call(self, agent_name: str, model: str, messages: list[dict],
                      response: str, tool_calls: Optional[list[dict]] = None,
                      input_tokens: int = 0, output_tokens: int = 0,
                      total_tokens: int = 0, latency_s: float = 0, attempt: int = 1):
        event = LLMCallEvent(
            agent=agent_name, model=model, messages=messages, response=response or "",
            tool_calls=tool_calls or [],
            input_tokens=input_tokens, output_tokens=output_tokens,
            total_tokens=total_tokens or (input_tokens + output_tokens),
            latency_s=latency_s, attempt=attempt,
            elapsed_s=self._elapsed(), timestamp=self._now(),
        )
        self.events.append(event.model_dump())

    def log_replan(self, agent_name: str, revision: int):
        event = ReplanEvent(agent=agent_name, revision=revision,
                             elapsed_s=self._elapsed(), timestamp=self._now())
        self.events.append(event.model_dump())

    def log_run_error(self, error_type: str, message: str, traceback_str: str):
        event = RunErrorEvent(error_type=error_type, message=message, traceback=traceback_str,
                               elapsed_s=self._elapsed(), timestamp=self._now())
        self.events.append(event.model_dump())

    def log_artifact_manifest(self, artifacts: list[dict]):
        event = ArtifactManifestEvent(artifacts=artifacts,
                                       elapsed_s=self._elapsed(), timestamp=self._now())
        self.events.append(event.model_dump())

    def log_token_usage(self, agent_name: str, input_tokens: int, output_tokens: int,
                         total_tokens: int = 0, model: str = ""):
        """Record token consumption for a single LLM call made by an agent."""
        if not total_tokens:
            total_tokens = input_tokens + output_tokens
        event = TokenUsageEvent(
            agent=agent_name, input_tokens=input_tokens, output_tokens=output_tokens,
            total_tokens=total_tokens, model=model,
            elapsed_s=self._elapsed(), timestamp=self._now(),
        )
        self.events.append(event.model_dump())

    def log_user_verification(self, succeeded: bool, reported_issue: str = ""):
        """Record the human's end-of-run answer to 'did the workflow execute properly?'."""
        event = UserVerificationEvent(succeeded=succeeded, reported_issue=reported_issue or "",
                                       elapsed_s=self._elapsed(), timestamp=self._now())
        self.events.append(event.model_dump())
        if self.run_metadata is not None:
            self.run_metadata.user_verified = succeeded
            self.run_metadata.reported_issue = reported_issue or ""

    def log_skill_recommendation(self, root_cause: str, recommendations: list[dict]):
        """Record the post-run diagnosis and the skill-file additions it proposes."""
        event = SkillRecommendationEvent(root_cause=root_cause,
                                          recommendations=recommendations or [],
                                          elapsed_s=self._elapsed(), timestamp=self._now())
        self.events.append(event.model_dump())

    def log_message(self, from_agent: str, to_agent: str, message: str):
        """Record a message passed between agents via state."""
        event = MessageEvent.model_validate({
            "from": from_agent, "to": to_agent, "message": message,
            "elapsed_s": self._elapsed(), "timestamp": self._now(),
        })
        self.events.append(event.model_dump(by_alias=True))

    # __ Accessors _________________________________________________________________

    def get_trace(self) -> list[dict]:
        """Get the full trace as a list of events."""
        return self.events

    def get_summary(self) -> dict:
        """Get a summary of the trace."""
        agents_seen = set()
        tool_calls = 0
        tool_successes = 0
        routings = []
        total_input_tokens = 0
        total_output_tokens = 0
        tokens_by_agent: dict[str, int] = {}

        for event in self.events:
            etype = event["type"]
            if etype in ("agent_start", "agent_end"):
                agents_seen.add(event["agent"])
            elif etype == "tool_call":
                tool_calls += 1
                if event.get("succeeded"):
                    tool_successes += 1
            elif etype == "routing":
                routings.append(f"{event['from']} -> {event['to']}")
            elif etype == "llm_call":
                total_input_tokens += event.get("input_tokens", 0)
                total_output_tokens += event.get("output_tokens", 0)
                tokens_by_agent[event["agent"]] = (
                    tokens_by_agent.get(event["agent"], 0) + event.get("total_tokens", 0)
                )

        return {
            "total_events": len(self.events),
            "total_duration_s": self._elapsed(),
            "agents_involved": sorted(agents_seen),
            "tool_calls": tool_calls,
            "tool_successes": tool_successes,
            "routing_path": routings,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_input_tokens + total_output_tokens,
            "tokens_by_agent": tokens_by_agent,
        }

    def get_interaction_sequence(self) -> list[dict]:
        """Get a simplified sequence of agent-to-agent interactions for visualization."""
        interactions = []
        for event in self.events:
            if event["type"] == "routing":
                interactions.append({
                    "from": event["from"],
                    "to": event["to"],
                    "elapsed_s": event["elapsed_s"],
                    "reasoning": event["reasoning"],
                })
            elif event["type"] == "agent_end":
                interactions.append({
                    "agent": event["agent"],
                    "action": "completed",
                    "duration_s": event["duration_s"],
                    "elapsed_s": event["elapsed_s"],
                })
            elif event["type"] == "tool_call":
                interactions.append({
                    "agent": event["agent"],
                    "action": f"tool:{event['tool']}",
                    "succeeded": event["succeeded"],
                    "elapsed_s": event["elapsed_s"],
                })
        return interactions

    def save(self, path: str):
        """Validate and save the full trace (run metadata + events + summary) to JSON."""
        if self.run_metadata is None:
            self.start_run(run_id=os.path.splitext(os.path.basename(path))[0])

        payload = {
            "run_metadata": self.run_metadata.model_dump(),
            "trace": self.events,
            "summary": self.get_summary(),
        }
        TraceFile.model_validate(payload)  # raises if any event violates the schema

        with open(path, "w") as f:
            json.dump(payload, f, indent=2, default=str)

    def reset(self):
        """Clear all trace data."""
        self.events = []
        self.start_time = time.time()
        self._agent_starts = {}
        self.run_metadata = None


def extract_usage(response: Any) -> Optional[dict]:
    """Pull token counts out of a LangChain chat response.

    Returns {"input_tokens", "output_tokens", "total_tokens"} or None if the
    response carries no usage info.
    """
    # Preferred: AIMessage.usage_metadata (provider-agnostic, normalized keys)
    usage = getattr(response, "usage_metadata", None)
    if usage:
        return {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
    # Fallback: response_metadata["token_usage"] (OpenAI-style keys)
    meta = getattr(response, "response_metadata", None) or {}
    tu = meta.get("token_usage") or meta.get("usage")
    if tu:
        return {
            "input_tokens": tu.get("prompt_tokens", 0),
            "output_tokens": tu.get("completion_tokens", 0),
            "total_tokens": tu.get("total_tokens", 0),
        }
    return None


# Global tracer instance
tracer = TraceLogger()
