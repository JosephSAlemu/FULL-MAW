from dataclasses import dataclass, field
from typing_extensions import TypedDict
from typing import Annotated, Literal, Sequence
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph.message import add_messages
from enum import Enum, auto
from pydantic import BaseModel


class AgentSteps(str, Enum):
    START = "start"
    PLANNER_COMPLETE = "planner_complete"
    INSTALLER_REQUIREMENTS_PENDING_APPROVAL = "installer_requirements_pending_approval"
    INSTALLER_COMPLETE = "installer_complete"
    EXPLORER_COMPLETE = "explorer_complete"


class AgentState(TypedDict):
    messages:              Annotated[Sequence[BaseMessage], add_messages]
    goal:                  str
    pdf_path:              str
    literature_findings:   list[str]
    stack_decision:        list[str]
    tasks:                 list[str]
    exploration_log:       list[dict]       # explorer output (tool call records)
    selected_data_files:   list[str]        # filenames chosen from data/ at startup
    requirements_content:  str
    requirements_approved: bool
    image_path:            str              # path to user-uploaded image for planning (optional)
    current_step:          str
    orchestrator_feedback: str
    next:                  str
    planner_revisions:     int
    installer_revisions:   int
    explorer_revisions:    int
    engine:                str              # workflow engine: "parsl", "pycompss", etc.
    env:                   str              # execution environment: "local" or "hpc"
    condition:             str              # ablation condition: "A" (no-skills), "B" (full), "C" (single-agent)
    domain:                str              # paper domain label, e.g. "cosmology"
                                            # used to look up use_cases/<domain>/* directly instead of keyword matching


class OrchestratorOutput(BaseModel):
    reasoning:             str
    next:                  Literal["planner", "installer", "explorer", "end"]
    feedback:              str
    requirements_approved: bool = False
    skill_requests:        list[str] = []


class PlannerOutput(BaseModel):
    literature_findings: list[str]
    stack_decision:      list[str]
    tasks:               list[str]
    skill_requests:      list[str] = []


class InstallerOutput(BaseModel):
    requirements_content: str


@dataclass
class UserInput:
    pdf: str = ""
    image: str = ""
    goal: str = ""
    data_files: list[str] = field(default_factory=list)
