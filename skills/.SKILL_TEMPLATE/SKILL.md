---

**TEMPLATE USE ONLY: All "e.g." examples below are for illustration.
Replace with specific conditions when writing a real skill file.**

---

**---**

**name:** skill-name 
- (ex:use_case/<domain_folder_name>/<domain_name>)

**description: >**

- Brief summary of what this skill enables the agent to do. Include WHEN to trigger it —

- list specific user phrases, contexts, and task types. Be slightly "pushy" to avoid.

- undertriggering: e.g., "Use this skill whenever the user mentions X, Y, or Z, even.

- if they don't explicitly ask for it."

**---**



# **Skill Name**
- One-sentence summary of what this skill does and why it exists
- Specify the role of the specific agent for your workflow. **For Example:** 
  - Orchestrator routes rules specific to (domain) workflow.
  - Planner extracts specific rules and steps for the workflow/project
  - Installer installs and sets up necessary python packages and external packages/tools (mpi, spack, PBS pro)
  - Explorer the steps of execution for this (domain)

**---**



## **When to Use This Skill**
 Use this skill when the user:
    
    - Trigger condition 1 (e.g., asks to generate a specific type of file or artifact)

    - Trigger condition 2 (e.g., mentions a specific tool, library, or framework)

    - Trigger condition 3 (e.g., wants to perform a multi-step task in a codebase)

    Note: Specify the paths for the tools necessary for this workflow.


 **Do NOT use this skill when:**

    - Anti-trigger 1 (e.g., this skill file is used to provide workflow context and the user is requesting for code generation)

    - Anti-trigger 2 (e.g., the specified scientific workflow involves PARSL -- use the PARSL skill instead)



**---**



## **Overview**

Short description of the workflow this skill enables. Explain the high-level approach in 2-4 sentences.

**Dependencies:** List any tools, packages, or CLIs needed (e.g., `ripgrep`, `jq`, `python3`).

**---**



## **Step-by-Step Workflow**

### Note: This section has no limit to the steps it can contain. This three step example is not the maximum or minimum amount of steps required.

### Step 1: [First Action]

Describe what to do first. Be explicit — name the tool, the path, and the expected result.

```bash
# Example: reading an uploaded file
cat /mnt/user-data/uploads/input.csv | head -20
```

**Few-shot:**
- Input: user says "process my sales data" with a file uploaded
- Action: run `ls /mnt/user-data/uploads/` to confirm the file exists, then inspect it
- Output: a brief summary of what the file contains before proceeding

### Step 2: [Second Action]

Describe the next step with a concrete example of what correct behavior looks like.

```python
# Example: a transformation the agent might apply
import pandas as pd
df = pd.read_csv("/mnt/user-data/uploads/input.csv")
result = df.groupby("category").sum()
result.to_csv("/mnt/user-data/outputs/summary.csv", index=False)
```

**Few-shot:**
- Input: a CSV with columns `date`, `category`, `amount`
- Action: group by `category`, sum `amount`, write result to outputs/
- Output: `summary.csv` saved to `/mnt/user-data/outputs/`

### Step 3: [Third Action]

Continue through the workflow. Include any validation step before finalizing output.

**Few-shot:**
- Before writing output: confirm the result has the expected shape (e.g., row count, columns)
- If validation fails: surface the issue to the user with a plain-language explanation
- If validation passes: write the file and call `present_files` (if available in context)

**---**



## **Input / Output**

**Input:** Describe what the agent receives — file paths, user instructions, structured data, etc.

**Output:** Describe what the agent produces — file type, naming convention, directory.

**Output path:** `/mnt/user-data/outputs/<descriptive-filename>.<ext>`

### Few-Shot I/O Examples

**Example A — File transform:**
- Input: `/mnt/user-data/uploads/data.xlsx`
- Output: `/mnt/user-data/outputs/data_cleaned.csv`

**Example B — Code generation:**
- Input: user describes a module they want
- Output: new file written to the project directory, e.g., `src/utils/parser.py`

**Example C — Multi-file output:**
- Input: a directory of `.md` files
- Output: a single compiled `report.pdf` in `/mnt/user-data/outputs/`

**---**



## **Key Rules and Constraints**

- **Always** do X before proceeding to step Y (e.g., validate input format before transforming)
- **Never** overwrite the original uploaded file — always write to `/mnt/user-data/outputs/`
- **Format constraint:** output must conform to [schema / structure / naming convention]
- **Error handling:** if a step fails, explain what went wrong plainly before retrying or stopping

### Few-Shot Constraint Examples

| Situation | Correct behavior | Incorrect behavior |
|---|---|---|
| Input file is missing | Tell the user and stop | Silently create an empty output |
| Output would overwrite existing file | Append a suffix (e.g., `_v2`) | Overwrite without warning |
| Ambiguous instruction | Ask one targeted clarifying question | Guess and proceed silently |
| Dependency not installed | Run `pip install X --break-system-packages` | Fail with a raw traceback |

**---**



## **Examples**

### Example 1: [Happy Path — Simple Case]

**User says:** "Summarize the data in my uploaded CSV."

**Agent does:**
1. Runs `ls /mnt/user-data/uploads/` to find the file
2. Reads a sample with `head` to understand structure
3. Writes a Python script to compute summary stats
4. Saves output to `/mnt/user-data/outputs/summary.txt`
5. Reports the result inline with key findings

**Expected output snippet:**
```
Rows: 1,204
Columns: date, region, revenue
Revenue range: $412 – $98,430
Top region: Northeast ($1.2M total)
```

**---**



### Example 2: [Edge Case — Malformed Input]

**User says:** "Parse this JSON config and generate a scaffold."

**Agent does:**
1. Reads the file — finds it is not valid JSON
2. Reports: "The file has a syntax error on line 14 — missing closing bracket. Please fix and re-upload."
3. Does NOT attempt to proceed or guess at intent

**---**



### Example 3: [Ambiguous Intent]

**User says:** "Clean up my project."

**Agent does:**
1. Recognizes this is ambiguous — "clean up" could mean formatting, dead code removal, dependency pruning, etc.
2. Asks: "Do you mean auto-format the code, remove unused imports, or something else?"
3. Waits for clarification before running any tool

**---**



## **Edge Cases**

- **Missing file:** If `/mnt/user-data/uploads/` is empty or the expected file isn't there, ask the user to re-upload rather than proceeding
- **Unsupported format:** If the input is a file type not covered by this skill, say so clearly and suggest the appropriate skill or tool
- **Large file:** If the file exceeds reasonable in-memory limits, process it in chunks or stream it
- **Conflicting instructions:** If the user's request contradicts a constraint in this skill, surface the conflict and ask which takes priority

**---**



## **Notes**

- **Claude Code specifics:** This skill assumes access to `Bash`, `Read`, `Write`, and `Edit` tools. If running in a restricted environment, fall back to [alternative approach].
- **Package installs:** Always use `pip install <pkg> --break-system-packages` in the Claude Code sandbox.
- **Gotcha:** [Known failure mode and how to avoid it]
- **Version note:** [Any behavior that differs across versions of a dependency]

