---
name: agents/installer
description: >
  Base skill for the installer agent. Documents the two-phase venv setup process:
  Phase 1 reads/generates a requirements.txt, Phase 2 pip-installs into the local venv.
  Covers the dependency-reuse skip logic and orchestrator approval flow.
---

# Installer Agent — Base Skill

Manages the local venv environment. Phase 1 produces a requirements.txt for orchestrator review; Phase 2 pip-installs into the venv (skipped if all packages already present).

---

## When to Use This Skill

Loaded on every installer() call. Provides the general installer mechanics.

Pair with the use-case installer skill for domain-specific package requirements.

---

## Overview

The installer is a two-phase agent:
- **Phase 1:** Returns a requirements.txt to the orchestrator for review. Sets `current_step="installer_requirements_pending_approval"`.
- **Phase 2:** Runs `pip install -r requirements.txt` only after the orchestrator sets `requirements_approved=True`. Skips install if all packages are already present in the venv.

---

## Step-by-Step Workflow

### Step 1 (Phase 1): Read or generate requirements.txt

Reads `builds/requirements.txt` from disk if it exists, otherwise generates one from `stack_decision`.

Returns:
```python
{"requirements": requirements_content, "current_step": "installer_requirements_pending_approval"}
```

### Step 2: Orchestrator reviews and approves

The orchestrator checks the requirements and sets `requirements_approved=True`. Installer is called again.

### Step 3 (Phase 2): Install packages (or skip)

Check if all packages are already installed before running pip:
```bash
pip show <package>
```
If all present → skip install, return immediately.
If any missing → run `pip install -r builds/requirements.txt`.

---

## Key Rules and Constraints

- Never proceed to Phase 2 without `requirements_approved=True` in state
- Always check for existing packages before installing — avoids unnecessary network calls
- The installer only manages requirements.txt and pip installs into the venv -- no other build/runtime state is tracked

---

## Notes
