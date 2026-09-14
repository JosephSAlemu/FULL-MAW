from common.consts import LITERATURE_PATH, SKILLS_PATH, DATA_PATH, RUN_PATH, ENV_KNOWLEDGE, ENV_NOTES
from systemprompts.orchestrator import ORCHESTRATOR_SYSTEM_PROMPT, ORCHESTRATOR_SYSTEM_PROMPT_NO_SKILLS
from systemprompts.planner import PLANNER_PROMPT, PLANNER_PROMPT_NO_SKILLS
from systemprompts.context import PROJECT_LAYOUT
from common.exceptions import exception_message
import os
import base64
import json
import mimetypes
import operator
import argparse
import re
import subprocess
import time
import warnings
from datetime import datetime
from typing import Annotated, Literal, Sequence
from typing_extensions import TypedDict
from pydantic import BaseModel
from pypdf import PdfReader
from pathlib import Path
warnings.filterwarnings("ignore", module="pypdf")
from dotenv import load_dotenv
from rich.console import Console as console
from rich.panel import Panel
from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages

from mcp_explorer import explorer
from trace_logger import tracer, extract_usage, message_to_dict
from run_archiver import archive_run


async def _read_skill(rel_path: str, agent_name: str, enabled: bool = True) -> str:
    """Read skills/<rel_path>.SKILL.md -- returns '' if disabled (condition A) or not found.

    Condition A never touches the filesystem under skills/ at all -- not even an
    os.path.isfile check -- so disabled short-circuits before any I/O happens.
    """
    if not enabled:
        tracer.log_skill_load(agent_name, rel_path, found=False, suppressed=True)
        return ""
    full = os.path.join(SKILLS_PATH, rel_path + ".SKILL.md")
    found = os.path.isfile(full)
    tracer.log_skill_load(agent_name, rel_path, found, suppressed=False)
    if found:
        with open(full) as f:
            return f.read()
    return ""

async def _list_skills(folder: str) -> list:
    """List available names in skills/<folder>/: subdirectory names AND .SKILL.md base names."""
    d = os.path.join(SKILLS_PATH, folder)
    if not os.path.isdir(d):
        return []
    results = []
    for name in os.listdir(d):
        if os.path.isdir(os.path.join(d, name)):
            results.append(name)
        elif name.endswith(".SKILL.md"):
            results.append(os.path.splitext(name)[0])
    return results


async def _env_knowledge(env: str, agent_name: str, enabled: bool = True) -> str:
    """Return environment-knowledge content for the given env.

    enabled=True (condition B/C): reads the real knowledge/local|lcrc skill file, unchanged.
    enabled=False (condition A): the skill file is suppressed (logged as such); a short
    hardcoded ENV_NOTES substitute is returned instead of nothing.
    """
    skill_key = ENV_KNOWLEDGE.get(env, "knowledge/local")
    if enabled:
        return await _read_skill(skill_key, agent_name, enabled=True)
    # condition A: no filesystem touch, _read_skill(enabled=False) just logs it and bails
    await _read_skill(skill_key, agent_name, enabled=False)
    return ENV_NOTES.get(env, ENV_NOTES["local"])


async def _invoke_structured(llm, schema, messages, agent_name, retries=5):
    """Call llm and parse the response as schema, tolerating preamble text before the JSON block."""
    import json as _json, re as _re
    last_err = None
    model_name = getattr(llm, "model_name", None) or os.getenv("MODEL_NAME", "")
    msg_dicts = [message_to_dict(m) for m in messages]
    for attempt in range(retries):
        _t0 = time.time()
        response = llm.invoke(messages)
        latency_s = round(time.time() - _t0, 2)
        text = response.content if hasattr(response, "content") else str(response)
        usage = extract_usage(response)
        tracer.log_llm_call(
            agent_name, model_name, msg_dicts, text,
            input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
            total_tokens=usage["total_tokens"], latency_s=latency_s, attempt=attempt + 1,
        )
        match = _re.search(r'\{.*\}', text, _re.DOTALL)
        if not match:
            last_err = ValueError(f"No JSON object found in model response:\n{text[:500]}")
            console.print(f"[yellow][_invoke_structured] attempt {attempt+1}: no JSON found, retrying...[/yellow]")
            continue
        try:
            return schema.model_validate(_json.loads(match.group(0)))
        except (_json.JSONDecodeError, Exception) as e:
            last_err = e
            console.print(f"[yellow][_invoke_structured] attempt {attempt+1}: parse error ({e}), retrying...[/yellow]")
    raise last_err
