from dataclasses import dataclass, field
from typing_extensions import TypedDict
from typing import Annotated, Literal, Sequence
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from enum import Enum
from pydantic import BaseModel


class AgentSteps(str, Enum):
    START = "start"
    PLANNER_COMPLETE = "planner_complete"
    # The installer's pass 1 reports this and stops; pass 2 installs. The route
    # out is back to the installer, from the _ROUTE table -- not a decision.
    # The orchestrator's only input at this step is requirements_override.
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
    selected_data_files:   list[str]        # filenames chosen from inputs/data/ at startup
    requirements_content:  str
    requirements_approved: bool
    #: The orchestrator's corrected package list, if it supplied one. The
    #: installer prefers this over stack_decision.
    requirements_override: list[str]
    image_path:            str              # path to user-uploaded image for planning (optional)
    current_step:          str
    orchestrator_feedback: str
    next:                  str
    #: ALWAYS 0. No agent is ever re-entered, so nothing can be a revision.
    #: Kept so an archived trace from when the orchestrator could send agents
    #: back still deserializes, and so the dashboard's columns do not move.
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
    #: What the orchestrator observed. THE ONLY FIELD THAT REACHES A HUMAN --
    #: it goes to the run log and the console. Not addressed to another agent;
    #: no agent receives anything from the orchestrator but the installer, and
    #: the installer receives a list.
    reasoning:             str

    #: IGNORED. The route is a property of current_step, looked up in
    #: Orchestrator.action's _ROUTE table:
    #:
    #:   start                                   -> planner
    #:   planner_complete                        -> installer
    #:   installer_requirements_pending_approval -> installer   (pass 2)
    #:   installer_complete                      -> explorer
    #:   explorer_complete                       -> end
    #:
    #: Kept in the schema because the model fills it in and the trace records
    #: what it asked for, which is worth seeing when it disagrees. Earlier
    #: versions honoured it and policed the result afterwards -- a revision
    #: cap, then a vacuous-reason guard. Both treated the symptom: if the
    #: route follows from the step, the model should not be asked for it.
    next:                  Literal["planner", "installer", "explorer", "end"]

    #: IGNORED AND BLANKED before the state update. There is no recipient:
    #: the installer is corrected with a list rather than with prose, and no
    #: other agent is ever re-entered.
    #:
    #: WHATEVER THE PLANNER PRODUCES STANDS, EVEN IF IT IS WRONG. The planner
    #: owns the plan and the explorer reports what happened; sending either
    #: back costs a full LLM session, plus a PDF re-read for the planner, and
    #: returns the same output.
    feedback:              str

    #: Whether the pending requirements.txt installs as written. The code sets
    #: this true at the approval step either way -- there is nothing a
    #: withheld approval could achieve on its own.
    requirements_approved: bool = False

    #: The corrected package list, when the orchestrator does not want the one
    #: the installer wrote. The installer has no language model -- it writes a
    #: list to disk and runs pip -- so a different list is the only thing that
    #: alters the outcome, and the orchestrator supplies it itself.
    requirements_override: list[str] = []

    #: The corrected TASK list, when the orchestrator finds a problem in the
    #: planner's output. Same principle as requirements_override: the
    #: orchestrator fixes it rather than sending it back, because the planner
    #: owns the plan and re-entering it costs a full LLM session plus a PDF
    #: re-read, observed to return the same output. Empty means no correction.
    tasks_override:        list[str] = []

    #: First call only.
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