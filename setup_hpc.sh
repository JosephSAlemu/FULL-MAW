#!/usr/bin/env bash
# Source this script before running the agent on LCRC (Swing/Improv).
# It sets LD_LIBRARY_PATH so the Intel MPI shared libraries (libmpi.so.12,
# libfabric.so.1) are visible to both the agent process and task subprocesses.
#
# Usage:
#   source setup_hpc.sh
#   python academy_agents.py --env hpc --paper 1 --goal "..."
#
# Under --env multi-node the Explorer runs on a compute node that never saw
# your shell, so academy_agents.py sources this same file in that node's
# worker_init. Both processes get one identical environment from one place.
#
# Why this is needed:
#   LAMMPS is compiled with Intel MPI support. Its Python bindings load
#   libmpi.so.12 at import time. Without these paths, you get:
#     OSError: libmpi.so.12: cannot open shared object file: No such file or directory
#
# The same paths are also baked into TASK_ENV in servers/parsl_server.py so
# every submit_task subprocess inherits them automatically. This script covers
# the agent process itself.
#
# No `set -euo pipefail` here, deliberately. A sourced script runs in the
# caller's shell, so those options would stay on after it returned: in an
# interactive `qsub -I` shell the next failing command -- even a typo --
# would exit the shell and end the job, and in a Parsl worker_init `set -u`
# would abort the submit script on the first unset variable after it. This
# script only sets variables, so it changes nothing else about the shell.

# Sourcing is the only way the exports can reach the caller.
if ! (return 0 2>/dev/null); then
    echo "setup_hpc.sh must be sourced, not executed:  source ${BASH_SOURCE[0]}" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# MPI shared libraries required by LAMMPS Python bindings
SWING_MPI="/gpfs/fs1/soft/swing/manual/intel/oneapi/2021.2.0.2883/mpi/2021.2.0/lib/release"
IMPROV_MPI="/gpfs/fs1/soft/improv/software/custom-built/intel-oneapi-toolkit/mpi/2021.15/lib"
IMPROV_FABRIC="/gpfs/fs1/soft/improv/software/custom-built/intel-oneapi-toolkit/mpi/2021.15/opt/mpi/libfabric/lib"

export LD_LIBRARY_PATH="${SWING_MPI}:${IMPROV_MPI}:${IMPROV_FABRIC}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

# points at the project venv with lammps, ovito, etc in it.
# the servers pick this up straight from the environment to launch with the right interpreter
export VENV_PYTHON="${SCRIPT_DIR}/venv3/bin/python3"

echo "LD_LIBRARY_PATH set for Intel MPI (Swing + Improv)"
echo "VENV_PYTHON=${VENV_PYTHON}"