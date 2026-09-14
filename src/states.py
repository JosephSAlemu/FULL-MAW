from dataclasses import dataclass
from typing_extensions import TypedDict
from typing import Annotated, Literal, Sequence
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph.message import add_messages
from enum import Enum, auto
from pydantic import BaseModel


class AgentSteps(Enum):
    START = auto()
    PLANNER_COMPLETE = auto()
    INSTALLER_REQUIREMENTS_PENDING_APPROVAL = auto()
    INSTALLER_COMPLETE = auto()
    EXPLORER_COMPLETE = auto()

@dataclass
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
    current_step:          AgentSteps
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
    
    #Note: some of these don't need defaults because the parser assigned them anyways
    def __init__(self,
                 engine,
                 env,
                 condition,
                 domain, 
                 messages=[], 
                 goal="", 
                 pdf_path="", 
                 literature_findings=[],
                 stack_decision=[],
                 tasks=[],
                 exploration_log=[],
                 selected_data_files=[],
                 requirements_content="",
                 requirements_approved=False,
                 image_path="",           
                 current_step=AgentSteps.START,   
                 orchestrator_feedback="",
                 next="",
                 planner_revisions=0,    
                 installer_revisions=0,
                 explorer_revisions=0,
                 ):
        self.engine = engine
        self.env = env
        self.condition = condition
        self.domain = domain
        self.messages = messages
        self.goal = goal
        self.pdf_path = pdf_path
        self.literature_findings = literature_findings
        self.stack_decision = stack_decision
        self.tasks = tasks
        self.exploration_log = exploration_log
        self.selected_data_files = selected_data_files
        self.requirements_content = requirements_content
        self.requirements_approved = requirements_approved
        self.image_path = image_path
        self.current_step = current_step
        self.orchestrator_feedback = orchestrator_feedback
        self.next = next
        self.planner_revisions = planner_revisions
        self.installer_revisions = installer_revisions
        self.explorer_revisions = explorer_revisions

@dataclass
class OrchestratorOutput(BaseModel):
    reasoning:           str
    next:                  Literal["planner", "installer", "explorer", "end"]
    feedback:              str
    requirements_approved: bool = False
    skill_requests:        list[str] = []

@dataclass
class PlannerOutput(BaseModel):
    literature_findings: list[str]
    stack_decision:      list[str]
    tasks:               list[str]
    skill_requests:      list[str] = []

@dataclass
class InstallerOutput(BaseModel):
    requirements_content: str