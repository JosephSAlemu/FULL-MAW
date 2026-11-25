"""
Wilkins workflow MCP server.

Wilkins is an in situ workflow system whose data transport is LowFive, an HDF5
Virtual Object Layer plugin. Tasks keep using plain HDF5 calls; LowFive
intercepts them and redirects the data over MPI instead of to a file. So a
Wilkins workflow is not a program that calls a workflow API -- it is a YAML file
describing tasks and the datasets they produce and consume, run by Wilkins'
Python driver.

What is specific to this engine:

  - the config IS the workflow. write_config writes the YAML; run_workflow
    launches the driver on it. The task codes themselves are separate programs
    that use HDF5 and nothing else.
  - THE LOWFIVE CHECK. LowFive is enabled by environment variables, not by the
    config: HDF5_VOL_CONNECTOR names the plugin and HDF5_PLUGIN_PATH says where
    to find it. If they are unset, HDF5 resolves normally and every task writes
    a real .h5 file to disk. The workflow still RUNS -- it just stopped being in
    situ, silently, with no error anywhere. check_lowfive is how that is caught
    before a run, and prepare_run refuses to launch without it.
  - the launcher takes the TOTAL rank count, which is the sum of every task's
    nprocs. Wilkins starts each task on its slice of that one MPI job.

Environment
-----------
In addition to the variables in src/servers/server.py:

    MAW_MPI_LAUNCHER     mpirun/mpiexec/srun. Default: "mpirun".
    MAW_WILKINS_DRIVER   the driver script. Default: wilkins-master.py, which
                         Wilkins installs and expects to be on PATH or in the
                         work directory.
    MAW_WILKINS_MODULE   override the generated module name. Default: the
                         newest under spack_mpi/modules/wilkins/.
    HDF5_VOL_CONNECTOR   LowFive's VOL name. Normally set BY the module.
    HDF5_PLUGIN_PATH     where the plugin lives. Normally set BY the module.

HOW WILKINS REACHES A RUN. Not from this process's environment: the server
loads the generated module itself, in the `bash -lc` that launches the driver.
That is what keeps the core Spack environment MPI-free -- Wilkins and its MPI
exist only inside that one subshell, per run, so a parsl or pycompss run never
carries them.

Which means the LowFive check CANNOT read os.environ: this server starts before
any task runs, so its own environment has neither variable on a run that is
perfectly well configured. _lowfive_env probes them in the same subshell the
launcher uses. With no generated module the server falls back to its own
environment, which is the manual-install path.
"""

import os
import shutil
from typing import Optional

if __package__:
    from .server import Server
else:
    from server import Server


class WilkinsServer(Server):

    ENGINE = "wilkins"
    DESCRIPTION = (
        "MCP server for Wilkins in situ workflows. Write a YAML describing the "
        "tasks and the datasets they exchange, then run the Wilkins driver on it."
    )

    LAUNCH_NOTE = (
        "run_workflow takes the YAML config, not a Python file, and launches "
        "`mpirun -n <total> -l <python> -u <driver> <config.yaml>` where "
        "<total> is the sum of every task's nprocs. Pass ranks=0 and let the "
        "server compute it from the config."
    )

    #: The config is YAML, so the base class's Python syntax check does not
    #: apply and WORKFLOW_CHECKS would be looking for the wrong things.
    #: _check_config does the structural review instead.
    WORKFLOW_CHECKS = ()

    DEFAULT_CONFIG = "wilkins.yaml"

    #: Result of the one subshell probe per server. The answer cannot change
    #: inside a run, and the probe costs a login shell.
    _lowfive_cache: Optional[dict] = None

    # __ LowFive ______________________________________________________________

    #: The two variables that decide whether this is an in situ workflow or a
    #: set of programs writing files to disk. Wilkins' Spack package sets them;
    #: a manual install requires setting them by hand.
    VOL_CONNECTOR_VAR = "HDF5_VOL_CONNECTOR"
    PLUGIN_PATH_VAR = "HDF5_PLUGIN_PATH"

    #: Generated modules this engine needs, in load order. Produced by
    #: `spack -e spack_mpi module tcl refresh`.
    #:
    #: ALL THREE, not just wilkins. py-mpi4py and py-h5py are SIBLING root
    #: specs, not dependencies of wilkins, so `autoload: direct` on the wilkins
    #: module never brings them -- and wilkins-master.py does
    #: `from mpi4py import MPI`, so the driver dies on import without it.
    #: Missing ones are skipped, so a site that supplies them another way
    #: (an MPI-built h5py already on PYTHONPATH) still works.
    MODULE_PACKAGES = ("wilkins", "py-mpi4py", "py-h5py")

    #: The one that decides whether this engine is available at all.
    MPI_MODULE_PACKAGE = "wilkins"

    def module_root(self) -> Optional[str]:
        root = os.environ.get("MAW_REPO_ROOT", self.repo_root)
        d = os.path.join(root, "spack_mpi", "modules")
        return d if os.path.isdir(d) else None

    def _module_for(self, package: str) -> Optional[str]:
        """Newest generated module for `package`, or None.

        Read off the filesystem rather than rebuilt from spack.yaml's
        `projections` template, which would be a second implementation of it.
        Handles lmod (.lua), tcl (.tcl) and extensionless tcl names.
        """
        root = self.module_root()
        d = os.path.join(root or "", package)
        if not root or not os.path.isdir(d):
            return None
        names = sorted(f[:-4] for f in os.listdir(d)
                       if f.endswith(".lua") or f.endswith(".tcl"))
        if not names:
            names = sorted(f for f in os.listdir(d)
                           if os.path.isfile(os.path.join(d, f))
                           and not f.startswith("."))
        return f"{package}/{names[-1]}" if names else None

    def mpi_module(self) -> Optional[str]:
        """The generated wilkins module, or None if it was never built."""
        return (os.environ.get("MAW_WILKINS_MODULE")
                or self._module_for(self.MPI_MODULE_PACKAGE))

    def module_prelude(self) -> list[str]:
        """Shell commands that put Wilkins on this run's environment, or [].

        `module use` first: the generated tree lives under spack_mpi/, not on
        the site MODULEPATH. maw.env prepends it at activation, but a task
        command that ran `module purge` has dropped it again.
        """
        if not self.mpi_module():
            return []
        prelude = []
        root = self.module_root()
        if root:
            prelude.append(f"module use {root}")
        modules = [m for m in (self._module_for(p) for p in self.MODULE_PACKAGES)
                   if m]
        if os.environ.get("MAW_WILKINS_MODULE"):
            modules = [os.environ["MAW_WILKINS_MODULE"]] + modules[1:]
        prelude.append("module load " + " ".join(modules))
        return prelude

    def _lowfive_env(self) -> dict:
        """The two HDF5 variables AS THE LAUNCH WILL SEE THEM.

        Not os.environ. This server starts before any task runs and the module
        is loaded inside the launch subshell, so reading our own environment
        reports what the SERVER sees -- which is nothing, on a run that is
        perfectly well configured. Probed in the same `bash -lc` the launcher
        uses, so the answer is about the environment the driver actually gets.

        Falls back to our own environment when no module was built, which is
        the manual-install path.
        """
        prelude = self.module_prelude()
        if not prelude:
            return {k: os.environ.get(k, "")
                    for k in (self.VOL_CONNECTOR_VAR, self.PLUGIN_PATH_VAR)}
        if self._lowfive_cache is not None:
            return self._lowfive_cache

        probe = (" && ".join(prelude) +
                 f' && printf "%s\\n%s\\n" "${self.VOL_CONNECTOR_VAR}"'
                 f' "${self.PLUGIN_PATH_VAR}"')
        result = self._run(["bash", "-lc", probe], timeout=120)
        lines = (result["stdout"] or "").splitlines()
        self._lowfive_cache = {
            self.VOL_CONNECTOR_VAR: lines[0].strip() if len(lines) > 0 else "",
            self.PLUGIN_PATH_VAR: lines[1].strip() if len(lines) > 1 else "",
            "_module": " ".join(prelude[-1].split()[2:]),
            "_probe_error": result["stderr"][-300:] if result["exit_code"] else "",
        }
        return self._lowfive_cache

    def lowfive_state(self) -> dict:
        """Is LowFive actually the active HDF5 VOL? Reported, with evidence.

        Checked from the environment rather than from the config, because that
        is where HDF5 reads it from. The failure this catches is not a crash:
        with these unset, HDF5 falls back to its native VOL, every task writes
        a real file, and the workflow completes having transported nothing in
        memory. Nothing in the output says so.
        """
        probed = self._lowfive_env()
        connector = probed.get(self.VOL_CONNECTOR_VAR, "")
        plugin_path = probed.get(self.PLUGIN_PATH_VAR, "")

        problems = []
        if not connector:
            problems.append(
                f"{self.VOL_CONNECTOR_VAR} is unset, so HDF5 will use its "
                f"native VOL. Tasks will write real .h5 files and nothing will "
                f"be transported in memory -- the run will succeed and will "
                f"not be in situ."
            )
        elif "lowfive" not in connector.lower():
            problems.append(
                f"{self.VOL_CONNECTOR_VAR} is {connector!r}, which does not "
                f"name LowFive. Some other VOL connector is active."
            )

        if not plugin_path:
            problems.append(
                f"{self.PLUGIN_PATH_VAR} is unset, so HDF5 has nowhere to look "
                f"for the plugin named by {self.VOL_CONNECTOR_VAR}."
            )
        else:
            # Each entry checked for a plugin, not just for existing: a stale
            # path from another install resolves to nothing and HDF5 reports
            # that as "connector not found" rather than as a bad path.
            found = []
            for entry in plugin_path.split(":"):
                if not entry or not os.path.isdir(entry):
                    continue
                found += [
                    f for f in os.listdir(entry)
                    if "lowfive" in f.lower() and ".so" in f
                ]
            if not found:
                problems.append(
                    f"No LowFive plugin found on {self.PLUGIN_PATH_VAR} "
                    f"({plugin_path}). HDF5 will fail to load the connector and "
                    f"fall back to its native VOL."
                )

        return {
            "lowfive_active": not problems,
            self.VOL_CONNECTOR_VAR: connector or None,
            self.PLUGIN_PATH_VAR: plugin_path or None,
            "problems": problems,
            "module": probed.get("_module"),
            **({"probe_error": probed["_probe_error"]}
               if probed.get("_probe_error") else {}),
            **({"fix": (
                f"The generated module is {probed['_module']} and the launcher "
                f"loads it, but it does not export these. Check with "
                f"`module show {probed['_module']}`; if the variables are "
                f"absent, add lowfive to `include:` in spack_mpi/spack.yaml and "
                f"re-run `spack -e . module lmod refresh -y` -- LowFive is what "
                f"sets them, and a module is only generated for packages that "
                f"list is restricted to."
            ) if probed.get("_module") else (
                "No generated wilkins module was found under spack_mpi/modules, "
                "so these must be exported by hand. Build the environment: "
                "`cd spack_mpi && spack -e . install && "
                "spack -e . module lmod refresh -y`."
            )} if problems else {}),
        }

    def register_tools(self) -> None:
        """Add the two tools that exist for this engine alone."""
        for method in (self.write_config, self.check_lowfive):
            self.register_tool(method)

    def check_lowfive(self) -> str:
        """Check that LowFive is the active HDF5 VOL, before running anything.

        Call this FIRST, before writing the config. If it reports
        lowfive_active=false, a Wilkins run will still complete -- every task
        will write a real .h5 file and the workflow will have transported
        nothing in memory. There is no error message anywhere in that outcome,
        which is why it has to be checked rather than observed.

        Returns:
            JSON with lowfive_active, the two HDF5 variables as the server sees
            them, and what to do about any problem
        """
        return self._json({"engine": self.ENGINE, **self.lowfive_state()})

    # __ The config ___________________________________________________________

    def write_config(self, content: str, filename: str = "") -> str:
        """Write the Wilkins workflow YAML. This file IS the workflow.

        The schema is a list of tasks. Each task names the program to run
        (`func`), how many ranks it gets (`nprocs`), and the datasets it
        produces (`outports`) and consumes (`inports`). Wilkins matches an
        outport against an inport by filename and dataset name and builds a
        communication channel for each match -- you do not declare edges.

            tasks:
              - func: "./sim.hx"
                nprocs: 8
                args: ["in.params"]
                outports:
                  - filename: "outfile.h5"
                    dsets:
                      - name: /group1/grid
                        passthru: 0
                        metadata: 1
              - func: "./analysis.hx"
                nprocs: 4
                inports:
                  - filename: "outfile.h5"
                    dsets:
                      - name: /group1/grid
                        passthru: 0
                        metadata: 1

        Per dataset: `metadata: 1` transports it in memory over MPI,
        `passthru: 1` writes it through to a real HDF5 file. Both may be 1 (in
        memory and on disk); both 0 means the dataset goes nowhere. Filenames
        and dataset names support patterns, e.g. `*.h5/particles`.

        A task's `func` is dispatched by its SUFFIX:

        - `.py` -- imported as a module and called, so it **must define
          `main()`**; without one the task silently does nothing. Plain h5py,
          no Wilkins or LowFive calls, no Henson.
        - anything else -- a Henson-linked, position-independent shared object
          named `./<name>.hx`. That is Wilkins' execution model, not an option:
          a plain MPI binary will not work as a task without that link step.

        Keep `taskCount` out of a Python workflow. The driver sets its ensemble
        flag once and never clears it, so from the first `taskCount > 1` task
        onward every task -- including a later `.py` one -- is looked up as
        `./<func>.hx`.

        Args:
            content: the full YAML document
            filename: config file name (default: wilkins.yaml)

        Returns:
            JSON with the path written, the task/rank totals parsed from it,
            and any structural warnings
        """
        name = filename or self.DEFAULT_CONFIG
        # Rejected here rather than by the driver, which regex-matches
        # `(.*yaml)` and exits 1 after the launch. Cheap to catch now.
        if not name.endswith("yaml"):
            return self._json({
                "status": "rejected",
                "engine": self.ENGINE,
                "error": f"config filename must end in 'yaml', got {name!r}. "
                         f"Wilkins rejects '.yml' outright.",
            })
        try:
            path = self._resolve_write_path(name)
        except ValueError as e:
            return self._json({"status": "rejected", "error": str(e)})
        os.makedirs(os.path.dirname(path) or self.work_dir, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

        parsed, parse_error = self._parse_config(path)
        warnings = self._check_config(parsed) if parsed else []

        payload = {
            "status": "written",
            "engine": self.ENGINE,
            "path": path,
            "bytes": len(content.encode()),
        }
        if parse_error:
            # Not a rejection: a config that does not parse is still on disk
            # and still editable, and rejecting it would leave the agent with
            # nothing to read back and fix.
            payload["parse_error"] = parse_error
            payload["warnings"] = [
                "This file is not valid YAML, so the server could not check it "
                "and the Wilkins driver will fail to read it."
            ]
        else:
            payload["tasks"] = len(parsed.get("tasks", []))
            payload["total_ranks"] = self.total_ranks(parsed)
            if warnings:
                payload["warnings"] = warnings

        payload["next"] = (
            "Write each task's program, then call run_workflow(filename="
            f"'{name}', ranks=0) -- ranks=0 lets the server use the total above."
        )
        return self._json(payload)

    def _parse_config(self, path: str) -> tuple:
        """(parsed, error). PyYAML is a Wilkins dependency, so it is available."""
        try:
            import yaml
        except ImportError:
            return None, ("PyYAML is not importable, so the config cannot be "
                          "checked here. Wilkins' own driver needs it too -- "
                          "check_package('yaml').")
        try:
            with open(path) as f:
                return yaml.safe_load(f) or {}, None
        except Exception as e:
            return None, str(e)

    def total_ranks(self, parsed: dict) -> int:
        """Sum of taskCount x nprocs over every task -- what the launcher needs.

        taskCount, NOT just nprocs: an ensemble task runs taskCount instances of
        nprocs ranks each, and Wilkins assigns every instance its own contiguous
        block. Summing nprocs alone under-counts an ensemble, and launching
        short does not run a smaller workflow -- it starves the tasks at the
        tail, which never run. (Upstream's own generate_run_script.py has this
        bug; do not copy its number.)
        """
        total = 0
        for task in parsed.get("tasks", []) or []:
            if not isinstance(task, dict):
                continue
            try:
                nprocs = int(task.get("nprocs", 1))
            except (TypeError, ValueError):
                nprocs = 1
            try:
                count = max(1, int(task.get("taskCount", 1)))
            except (TypeError, ValueError):
                count = 1
            total += count * nprocs
        return total

    def _check_config(self, parsed: dict) -> list:
        """Structural review of a parsed config. Warnings, never rejections."""
        notes: list = []
        tasks = parsed.get("tasks")

        if not isinstance(tasks, list) or not tasks:
            return ["No `tasks:` list. The config is a list of tasks under a "
                    "top-level `tasks:` key."]
        if len(tasks) < 2:
            notes.append(
                "Only one task. Wilkins couples tasks -- with one there is "
                "nothing to couple it to, and no channel is created."
            )

        outs: dict = {}
        ins: dict = {}
        for i, task in enumerate(tasks):
            if not isinstance(task, dict):
                notes.append(f"Task {i} is not a mapping.")
                continue
            if not task.get("func"):
                notes.append(f"Task {i}: no `func` -- nothing to run.")
            if "nprocs" not in task:
                # Not a default: Wilkins reads nprocs unguarded, so a missing
                # one throws a YAML parser exception and exits 1.
                notes.append(
                    f"Task {i}: no `nprocs`. It is REQUIRED and unguarded -- "
                    f"Wilkins throws a YAML parser exception and exits."
                )
            for port_key, bucket in (("outports", outs), ("inports", ins)):
                for port in task.get(port_key, []) or []:
                    if not isinstance(port, dict):
                        continue
                    fname = port.get("filename", "")
                    dsets = port.get("dsets", []) or []
                    if not dsets:
                        notes.append(
                            f"Task {i}, {port_key} {fname}: no `dsets`, so this "
                            f"port creates no channel and is silently ignored."
                        )
                    for dset in dsets:
                        if not isinstance(dset, dict):
                            continue
                        bucket.setdefault(f"{fname}{dset.get('name', '')}",
                                          []).append(i)
                        # Omitting BOTH is fine -- Wilkins defaults metadata to 1
                        # and passthru to 0, i.e. in-memory. Only an explicit
                        # 0/0 is wrong, and upstream makes that a hard error.
                        if (str(dset.get("metadata", 1)) == "0"
                                and str(dset.get("passthru", 0)) == "0"):
                            notes.append(
                                f"Task {i}, {port_key} {fname}"
                                f"{dset.get('name', '')}: metadata and passthru "
                                f"are both 0. Wilkins exits with 'Either "
                                f"metadata or passthru must be enabled'."
                            )
                        elif (str(dset.get("metadata", 1)) != "0"
                                and str(dset.get("passthru", 0)) != "0"):
                            notes.append(
                                f"Task {i}, {port_key} {fname}"
                                f"{dset.get('name', '')}: metadata and passthru "
                                f"are both set. LowFive does not do both -- "
                                f"memory wins and passthru is ignored."
                            )

        # An inport nothing produces is a real defect: with metadata it is a
        # hard error naming the port, and with passthru it silently degrades to
        # plain file I/O that transports nothing.
        #
        # Matched with fnmatch in BOTH directions, because Wilkins matches
        # globs: a literal inport "outfile.h5" IS produced by an outport
        # "*.h5". Comparing the key strings alone reported that as unproduced.
        from fnmatch import fnmatch

        for key, consumers in ins.items():
            if any(key == o or fnmatch(key, o) or fnmatch(o, key) for o in outs):
                continue
            notes.append(
                f"Task(s) {consumers} read {key!r}, which no task's outports "
                f"produce. Wilkins builds channels by glob-matching these "
                f"names: in memory mode it exits with 'No matching link "
                f"found'; in passthru mode it silently reads a real file "
                f"instead and transports nothing."
            )
        return notes

    # __ Engine specifics ______________________________________________________

    @property
    def driver(self) -> str:
        """Wilkins' Python driver. On PATH, in the work directory, or named."""
        explicit = os.environ.get("MAW_WILKINS_DRIVER", "")
        if explicit:
            return explicit
        local = os.path.join(self.work_dir, "wilkins-master.py")
        if os.path.isfile(local):
            return local
        found = shutil.which("wilkins-master.py")
        # Returned by name even when not found, so the failure is mpirun's
        # "No such file" naming the driver rather than a server error that
        # looks like a config problem.
        return found or "wilkins-master.py"

    def launch_command(self, workflow_path: str, args: list[str], ranks: int) -> list[str]:
        """`mpirun -n <total> -l <python> -u <driver> <config.yaml>`.

        `-l` labels each rank's output, which is how a task's message is
        attributable when every task writes to one stream. `-u` unbuffers the
        driver, without which a hung workflow shows nothing at all.

        ranks=0 means "use the config": the total is the sum of every task's
        nprocs. An explicit `ranks` overrides it, which is almost always a
        mistake -- too few starves a task of the ranks it declared.
        """
        total = ranks if ranks and ranks > 0 else 0
        if not total:
            parsed, _ = self._parse_config(workflow_path)
            total = self.total_ranks(parsed) if parsed else 0
        if not total:
            raise ValueError(
                f"Could not determine a rank count from {workflow_path}. "
                f"Every task needs an `nprocs`, or pass ranks explicitly."
            )
        command = [self.mpi_launcher, "-n", str(total), "-l",
                   self.python, "-u", self.driver, workflow_path, *args]

        # The module load is shell and the launcher is argv, so they are joined
        # through `bash -lc`. This is what keeps the core environment MPI-free:
        # Wilkins and its MPI arrive here, per run, and nowhere else.
        prelude = self.module_prelude()
        if prelude:
            return ["bash", "-lc",
                    " && ".join(prelude) + " && " + " ".join(command)]
        return command

    def prepare_run(self, workflow_path: str) -> dict:
        """Report the LowFive state, and refuse the run if it is wrong.

        The one place in this server that blocks rather than warns. A run with
        LowFive inactive completes successfully having written files to disk
        instead of transporting anything -- there is no failure to observe
        afterwards, so the only useful moment is before.
        """
        state = self.lowfive_state()
        if not state["lowfive_active"]:
            raise RuntimeError(
                "LowFive is not the active HDF5 VOL, so this would run as "
                "ordinary file I/O and report success: "
                + "; ".join(state["problems"])
                + ". " + state.get("fix", "")
            )

        parsed, _ = self._parse_config(workflow_path)
        return {
            "lowfive": {k: state[k] for k in
                        ("lowfive_active", self.VOL_CONNECTOR_VAR,
                         self.PLUGIN_PATH_VAR)},
            "wilkins_module": self.mpi_module(),
            "driver": self.driver,
            **({"config_tasks": len(parsed.get("tasks", [])),
                "config_total_ranks": self.total_ranks(parsed)} if parsed else {}),
        }

    def resource_info(self) -> dict:
        """Rank totals come from the config here, and LowFive state matters."""
        return {
            "driver": self.driver,
            "launcher": self.mpi_launcher,
            "wilkins_module": self.mpi_module(),
            **self.lowfive_state(),
            "note": (
                "The workflow is the YAML config, not a Python file: write it "
                "with write_config, then run_workflow(filename='wilkins.yaml', "
                "ranks=0). The rank count is the SUM of every task's nprocs and "
                "the server reads it from the config -- do not pass ranks. Each "
                "task's program must be linked against Henson and built "
                "position independent; a plain MPI binary cannot be a task. "
                "This engine is bound to the MPI Wilkins was built against, so "
                "a domain using a different one cannot use it."
            ),
        }


if __name__ == "__main__":
    WilkinsServer.main()
