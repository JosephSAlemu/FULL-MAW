PROJECT_LAYOUT = """\
Repo directory tree -- the repo root is mapped to /app/ at runtime:

/app/                                  <- repo root
+-- academy_agents.py                  <- the ONLY entry point: builds the graph and runs it
+-- src/
|   +-- mcp_explorer.py                <- explorer agent (ReAct loop, MCP client)
|   +-- agents.py                      <- Planner, Installer, Explorer, Orchestrator
|   +-- states.py                      <- AgentState and the step enum
|   +-- utility.py                     <- skill loading, structured LLM calls
|   +-- tracing/                       <- run tracing, imported as `tracing.*`
|   |   +-- trace_logger.py            <- the event log and its singleton `tracer`
|   |   +-- trace_schema.py            <- pydantic models for every event
|   |   +-- run_archiver.py            <- copies work/run0 + trace.json out of the repo
|   |   +-- merge_explorer_trace.py    <- folds a remote Explorer's events back in
|   +-- servers/                       <- MCP server backends, one per workflow engine
|   |   +-- server.py                  <- base class: every shared MCP tool
|   |   +-- parsl_server.py            <- subclasses of it, engine specifics only
|   |   +-- pycompss_server.py
|   |   +-- adios_sst.py               <- ADIOS2 serial build: SST over TCP
|   |   +-- adios_mpmd.py              <- ADIOS2 MPI build: MPMD rank blocks
|   |   +-- wilkins.py                 <- Wilkins: YAML config + LowFive
|   +-- common/
|   |   +-- consts.py                  <- PROJECT_DIR and every path derived from it
|   |   +-- exceptions.py
|   +-- systemprompts/                 <- core prompt text for each agent
|       +-- context.py                 <- THIS tree, injected into every prompt
|       +-- orchestrator.py  planner.py  explorer.py
+-- skills/
|   +-- agents/  knowledge/  systems/  use_cases/
+-- spack_repo/                        <- this repo's own Spack packages (API v2)
|   +-- maw/
|       +-- repo.yaml                  <- namespace maw; api: v2.0
|       +-- packages/env/package.py    <- maw.env: the activation hook
|       +-- packages/compss/package.py <- maw.compss: COMPSs from the tarball
+-- spack_mpi/                         <- Spack env for the MPI-LINKED engines
|   +-- spack.yaml.in                  <- template; copy to spack.yaml to build
|   +-- deps/                          <- wilkins + LowFive git clones (submodules)
+-- spack.yaml                         <- the MPI-FREE core environment
+-- bootstrap.sh                       <- one-time machine setup
+-- requirements.txt
+-- .env
+-- inputs/                            <- EVERY input lives here
|   +-- data/
|   |   +-- in.watbox                  <- LAMMPS input script
|   |   +-- data.init                  <- LAMMPS initial atom positions
|   |   +-- AW.tersoff                 <- LAMMPS force field parameters
|   +-- literature/                    <- *.pdf
|   +-- images/                        <- *.png / *.jpg, optional planning figures
+-- runs/                              <- per-run logs and traces
+-- work/                              <- GENERATED; everything here is output
    +-- builds/
    |   +-- requirements.txt           <- installer-generated, rewritten every run
    +-- run0/                          <- default working directory
\
"""