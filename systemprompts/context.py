


PROJECT_LAYOUT = """\
Repo directory tree -- the repo root is mapped to /app/ at runtime:

/app/                                  <- repo root
+-- agent_mcp.py                       <- orchestrator/planner/installer + graph
+-- mcp_explorer.py                    <- explorer agent (ReAct loop, MCP client)
+-- servers/                           <- MCP server backends, one per workflow engine
|   +-- parsl_server.py
|   +-- pycompss_server.py
|   +-- adios_server.py
+-- requirements.txt
+-- .env
+-- data/
|   +-- in.watbox                      <- LAMMPS input script
|   +-- data.init                      <- LAMMPS initial atom positions
|   +-- AW.tersoff                     <- LAMMPS force field parameters
+-- Literature/
|   +-- *.pdf
+-- images/
|   +-- *.png / *.jpg                  <- optional planning diagrams/figures
+-- builds/                            <- installer-generated requirements
|   +-- requirements.txt               <- venv package list
+-- work/                              <- explorer output goes here
    +-- run0/                          <- default working directory
\
"""