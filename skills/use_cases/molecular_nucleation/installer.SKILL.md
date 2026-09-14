---
name: use_cases/molecular_nucleation/installer
description: >
  Installer behavior for the molecular nucleation project. Covers pip-installable
  packages, the LAMMPS source-build requirement on Linux/WSL, and the package reuse
  skip logic.
---

# Molecular Nucleation — Installer Skill

Sets up the local venv with all packages needed for the LAMMPS water crystallization workflow.

---

## Current Installer Behavior

1. **Phase 1:** Read or generate `builds/requirements.txt`. Return it as `requirements`. Do NOT call an LLM to generate a new one if the file already exists.
2. **Orchestrator:** Always approves immediately (sets `requirements_approved=True`).
3. **Phase 2:** Check if all packages are already installed. If yes, skip pip install and return. If no, run `pip install -r builds/requirements.txt`.

---

## Package Requirements

```
ovito
numpy
matplotlib
pillow
```

**Engine-specific package (depends on `--engine`):**
- `--engine parsl`: add `parsl>=2024.0.0` to requirements.txt — normal pip package.
- `--engine pycompss`: do NOT add `pycompss` to requirements.txt — `pip install
  pycompss` does not work reliably (see `systems/pycompss` skill); COMPSs is
  hand-installed outside pip and detected automatically via `COMPSS_HOME`.
- `--engine adios`: `adios2` is optional — try installing it, but a failure is not
  fatal (numpy-I/O fallback exists, see `systems/adios` skill).

**LAMMPS:** Must be built from source on Linux/local — it is NOT pip-installable for this use case. On HPC clusters (LCRC/Swing), LAMMPS is pre-installed on the cluster; install only the Python bindings into the venv. See build notes below.

**Do NOT add:** scipy, ase, mdanalysis, h5py, or any other package not listed above. Add `mpi4py` only when the environment knowledge confirms MPI is available.

---

## LAMMPS — Local Build (Linux/WSL)

Build from source with `BUILD_MPI=off`. The pip `lammps` wheel (and builds that link
`libmpi.so`) call `MPI_Init` on import in serial contexts, crashing the worker process
that runs the task regardless of engine (e.g. Parsl's `WorkerLost`). Serial build
avoids this entirely.

### System dependencies (apt)
```
python3-dev build-essential cmake wget git
libfftw3-dev libpng-dev libjpeg-dev
libosmesa6 libgl1 libegl1 libopengl0
libglib2.0-0 libxkbcommon0 libxcb-icccm4 libxcb-image0
libxcb-keysyms1 libxcb-render-util0 libxcb-xinerama0 libxcb-xkb1
libxrender1 libxi6 libxtst6
```

### cmake flags (local/serial)
```
-DBUILD_MPI=off -DBUILD_OMP=off -DBUILD_SHARED_LIBS=on
-DLAMMPS_EXCEPTIONS=on -DPKG_MANYBODY=on -DPKG_MOLECULE=on
-DPKG_KSPACE=on -DPKG_RIGID=on -DPKG_PYTHON=on -DFFT=FFTW3
```
Install to `/usr/local` via `make install`, then set `LD_LIBRARY_PATH=/usr/local/lib`.

### LAMMPS Python bindings (local)
After the shared library is installed, install the Python bindings from the source tarball:
```bash
cd lammps-<version>/python && pip install .
```

### Environment variables (local)
```
LIBGL_ALWAYS_SOFTWARE=1
PYOPENGL_PLATFORM=osmesa
OVITO_GUI_MODE=0
LD_LIBRARY_PATH=/usr/local/lib
```

---

## LAMMPS — HPC Build (LCRC/Swing)

On LCRC, LAMMPS is pre-built on the cluster with MPI support. Do NOT build from source —
install only the Python bindings into the venv from the cluster's existing binary.

```bash
# Find the cluster LAMMPS python bindings and install into venv
cd /path/to/lammps-source/python && pip install .
```

The pre-built binary links `libmpi.so.12` (Intel oneAPI MPI). These paths are needed at
runtime for `from lammps import lammps` to succeed. They are handled automatically by:
- `setup_hpc.sh` — sets `LD_LIBRARY_PATH` for the agent process
- `TASK_ENV` in `servers/<engine>_server.py` (`parsl_server.py`/`pycompss_server.py`/
  `adios_server.py` each define one) — propagates paths to every task subprocess

Run `source setup_hpc.sh` before starting the agent on HPC. No manual `module load` needed.

---

## Notes

- LAMMPS source build takes ~20 min; once installed in the venv it is reused across runs
- `builds/.requirements_hash` can store an MD5 of the last installed requirements for skip detection
