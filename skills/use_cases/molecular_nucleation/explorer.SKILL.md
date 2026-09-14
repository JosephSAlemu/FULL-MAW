---
name: use_cases/molecular_nucleation/explorer
description: >
  Use-case-specific explorer rules for water crystallization nucleation workflow
  via LAMMPS + OVITO. Covers LAMMPS Python API usage, OVITO diamond structure
  detection, file layout, and known pitfalls.
---

# Molecular Nucleation -- Explorer Skill

Domain-specific guidance for the explorer agent when executing the water crystallization
nucleation workflow (LAMMPS molecular dynamics + OVITO diamond structure detection).

---

## When to Use This Skill

Load this whenever the explorer is executing a workflow involving LAMMPS and OVITO for
molecular nucleation / water crystallization simulation.

---

## Domain-Specific Tools (this use case)

| Tool | Purpose |
|---|---|
| `run_lammps` | Run the LAMMPS simulation. Auto-selects mpirun+binary on HPC, Python API locally. Call directly — never reimplement with `submit_task`. |

---

## Workflow Overview

The workflow has 3 main stages:
1. **LAMMPS simulation** -- run MD simulation of water molecules, output trajectory frames
2. **OVITO analysis** -- read trajectory frames, detect ice crystal structures
3. **Visualization** -- render frames and generate plots

---

## Stage 1: LAMMPS Simulation

### Setup
Use `submit_shell_task` to create directories and copy all input files:
```
mkdir -p /app/work/run0/frames /app/work/run0/renders
cp /app/data/AW.tersoff /app/work/run0/
cp /app/data/data.init /app/work/run0/
cp /app/data/in.watbox /app/work/run0/
```


### Expected output
- `/app/work/run0/frames/step.*.lammpstrj` — trajectory files
- `/app/work/run0/log.lammps` — LAMMPS log

---

## Stage 2: OVITO Analysis

### Run analysis (run_python)
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

### 3a: Render individual frames (run_python)

Render each trajectory frame as a PNG with color-coded atom types using matplotlib scatter plots.

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

### 3b: Generate animation GIF (run_python)

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

### 3c: Nucleation timeseries plot (run_python)
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

The real inter-stage numerical data this applies to is Stage 2's per-frame
diamond-structure counts (frame, timestep, cubic_diamond_count,
hexagonal_diamond_count) -- the only numbers that flow from one stage to
another in this workflow:

- **Stage 2 (OVITO analysis)**: write the per-frame counts with `write_bp`
  instead of (or in addition to) `results.csv` -- one `stream.write(...)`
  call per column per frame inside the same loop that currently builds the
  CSV rows. This is the producer side of the real transport, not a
  conversion step bolted on after the fact.
- **Stage 3c (nucleation timeseries plot)**: read those counts back with
  `read_bp` -- `for _ in stream.steps(): cubic = stream.read("cubic_diamond_count")`
  etc. -- instead of opening `results.csv` directly.
- **Stage 3a (per-frame rendering) and 3b (GIF assembly) do NOT need
  ADIOS2.** Stage 3a re-reads the raw trajectory frames directly via OVITO
  for per-atom coloring (not Stage 2's aggregate counts), and 3b just
  combines already-rendered PNGs. Both are final human-facing visual
  artifacts -- same as cosmology's final density-slice PNG, plain files
  regardless of engine mode.

See `systems/adios` skill for the general `write_bp`/`read_bp` API and the
three-state `engine` field (`adios2` / `adios2-unused` / `adios2-fallback`)
that the trace checks this against.

---

## Common Pitfalls

| Pitfall | Solution |
|---|---|
| LAMMPS can't find data.init | Copy ALL data files to work_dir BEFORE calling run_lammps |
| Frames directory empty | Verify data files were copied before run_lammps; check run_lammps stderr |
| OVITO counts are all zero | Use types 1+2+3 for cubic and 4+5+6 for hexagonal (not just 1 and 4) |
| matplotlib display error | Use `matplotlib.use("Agg")` for headless rendering |
| run_lammps fails exit 143 | MPI init error — the run_lammps tool handles this; check get_resources first |

---

## Input Files

- `/app/data/in.watbox` -- LAMMPS input script. Parameters: `run 9000`, `timestep 0.01`, `variable T equal 180`, `variable P equal 1.0`. DO NOT modify.
- `/app/data/data.init` -- initial atom positions
- `/app/data/AW.tersoff` -- Tersoff force field for water


### Run LAMMPS
Call the `run_lammps` tool directly — do NOT use `submit_task` or write Python code for this:
```
run_lammps(script="in.watbox", work_dir="/app/work/run0")
```
The server handles HPC vs local execution automatically. Never modify in.watbox.
