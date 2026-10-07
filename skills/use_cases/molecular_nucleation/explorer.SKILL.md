---
name: use_cases/molecular_nucleation/explorer
description: >
  Use-case-specific explorer rules for the water crystallization nucleation workflow
  via LAMMPS + OVITO. Covers expressing the whole pipeline as ONE generated workflow
  file, the LAMMPS MPI and Python-API run paths, OVITO diamond structure detection,
  file layout, and known pitfalls.
---

# Molecular Nucleation -- Explorer Skill

Domain-specific guidance for the explorer agent when executing the water crystallization
nucleation workflow (LAMMPS molecular dynamics + OVITO diamond structure detection).

---

## When to Use This Skill

Load this whenever the explorer is executing a workflow involving LAMMPS and OVITO for
molecular nucleation / water crystallization simulation.

---

## Overall Shape: One Workflow File, All Stages

Setup, LAMMPS, OVITO analysis, rendering, GIF assembly, and the timeseries plot are
**one workflow**, written into a single file with `write_workflow(python_code, filename)`
and executed with `run_workflow(filename)`. You never run any of it yourself: no
subprocess from your tools, no CLI call, no bash script, no PBS script, no `qsub`, no
`mpirun` typed by you.

What that file looks like depends on the engine — see the engine reference injected in
your context (`systems/parsl` / `systems/pycompss` / `systems/adios`) for the exact
constructs. In short:

| Engine | Python work (OVITO, rendering, GIF, plot) | CLI/MPI work (LAMMPS) |
|---|---|---|
| parsl | `@python_app` | `@bash_app` whose body returns the command **string** |
| pycompss | `@task` | `@mpi(binary="lmp", runner="mpirun", processes=N)` stacked above `@task` |
| adios | a top-level stage function called from `main()` | a stage function using `subprocess` **inside its body** |

Ordering between stages is expressed with the engine's own dependency mechanism
(Parsl futures / `File` objects, COMPSs `FILE_IN`/`FILE_OUT` parameter types, the call
order inside ADIOS2's `main()`), never by polling or wall-clock timing.

---

## Workflow Overview

The workflow has 3 main stages:
1. **LAMMPS simulation** -- run MD simulation of water molecules, output trajectory frames
2. **OVITO analysis** -- read trajectory frames, detect ice crystal structures
3. **Visualization** -- render frames and generate plots

---

## Stage 0: Setup

Directory creation and input staging are steps **inside** the generated file, not
separate calls you make. Express them as pure Python (`os.makedirs`, `shutil.copy2`)
so they work identically on every engine:

```python
import os, shutil
os.makedirs("/app/work/run0/frames", exist_ok=True)
os.makedirs("/app/work/run0/renders", exist_ok=True)
for f in ("AW.tersoff", "data.init", "in.watbox"):
    shutil.copy2(f"/app/data/{f}", f"/app/work/run0/{f}")
```

Copy all three fresh on every run — the user may have edited `in.watbox`.

---

## Stage 1: LAMMPS Simulation

### Clear stale frames first

Before the run, delete `/app/work/run0/frames/*.lammpstrj`. Otherwise trajectory files
left over from a previous run can be mistaken for this run's output and a failed
simulation looks like a successful one.

### Path A: MPI run (inside PBS with a launcher)

Use this when `get_resources` reports `in_pbs: true` and a launcher. The pip `lammps`
wheel's bundled `lmp` binary does **not** load on this cluster's kernel (elf
segment-layout mismatch) — use the cluster module instead:

- `module load lammps/22Jul2025` (built with gcc 13.2.0 + OpenMPI 5.0.6)
- `module` is a shell function, so the command needs a login shell: `bash -lc "..."`
- Intel-MPI singleton variables must be unset before a real `mpirun`, or MPI init
  fails: `env -u PMI_SIZE -u PMI_RANK -u I_MPI_HYDRA_BOOTSTRAP`

The command is:

```
bash -lc "module load lammps/22Jul2025 && cd /app/work/run0 && \
  env -u PMI_SIZE -u PMI_RANK -u I_MPI_HYDRA_BOOTSTRAP mpirun -n <ranks> lmp -in in.watbox"
```

Per engine:
- **parsl**: that whole string is the return value of a `@bash_app`. Size the executor
  slot for the job: `cores_per_worker=<ranks>`.
- **pycompss**: `mpirun` is never typed — use
  `@mpi(binary="lmp", runner="mpirun", processes=<ranks>)` above
  `@task(script={Type: FILE_IN, Prefix: "-in"})`, with `working_dir="/app/work/run0"`.
  The module environment and the `PMI_SIZE`/`PMI_RANK`/`I_MPI_HYDRA_BOOTSTRAP`
  removals must already be in place in the driver's `os.environ` before the task is
  submitted, because the decorator launches the binary directly with no shell.
  Set `fail_by_exit_value=False` so the exit code comes back for the check below.
- **adios**: a stage function whose body calls `subprocess.run(["bash", "-lc", cmd],
  check=False)` — `subprocess` is legal there but only inside the stage body.

### Path B: Python API, single process (no launcher)

When there is no PBS allocation / no MPI launcher, run LAMMPS in-process from Python
work (a `@python_app`, a `@task`, or an ADIOS2 stage function):

```python
import os
os.chdir("/app/work/run0")          # BEFORE constructing lammps()
from lammps import lammps
lmp = lammps(cmdargs=["-screen", "none"])
lmp.file("/app/work/run0/in.watbox")
lmp.close()
```

`os.chdir('/app/work/run0')` MUST happen before the `lammps` instance is created,
because the dump paths in `in.watbox` are relative to the current working directory.
Do not pip install `lammps`; the bindings are already available.

### Exit code handling

- **Exit 11 is a SIGSEGV during LAMMPS cleanup.** If trajectory frames were written,
  treat the step as **SUCCESS**, not failure. Have the step check for
  `frames/*.lammpstrj` and only propagate a failure when none exist.
- **Exit 143** is an MPI init SIGTERM — a real failure. Confirm `get_resources`
  reported `in_pbs: true` and that the `env -u ...` unsets are present.

Never modify `in.watbox` — it is user-controlled.

### Expected output
- `/app/work/run0/frames/step.*.lammpstrj` — trajectory files
- `/app/work/run0/log.lammps` — LAMMPS log

---

## Stage 2: OVITO Analysis

Python work in the generated file:

```python
import os, csv
from ovito.io import import_file
from ovito.modifiers import IdentifyDiamondModifier

pipeline = import_file("/app/work/run0/frames/step.*.lammpstrj")
pipeline.modifiers.append(IdentifyDiamondModifier())

output_csv = "/app/work/run0/results.csv"
with open(output_csv, "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["frame", "timestep", "cubic_diamond_count", "hexagonal_diamond_count"])
    for i in range(pipeline.source.num_frames):
        data = pipeline.compute(i)
        struct = data.particles["Structure Type"]
        cubic = int(((struct == 1) | (struct == 2) | (struct == 3)).sum())
        hexag = int(((struct == 4) | (struct == 5) | (struct == 6)).sum())
        writer.writerow([i, data.attributes.get("Timestep", i), cubic, hexag])

print(f"Analysis complete: {pipeline.source.num_frames} frames -> {output_csv}")
```

**CRITICAL -- IdentifyDiamondModifier structure type mapping:**

| Type | Meaning |
|---|---|
| 0 | Other (liquid, amorphous water) |
| 1 | Cubic diamond |
| 2 | Cubic diamond (1st neighbor) |
| 3 | Cubic diamond (2nd neighbor) |
| 4 | Hexagonal diamond (wurtzite ice) |
| 5 | Hexagonal diamond (1st neighbor) |
| 6 | Hexagonal diamond (2nd neighbor) |

- Cubic = types 1 + 2 + 3 (NOT just type 1)
- Hexagonal = types 4 + 5 + 6 (NOT just type 4)
- Counting only primary types gives ~10% of the actual crystal count

### Expected output
- `/app/work/run0/results.csv` -- columns: frame, timestep, cubic_diamond_count, hexagonal_diamond_count

---

## Stage 3: Visualization

There are THREE visualization tasks. All three are REQUIRED -- do not skip any.

### 3a: Render individual frames

Render each trajectory frame as a PNG with color-coded atom types using matplotlib
scatter plots.

```python
import os, glob
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from ovito.io import import_file
from ovito.modifiers import IdentifyDiamondModifier

frames_dir = "/app/work/run0/frames"
render_dir = "/app/work/run0/renders"
os.makedirs(render_dir, exist_ok=True)

pipeline = import_file(os.path.join(frames_dir, "step.*.lammpstrj"))
pipeline.modifiers.append(IdentifyDiamondModifier())

for i in range(pipeline.source.num_frames):
    data = pipeline.compute(i)
    pos = np.array(data.particles.positions)
    struct = np.array(data.particles["Structure Type"])

    fig = plt.figure(figsize=(8, 8))
    ax = fig.add_subplot(111, projection="3d")

    # Color mapping (MANDATORY -- do not use default colors)
    # Liquid/Other (type 0): cyan #00BFFF
    mask0 = struct == 0
    if mask0.any():
        ax.scatter(pos[mask0, 0], pos[mask0, 1], pos[mask0, 2],
                   c="#00BFFF", s=25, alpha=0.3, label="Liquid")

    # Cubic diamond (types 1,2,3): blue #0000FF
    mask_c = (struct == 1) | (struct == 2) | (struct == 3)
    if mask_c.any():
        ax.scatter(pos[mask_c, 0], pos[mask_c, 1], pos[mask_c, 2],
                   c="#0000FF", s=25, alpha=0.8, label="Cubic Ice")

    # Hexagonal diamond (types 4,5,6): red #FF2200
    mask_h = (struct == 4) | (struct == 5) | (struct == 6)
    if mask_h.any():
        ax.scatter(pos[mask_h, 0], pos[mask_h, 1], pos[mask_h, 2],
                   c="#FF2200", s=25, alpha=0.8, label="Hex Ice")

    ax.set_title(f"Frame {i}")
    ax.legend(loc="upper right", fontsize=8)
    fig.savefig(os.path.join(render_dir, f"frame_{i:04d}.png"), dpi=100, bbox_inches="tight")
    plt.close(fig)

print(f"Rendered {pipeline.source.num_frames} frames")
```

**Visualization rules (MANDATORY -- do not deviate):**
- Atom size: `s=25` MINIMUM -- default s=2 makes atoms invisible
- Liquid / Other (type 0): color `#00BFFF` (cyan), alpha >= 0.3
- Cubic diamond (types 1-3): color `#0000FF` (blue), alpha >= 0.8
- Hexagonal diamond (types 4-6): color `#FF2200` (red), alpha >= 0.8
- Do NOT use OVITO's default yellow/white rendering

### 3b: Generate animation GIF

Combine the rendered frame PNGs into an animated GIF.

```python
import os, glob
from PIL import Image

render_dir = "/app/work/run0/renders"
frame_files = sorted(glob.glob(os.path.join(render_dir, "frame_*.png")))

if frame_files:
    frames = [Image.open(f) for f in frame_files]
    frames[0].save(
        os.path.join(render_dir, "animation.gif"),
        save_all=True,
        append_images=frames[1:],
        loop=0,
        duration=100,
    )
    print(f"Animation saved: {len(frames)} frames")
else:
    print("No frame PNGs found -- render frames first")
```

- Requires `pillow` (PIL) -- it should be in the venv
- `duration=100` means 100ms per frame
- `loop=0` means infinite loop

### 3c: Nucleation timeseries plot
```python
import os, csv
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

timesteps, cubic_counts, hex_counts = [], [], []
with open("/app/work/run0/results.csv") as f:
    for row in csv.DictReader(f):
        timesteps.append(int(float(row["timestep"])))
        cubic_counts.append(int(row["cubic_diamond_count"]))
        hex_counts.append(int(row["hexagonal_diamond_count"]))

fig, ax = plt.subplots(figsize=(10, 6))
ax.plot(timesteps, cubic_counts, color="#0000FF", marker="o", markersize=4, label="Cubic Diamond (Ice Ic)")
ax.plot(timesteps, hex_counts,   color="#FF2200", marker="s", markersize=4, label="Hexagonal Diamond (Ice Ih)")
total_ice = [c + h for c, h in zip(cubic_counts, hex_counts)]
ax.plot(timesteps, total_ice, "k--", linewidth=1.5, alpha=0.6, label="Total Ice")
ax.set_xlabel("Timestep"); ax.set_ylabel("Ice-like Atoms")
ax.set_title("Water Freezing: Nucleation Progress"); ax.legend(); ax.grid(True, alpha=0.3)

render_dir = "/app/work/run0/renders"
os.makedirs(render_dir, exist_ok=True)
fig.savefig(os.path.join(render_dir, "nucleation_timeseries.png"), dpi=150, bbox_inches="tight")
plt.close(fig)
print("Timeseries plot saved")
```

### Expected output
- `/app/work/run0/renders/frame_*.png` -- per-frame atom renders (color-coded)
- `/app/work/run0/renders/animation.gif` -- animated GIF of crystallization
- `/app/work/run0/renders/nucleation_timeseries.png` -- line chart of ice counts over time

---

## ADIOS2 Engine Notes (when `--engine adios`)

LAMMPS trajectory output (Stage 1) stays native `.lammpstrj` -- **never add a
`dump ... adios ...` line or otherwise try to make LAMMPS itself emit BP
files.** Confirmed by checking `lmp -h`'s own "Installed packages" list on
this cluster's LAMMPS module: ADIOS is not compiled in. Attempting it just
fails with "Unrecognized dump style." `in.watbox` also stays untouched
regardless, per the existing rule.

The generated file calls the `adios2` library **directly** inside the stage
functions. The real inter-stage numerical data this applies to is Stage 2's
per-frame diamond-structure counts (frame, timestep, cubic_diamond_count,
hexagonal_diamond_count) -- the only numbers that flow from one stage to
another in this workflow:

- **Stage 2 (OVITO analysis stage)**: accumulate the per-frame counts and write
  them through `adios2.Stream(path, "w")` with `stream.write(...)`, in the same
  loop that builds the `results.csv` rows. This is the producer side of the real
  transport, not a conversion step bolted on after the fact.
- **Stage 3c (nucleation timeseries stage)**: read those arrays back with
  `adios2.Stream(path, "r")` --
  `for _ in stream.steps(): cubic = stream.read("cubic_diamond_count")` etc. --
  instead of opening `results.csv` directly. Write it, then actually read it
  back; do not keep using the in-memory copy, or no real I/O happened.
- **Stage 3a (per-frame rendering) and 3b (GIF assembly) do NOT need
  ADIOS2.** Stage 3a re-reads the raw trajectory frames directly via OVITO
  for per-atom coloring (not Stage 2's aggregate counts), and 3b just
  combines already-rendered PNGs. Both are final human-facing visual
  artifacts -- same as cosmology's final density-slice PNG, plain files
  regardless of engine mode.

See `systems/adios` skill for the `adios2.Stream` API, the staged-pipeline rules,
and the three-state `engine` field (`adios2` / `adios2-unused` / `adios2-fallback`)
that the trace checks this against.

---

## Common Pitfalls

| Pitfall | Solution |
|---|---|
| LAMMPS can't find data.init | Stage the copies of AW.tersoff, data.init, in.watbox into /app/work/run0/ as a step ordered BEFORE the LAMMPS step |
| Frames directory empty | Check the LAMMPS step's stderr; confirm the copies ran first and that frames/ exists |
| Old frames counted as new output | Delete `frames/*.lammpstrj` at the start of the LAMMPS step |
| LAMMPS exits 11 and the workflow reports failure | Exit 11 is a SIGSEGV on cleanup — if `frames/*.lammpstrj` exist, treat it as success |
| LAMMPS exits 143 | MPI init SIGTERM — confirm `get_resources` reports `in_pbs: true` and that `env -u PMI_SIZE -u PMI_RANK -u I_MPI_HYDRA_BOOTSTRAP` precedes `mpirun` |
| `lmp` fails to load / elf segment-layout error | The pip wheel's bundled binary does not run on this kernel — `module load lammps/22Jul2025` and use that `lmp` |
| `module: command not found` | `module` is a shell function — the command needs a login shell (`bash -lc "..."`) |
| Dump files land in the wrong directory | `os.chdir('/app/work/run0')` before creating the lammps instance (Python API), or `cd /app/work/run0` / `working_dir` for the MPI path |
| OVITO counts are all zero | Use types 1+2+3 for cubic and 4+5+6 for hexagonal (not just 1 and 4) |
| matplotlib display error | Use `matplotlib.use("Agg")` for headless rendering |
| Reaching for a subprocess, shell script, or `qsub` | None of it. Every step lives in the one generated workflow file |

---

## Input Files

- `/app/data/in.watbox` -- LAMMPS input script. Parameters: `run 9000`, `timestep 0.01`, `variable T equal 180`, `variable P equal 1.0`. DO NOT modify.
- `/app/data/data.init` -- initial atom positions
- `/app/data/AW.tersoff` -- Tersoff force field for water
