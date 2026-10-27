#!/bin/bash
# Sourced by COMPSs on each worker before it starts (project.xml ->
# ComputeNode -> Application -> EnvironmentScript). A non-interactive ssh
# session inherits nothing, so JAVA_HOME has to be stated absolutely here.
export JAVA_HOME="/gpfs/fs1/soft/improv/software/spack-built/linux-rhel8-zen3/gcc-12.3.0/openjdk-21.0.0_35-23zksi2"
export PATH="${JAVA_HOME}/bin:${PATH}"
export LD_LIBRARY_PATH="${JAVA_HOME}/lib/server:${LD_LIBRARY_PATH:-}"
