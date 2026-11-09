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
    # Retained so an archived trace from before the installer was collapsed to
    # a single pass still deserializes. Nothing produces it now: the installer
    # writes requirements.txt and installs it in one action, so the only step
    # it reports is INSTALLER_COMPLETE.
    INSTALLER_REQUIREMENTS_PENDING_APPROVAL = "installer_requirements_pending_approval"
    INSTALLER_COMPLETE = "installer_complete"
    EXPLORER_COMPLETE = "explorer_complete"


class AgentState(TypedDict):
    messages:              Annotated[Sequence[BaseMessage], add_messages]
    goal:                  str
    pdf_path:              str
    literature_findings:   list[str]
    stack_decision:        list[str]
    modules:               list[str]        # environment modules this run's tools need,
                                            # read by the planner out of the domain skill.
                                            # The installer builds compiled packages under
                                            # them; each task loads them in its own command.
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
    domain:                str              # skills/use_cases/<folder>, exact name; "" for none.
                                            # Selects the domain skill AND labels the trace.
    explorer_summary:      str              # explorer's own closing summary of the run
    run_succeeded:         bool             # user's answer to "has the workflow executed properly?"
    reported_issue:        str              # user's description of the issue, when run_succeeded is False
    skill_recommendations: list[dict]       # explorer's suggested skill-file edits
    skill_edits_applied:   bool             # whether the user let the explorer append them,
    mpi_nodes:         int   # nodes the MPI application may use = the whole allocation
    mpi_ranks_per_node: int  # mpiprocs per node -- the rank slots on each
    mpi_slots:         int   # mpi_nodes * mpi_ranks_per_node -- the rank ceiling


class OrchestratorOutput(BaseModel):
    reasoning:             str
    next:                  Literal["planner", "installer", "explorer", "end"]
    feedback:              str
    requirements_approved: bool = False
    skill_requests:        list[str] = []


class PlannerOutput(BaseModel):
    literature_findings: list[str]
    stack_decision:      list[str]
    #: Environment modules the domain skill says this run's tools need, e.g.
    #: ["lammps"]. Names only -- no `module load` prefix, no version guessing.
    #:
    #: It is a planner output because the planner is the agent that reads the
    #: domain skill, and the module is stated there in prose. The installer has
    #: no language model and cannot extract it; without this field it had no way
    #: to know which module to build compiled packages (mpi4py, bindings with
    #: MPI-linked extensions) under, and pip silently compiled against whatever
    #: the launching shell happened to have.
    #:
    #: Empty when the domain names no module.
    modules:             list[str] = []
    tasks:               list[str]
    skill_requests:      list[str] = []


class InstallerOutput(BaseModel):
    requirements_content: str


@dataclass
class UserInput:
    pdf: str = ""
    image: str = ""
    goal: str = ""
    domain: str = ""
    engine: str = "parsl"
    nodes: int = 1
    ranks: int = 1
    data_files: list[str] = field(default_factory=list)