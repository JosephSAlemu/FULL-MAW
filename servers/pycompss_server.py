"""
PyCOMPSs workflow MCP server.

Everything the explorer calls is inherited from Server. What is specific to
PyCOMPSs is:

  - the launch command. PyCOMPSs is used ONLY as a pip install in the venv, so
    the application is started with `<python> -m pycompss`, which takes the
    same flags as the `runcompss` script and infers the interpreter from the
    python it was invoked with. No `runcompss` binary, no COMPSS_HOME, nothing
    on PATH: the same interpreter that every other tool here uses is the one
    that brings up the runtime, runs the application inside it, and shuts it
    down. This server hosts no runtime of its own.

    The `-m pycompss` part is not optional and not cosmetic. Running the same
    file as plain `<python> workflow.py` executes it SEQUENTIALLY, ignoring
    every PyCOMPSs import, decorator and API call -- it exits 0 having
    parallelised nothing. That is the one failure this engine can produce that
    looks exactly like success.

  - the TOPOLOGY, which this server now builds itself. COMPSs does not read
    the allocation from PBS. It reads it from project.xml/resources.xml, and
    it reaches the hosts named there over SSH. That is the whole difference
    from Parsl, where a @bash_app's mpiexec inherits $PBS_NODEFILE and spans
    nodes with the runtime knowing nothing about them. Under COMPSs a node
    that is not in the XML does not exist, however plainly it sits in the
    allocation.

    An agent asked to write that XML by hand got it wrong in the expensive
    direction: it declared the master only, COMPSs correctly refused
    @mpi(processes=8) against 4 slots, and the agent then set
    OMPI_MCA_rmaps_base_oversubscribe=1 to silence the refusal -- stacking 8
    ranks onto one node's 4 slots and leaving the second node of a 2-node
    allocation idle, while still reporting success. The topology is an
    environment fact with one right answer, so it is derived here, once, from
    PBS_NODEFILE plus a real SSH probe, rather than guessed per run.

  - write_xml, which remains as an override for when the generated topology is
    wrong. Writing a kind marks it, and the generator leaves that file alone
    for the rest of the session.

Environment
-----------
In addition to the variables in servers/server.py:

    MAW_COMPSS_PROJECT_XML    project.xml to pass.   Default: <work_dir>/project.xml
    MAW_COMPSS_RESOURCES_XML  resources.xml to pass. Default: <work_dir>/resources.xml
    MAW_COMPSS_FLAGS          extra runcompss flags, space-separated. Optional.
    MAW_COMPSS_INSTALL_DIR    COMPSs home on the workers. Default: derived from
                              pycompss.__file__, i.e. the venv's own copy.
    MAW_COMPSS_WORKER_DIR     per-worker scratch. Default: /tmp/COMPSsWorker
    MAW_COMPSS_PORTS          NIO adaptor port range "min-max". Default: 43001-43100
    MAW_COMPSS_AUTO_XML       1 (default) generate the topology before each run;
                              0 never generate; "force" overwrite even files
                              written with write_xml.
    MAW_COMPSS_SSH_PROBE      1 (default) probe SSH before declaring a remote
                              host; 0 trust the allocation and declare every
                              host in PBS_NODEFILE without checking.

There is deliberately no variable for the launcher. `python -m pycompss` is a
property of the interpreter in MAW_PYTHON, so pointing at a different COMPSs
than the one pip put in that venv is not something this server will help with.
"""

import os
import re
import shlex

import pycompss

if __package__:
    from .server import Server
else:
    from server import Server


class PyCompssServer(Server):

    ENGINE = "pycompss"
    DESCRIPTION = (
        "MCP server for the PyCOMPSs workflow engine (pip-installed in the venv). "
        "The server builds the XML topology from the allocation; write one "
        "PyCOMPSs application file, then run it."
    )

    LAUNCH_NOTE = (
        "The file is run as `<python> -m pycompss [--project/--resources] "
        "workflow.py`, using the pycompss pip-installed in this venv. The "
        "module launcher owns the runtime lifecycle, so the file must NOT call "
        "compss_start() or compss_stop(). Rank counts come from "
        "@mpi(processes=N) inside the file, not from `ranks` here. The topology "
        "XML is generated from the allocation before every run -- check "
        "get_resources()['compss_topology'] for how many slots actually exist "
        "before choosing N."
    )

    #: Which XML goes where. Also what write_xml accepts.
    XML_KINDS = ("project", "resources")

    WORKFLOW_CHECKS = (
        (
            ("@task", "@task("),
            "No @task found. Every unit of work should be a COMPSs task: @task "
            "for Python work, or @binary/@mpi stacked above @task for a "
            "command-line program (body `pass`).",
        ),
        (
            ("from pycompss", "import pycompss"),
            "Nothing is imported from pycompss, so no decorator in this file is "
            "a real COMPSs task.",
        ),
        (
            ('if __name__', "def main("),
            "No main() and no __main__ guard. `python -m pycompss` executes this "
            "file as a program, so give it an entry point.",
        ),
        (
            ("oversubscribe",),
            None,  # see workflow_notes: this one fires when PRESENT, not absent
        ),
    )

    #: Activates the current virtualenv on the worker nodes. The whole premise
    #: here is a pip install in a venv, so without this the workers import a
    #: pycompss that is not the one the master is running.
    PROPAGATE_VENV_FLAG = "--python_propagate_virtual_environment=true"

    def __init__(self) -> None:
        self.extra_flags = shlex.split(os.environ.get("MAW_COMPSS_FLAGS", ""))
        super().__init__()  # sets work_dir, which the XML defaults hang off
        self.xml_paths = {
            kind: os.environ.get(
                f"MAW_COMPSS_{kind.upper()}_XML",
                os.path.join(self.work_dir, f"{kind}.xml"),
            )
            for kind in self.XML_KINDS
        }
        #: Kinds the agent wrote by hand. The generator leaves these alone.
        self.xml_overridden: dict[str, bool] = {k: False for k in self.XML_KINDS}
        #: host -> reachable. Probed at most once per host per session: an SSH
        #: round trip is cheap but get_resources is called repeatedly, and the
        #: answer cannot change inside one allocation.
        self._ssh_cache: dict[str, tuple[bool, str]] = {}
        self._topology: dict = {}

    # __ Topology ______________________________________________________________

    @property
    def compss_install_dir(self) -> str:
        """COMPSs home as the workers need to see it.

        Derived from the imported package rather than written down: the pip
        install puts it at <site-packages>/pycompss/COMPSs, and a guessed
        /opt/COMPSs fails on the worker with nothing useful on the master's
        stderr. Overridable for an installation the workers mount elsewhere.
        """
        return os.environ.get(
            "MAW_COMPSS_INSTALL_DIR",
            os.path.join(os.path.dirname(os.path.abspath(pycompss.__file__)), "COMPSs"),
        )

    @staticmethod
    def fqdn(host: str) -> str:
        """Fully-qualified name for a host from PBS_NODEFILE.

        NEVER "localhost", and never a short name. COMPSs starts its worker by
        ssh-ing to the name in project.xml, and on this site:

          * `ssh localhost` is REFUSED (publickey,hostbased) because sshd does
            not consult ~/.ssh/authorized_keys -- so COMPSs' own
            default_project.xml, which declares <ComputeNode Name="localhost">,
            can never start a worker here. That is why a run with no --project
            hangs: the default is not "no topology", it is localhost-over-ssh.
          * `ssh <fqdn>` SUCCEEDS via hostbased auth.

        So the FQDN is the only name that works, and it is what goes in the XML.
        """
        import socket

        if "." in host:
            return host
        try:
            return socket.getfqdn(host) or host
        except OSError:
            return host

    @property
    def worker_env_script(self) -> str:
        """Script COMPSs sources on each worker before starting it.

        Preference order, first that exists:
          1. MAW_COMPSS_ENV_SCRIPT -- explicit override.
          2. <repo>/worker_env.sh -- a minimal file holding JAVA_HOME, PATH and
             LD_LIBRARY_PATH. This is the shape that was verified to start a
             worker here, and it is narrow enough to be safe in a
             non-interactive shell.
          3. <repo>/setup_hpc.sh -- the full environment. Also carries the HACC
             platform environment that hacc_tpm needs, so it is the better
             choice once a real binary runs; it just does far more work in a
             context that inherits nothing.

        Returns "" when none exists, which worker_startable reports as the
        JAVA_HOME failure it will cause.
        """
        explicit = os.environ.get("MAW_COMPSS_ENV_SCRIPT", "")
        if explicit:
            return explicit if os.path.isfile(explicit) else ""
        for name in ("worker_env.sh", "setup_hpc.sh"):
            path = os.path.join(self.repo_root, name)
            if os.path.isfile(path):
                return path
        return ""

    def worker_startable(self, host: str) -> tuple[bool, str]:
        """Can COMPSs actually start a worker on `host`? Returns (ok, reason).

        Two checks, because each has produced a silent hang here:

        1. BatchMode ssh. It fails instead of prompting, so this gives a clean
           answer in a non-interactive server. Without it a declared
           ComputeNode yields a runtime that comes up, never reaches its
           worker, and schedules nothing.
        2. JAVA_HOME *as the worker will see it* -- i.e. after sourcing the
           environment script, over NON-interactive ssh, which inherits none of
           the master's environment. The worker runs
               find "${JAVA_HOME}"/lib/ -name libjvm.so
           and with JAVA_HOME empty dies with "Can't find JVM libraries in
           JAVA_HOME", then COMPSs retries forever. Checking here turns an
           hour of apparent hang into one line in get_resources.
        """
        if host in self._ssh_cache:
            return self._ssh_cache[host]
        if os.environ.get("MAW_COMPSS_SSH_PROBE", "1") == "0":
            self._ssh_cache[host] = (True, "probe disabled")
            return self._ssh_cache[host]

        env_script = self.worker_env_script
        probe = "echo \"JAVA_HOME=[$JAVA_HOME]\"; ls \"$JAVA_HOME\"/lib/server/libjvm.so"
        if env_script:
            probe = f"source '{env_script}' >/dev/null 2>&1; {probe}"

        result = self._run(
            [
                "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=10", host, probe,
            ],
            timeout=30,
        )
        if result["exit_code"] != 0:
            err = (result["stderr"] or result["stdout"] or "").strip()[-200:]
            if "JAVA_HOME=[]" in (result["stdout"] or ""):
                ok, reason = False, (
                    f"ssh to {host} works but JAVA_HOME is empty there. The "
                    f"worker will fail with \"Can't find JVM libraries in "
                    f"JAVA_HOME\". Point MAW_COMPSS_ENV_SCRIPT at a script, "
                    f"readable from every node, that exports JAVA_HOME."
                )
            else:
                ok, reason = False, f"ssh to {host} failed: {err}"
        else:
            ok, reason = True, "ssh ok, JAVA_HOME resolves on the worker"
        self._ssh_cache[host] = (ok, reason)
        return self._ssh_cache[host]

    def plan_topology(self) -> dict:
        """Decide which hosts COMPSs is told about, and how many slots each has.

        EVERY host -- including this one -- is declared as a <ComputeNode> by
        FQDN, and <MasterNode/> is always left BARE.

        There is deliberately no "master only" mode any more. An earlier
        version fell back to a bare <MasterNode> with computing units when the
        SSH probe failed, on the theory that tasks would then run in the
        master's own slots. That configuration does not work: COMPSs schedules
        onto resources from resources.xml, a bare master contributes none, and
        the run hangs with an empty jobs/ directory and no error. The verified
        working shape is a ComputeNode for the local node, reached over ssh to
        its FQDN -- which is exactly what a one-node allocation gets here.

        Returns the plan rather than the XML, so get_resources can report it
        and the agent can size @mpi(processes=N) against slots that exist
        instead of discovering the limit as "not enough slots available".
        """
        import socket

        info = self._detect_resources()
        raw_hosts = [h for h in (info.get("nodelist") or "").split(",") if h]
        units = max(1, int(info.get("ranks_per_node") or 1))

        # Outside PBS, or with an unreadable nodefile, the one host there is is
        # this one. Still a ComputeNode, still by FQDN.
        if not raw_hosts:
            raw_hosts = [socket.getfqdn()]

        hosts = []
        for host in raw_hosts:
            name = self.fqdn(host)
            if name not in hosts:
                hosts.append(name)

        usable, unusable, reasons = [], [], {}
        for host in hosts:
            ok, why = self.worker_startable(host)
            reasons[host] = why
            (usable if ok else unusable).append(host)

        plan = {
            "mode": "multi_node" if len(usable) > 1 else "single_node",
            "hosts": usable,
            "units_per_host": units,
            "total_slots": units * len(usable),
            "unusable_hosts": unusable,
            "probe": reasons,
            "env_script": self.worker_env_script,
        }

        if not usable:
            # Nothing can run. Say so loudly rather than emitting an XML that
            # produces a silent hang.
            plan["reason"] = (
                "NO host can start a COMPSs worker, so no task can execute. "
                + "; ".join(f"{h}: {reasons[h]}" for h in unusable)
                + ". Report this as an environment gap -- it is not fixable "
                "from the workflow file."
            )
            plan["total_slots"] = 0
        else:
            plan["reason"] = (
                f"{len(usable)} of {len(hosts)} allocated host(s) can start a "
                f"worker ({units} units each)"
                + (f"; unusable: {', '.join(unusable)}" if unusable else "")
            )
        return plan

    def build_topology_xml(self, plan: dict) -> tuple[str, str]:
        """Render (project.xml, resources.xml) for a plan from plan_topology.

        Multi-node layout: MasterNode is declared BARE, with no <Processor>, and
        every allocated host -- the master's own included -- appears as a
        ComputeNode. Giving the master computing units as well would double-count
        the node the JVM is already on, so COMPSs would believe in slots that do
        not exist and schedule into them.
        """
        head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        units = plan["units_per_host"]
        worker_dir = os.environ.get("MAW_COMPSS_WORKER_DIR", "/tmp/COMPSsWorker")
        ports = os.environ.get("MAW_COMPSS_PORTS", "43001-43100")
        min_port, _, max_port = ports.partition("-")
        install = self.compss_install_dir

        # COMPSs starts each remote worker over SSH, and a NON-INTERACTIVE ssh
        # session inherits none of the master's environment. The worker then runs
        # Runtime/scripts/system/runtime/bindings.sh, which does
        #
        #     libjava=$(find "${JAVA_HOME}"/lib/ -name libjvm.so | head -n 1)
        #
        # so an unset JAVA_HOME becomes `find /lib/ ...`, finds nothing, and the
        # worker dies with "Can't find JVM libraries in JAVA_HOME" -- while the
        # master, which has the variable, starts fine. Every task then sits
        # unscheduled and the run looks like a hang.
        #
        # <EnvironmentScript> is COMPSs' own answer: a path it sources on the
        # worker before starting it (project_schema.xsd -> ApplicationType, read
        # by WorkerStarter.getEnvScriptPaht). Pointing it at setup_hpc.sh keeps
        # the environment defined in exactly one place for the master, the
        # Parsl worker_init and the COMPSs workers alike -- rather than in a
        # ~/.bashrc that is per-user, per-machine and usually guarded against
        # non-interactive shells anyway.
        #
        # The script must be on a filesystem every node can see. It is, GPFS.
        # Resolved by the worker_env_script property, so the SSH probe and the
        # generated XML can never disagree about which script the worker gets.
        env_script = self.worker_env_script
        app_block = ""
        if env_script and os.path.isfile(env_script):
            app_block = ("        <Application>\n"
                         f"            <EnvironmentScript>{env_script}"
                         "</EnvironmentScript>\n"
                         "        </Application>\n")
        else:
            print(
                f"WARNING: no environment script at {env_script!r}. COMPSs workers "
                f"start over SSH with no inherited environment, so they will fail "
                f"with \"Can't find JVM libraries in JAVA_HOME\". Set "
                f"MAW_COMPSS_ENV_SCRIPT to a script, readable from every node, "
                f"that exports JAVA_HOME.",
                file=__import__("sys").stderr,
            )

        project = [f"{head}<Project>\n", "    <MasterNode/>\n"]
        resources = [f"{head}<ResourcesList>\n"]
        for host in plan["hosts"]:
            project += [
                f'    <ComputeNode Name="{host}">\n',
                f"        <InstallDir>{install}</InstallDir>\n",
                f"        <WorkingDir>{worker_dir}</WorkingDir>\n",
                app_block,
                f"        <LimitOfTasks>{units}</LimitOfTasks>\n",
                "    </ComputeNode>\n",
            ]
            resources += [
                f'    <ComputeNode Name="{host}">\n',
                '        <Processor Name="MainProcessor">\n',
                f"            <ComputingUnits>{units}</ComputingUnits>\n",
                "            <Architecture>amd64</Architecture>\n",
                "        </Processor>\n",
                "        <Adaptors>\n",
                '            <Adaptor Name="es.bsc.compss.nio.master.NIOAdaptor">\n',
                "                <SubmissionSystem>\n",
                "                    <Interactive/>\n",
                "                </SubmissionSystem>\n",
                "                <Ports>\n",
                f"                    <MinPort>{min_port}</MinPort>\n",
                f"                    <MaxPort>{max_port or min_port}</MaxPort>\n",
                "                </Ports>\n",
                "            </Adaptor>\n",
                "        </Adaptors>\n",
                "    </ComputeNode>\n",
            ]
        project.append("</Project>\n")
        resources.append("</ResourcesList>\n")
        return "".join(project), "".join(resources)

    def generate_topology(self, force: bool = False) -> dict:
        """Write the XML pair from the current allocation. Returns the plan.

        Called from prepare_run, so the files are correct for every run without
        the agent spending iterations on them. A kind the agent wrote with
        write_xml is left alone unless `force`.
        """
        plan = self.plan_topology()
        project_xml, resources_xml = self.build_topology_xml(plan)
        rendered = {"project": project_xml, "resources": resources_xml}

        written, skipped = [], []
        for kind, path in self.xml_paths.items():
            if self.xml_overridden[kind] and not force:
                skipped.append(kind)
                continue
            os.makedirs(os.path.dirname(path) or self.work_dir, exist_ok=True)
            with open(path, "w") as f:
                f.write(rendered[kind])
            written.append(kind)

        plan = dict(plan)
        plan["xml_written"] = written
        plan["xml_kept_from_write_xml"] = skipped
        plan["install_dir"] = self.compss_install_dir
        self._topology = plan
        return plan

    # __ Hooks _________________________________________________________________

    def prepare_run(self, workflow_path: str) -> dict:
        """Generate the topology immediately before the run.

        Here rather than in __init__ because the allocation is read from the
        environment at call time: the server may outlive one PBS job, and a
        stale nodelist is worse than none.
        """
        if os.environ.get("MAW_COMPSS_AUTO_XML", "1") == "0":
            return {"compss_topology": {"mode": "not generated (MAW_COMPSS_AUTO_XML=0)"}}
        force = os.environ.get("MAW_COMPSS_AUTO_XML") == "force"
        return {"compss_topology": self.generate_topology(force=force)}

    def workflow_notes(self, code: str) -> list[str]:
        """Inherited checks, plus the ones that fire on PRESENCE.

        WORKFLOW_CHECKS reports what is MISSING. Oversubscription and
        self-managed launchers are the opposite: they are present, they work,
        and they quietly invalidate the run. Warning beats rejecting -- a false
        rejection leaves the agent with no way forward -- but these need saying.
        """
        notes = [
            message for alternatives, message in self.WORKFLOW_CHECKS
            if message is not None
            and not any(alternative in code for alternative in alternatives)
        ]

        # The expensive one. COMPSs 3.4's mpi.py assigns `processes` to the
        # task's NODE reservation and defaults processes_per_node to 1, so
        # @mpi(processes=8) alone asks for eight nodes at one rank each and
        # fails with "not enough slots available" on any smaller allocation.
        # Cheap to check here; an hour of confusion otherwise.
        if "@mpi(" in code and "processes_per_node" not in code:
            plan = self._topology or self.plan_topology()
            notes.append(
                "An @mpi task declares processes= but not processes_per_node. "
                "In COMPSs 3.4 that parameter defaults to 1 and `processes` is "
                "read as the number of NODES, so this reserves one rank per "
                "node and will fail with 'not enough slots available'. Set "
                f"processes_per_node={plan.get('units_per_host', 1)} "
                "(compss_topology.units_per_host)."
            )

        if "oversubscribe" in code or "rmaps_base_oversubscribe" in code:
            notes.append(
                "This file sets MPI oversubscription. That silences COMPSs's "
                "'not enough slots' refusal by stacking ranks onto fewer slots "
                "than they need, which wastes the rest of the allocation and "
                "changes the result. It is almost always a missing "
                "processes_per_node rather than a real shortage -- read "
                "get_resources()['compss_topology'] and size the task to it."
            )

        if "subprocess" in code or "os.system" in code or "os.popen" in code:
            notes.append(
                "This file shells out directly. That takes the work out of "
                "COMPSs: the task graph records nothing about the program and "
                "the run is not reproducible by the engine it claims to use. "
                "A command-line program is @binary; an MPI program is @mpi, "
                "which builds `mpirun -np N <flags> <binary>` for you."
            )

        # Precise patterns, not a `".sh" in code` substring test -- that one
        # fired on `cat.shape[0]` and warned about a perfectly good file.
        # Matches: a shell interpreter as the binary, a shebang being written,
        # or a quoted path ending in .sh/.bash.
        shell_patterns = (
            r"""binary\s*=\s*["'](?:/[\w./-]*/)?(?:ba|z|k)?sh["']""",
            r"#!\s*/(?:usr/)?bin/(?:env\s+)?(?:ba|z|k)?sh",
            r"""["'][\w./${}-]+\.(?:sh|bash)["']""",
        )
        if any(re.search(p, code) for p in shell_patterns):
            notes.append(
                "This file writes or invokes a shell script. It is not needed "
                "and not allowed: @mpi spans nodes on its own, rank placement "
                "goes in flags=, the environment is pre-exported by "
                "setup_hpc.sh, and filtering belongs in a downstream @task. The "
                "deliverables are the two generated XML files and this one "
                "Python file."
            )

        if "@multinode" in code:
            notes.append(
                "@multinode accepts only computing_nodes and "
                "processes_per_node -- it has no binary=, so it cannot launch a "
                "program without shelling out. For a multi-node MPI binary use "
                "@mpi(binary=..., runner=..., processes=N, processes_per_node=M)."
            )

        if "os.environ" in code and "COMPSS_MPIRUN_TYPE" in code:
            notes.append(
                "COMPSS_MPIRUN_TYPE is set inside the file. The JVM copied its "
                "environment before this module was imported, so a setdefault "
                "here does not reach the runtime or the mpirun it spawns. It "
                "belongs in setup_hpc.sh / launch_env()."
            )

        return notes

    def launch_env(self) -> dict:
        """Inherited environment, plus the MPI dialect COMPSs builds args for.

        Set here and not in the generated file: by the time the application
        module is imported the JVM already has its copy of the environment, so
        an os.environ assignment in the workflow arrives too late to affect the
        mpirun COMPSs spawns.
        """
        env = super().launch_env()
        env.setdefault("COMPSS_MPIRUN_TYPE", os.environ.get("COMPSS_MPIRUN_TYPE", "ompi"))
        return env

    def resource_info(self) -> dict:
        """Report the launcher, the XML state and the topology COMPSs will see.

        total_slots is the number the agent needs before choosing
        @mpi(processes=N); reporting it here means that choice is informed on
        the first call rather than after a failed run.
        """
        plan = self._topology or self.plan_topology()
        return {
            "launcher": f"{self.python} -m pycompss",
            "compss_install_dir": self.compss_install_dir,
            "compss_xml": {
                kind: {
                    "path": path,
                    "exists": os.path.isfile(path),
                    "written_by_agent": self.xml_overridden[kind],
                }
                for kind, path in self.xml_paths.items()
            },
            "compss_topology": plan,
            "note": (
                "The topology XML is generated from PBS_NODEFILE plus an SSH "
                "probe before every run -- do not write it by hand. "
                f"compss_topology.total_slots ({plan.get('total_slots')}) is the "
                "ceiling for @mpi(processes=N); a larger N fails with 'not "
                "enough slots available', and oversubscribing to get around that "
                "wastes the allocation. Confirm the bindings with "
                "check_package('pycompss'); install_package will not fix a "
                "missing runtime mid-run."
            ),
        }

    # __ Engine-only tool ______________________________________________________

    def register_tools(self) -> None:
        """Add write_xml, which exists for this engine alone."""
        self.mcp.tool(self.write_xml)

    def write_xml(self, kind: str, content: str) -> str:
        """Override COMPSs's project.xml or resources.xml.

        YOU NORMALLY DO NOT NEED THIS. The server generates both files from the
        real allocation before every run, including the SSH check that decides
        whether remote workers are usable. Call get_resources() and read
        `compss_topology` instead of writing these by hand.

        Use it only to correct a generated topology that is demonstrably wrong,
        and say why in your summary. A kind you write here is kept for the rest
        of the session and the generator stops touching it.

        Args:
            kind: "project" or "resources"
            content: the full XML document, starting with its declaration

        Returns:
            JSON with status, the path written, and the topology it replaced
        """
        if kind not in self.XML_KINDS:
            return self._json({
                "status": "rejected",
                "error": f"kind must be one of {list(self.XML_KINDS)}, got '{kind}'",
            })

        path = self.xml_paths[kind]
        os.makedirs(os.path.dirname(path) or self.work_dir, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        self.xml_overridden[kind] = True

        return self._json({
            "status": "written",
            "engine": self.ENGINE,
            "kind": kind,
            "path": path,
            "bytes": len(content.encode()),
            "overrides_generated_topology": True,
            "generated_plan_for_reference": self._topology or self.plan_topology(),
            "next": (
                "This file is now yours for the session; the server will not "
                "regenerate it. Re-check that the slots you declared match the "
                "allocation, then write the workflow file."
            ),
        })

    # __ Engine specifics ______________________________________________________

    def launch_command(self, workflow_path: str, args: list[str], ranks: int) -> list[str]:
        """`<python> -m pycompss [--project=] [--resources=] workflow.py [args...]`.

        `-m pycompss` is what makes this a COMPSs run. The same file launched
        as plain `<python> workflow.py` runs top to bottom with every decorator
        ignored, so dropping it turns a parallel workflow into a sequential one
        that still exits 0.

        No `--python_interpreter`: the module form infers it from the python it
        was invoked with, which is MAW_PYTHON -- necessarily the same
        interpreter check_package and install_package report on.

        The XML flags are added only for files that exist. prepare_run has
        normally just written both; a run with MAW_COMPSS_AUTO_XML=0 and no
        hand-written files still starts, on COMPSs's own single-host defaults.
        """
        command = [self.python, "-m", "pycompss"]
        for kind, path in self.xml_paths.items():
            if os.path.isfile(path):
                command.append(f"--{kind}={path}")
        # Skipped when the operator already set it either way in MAW_COMPSS_FLAGS,
        # so an explicit choice is never silently duplicated or overridden.
        if not any("python_propagate_virtual_environment" in f for f in self.extra_flags):
            command.append(self.PROPAGATE_VENV_FLAG)
        command += self.extra_flags
        return [*command, workflow_path, *args]

    #: COMPSs writes its per-task job logs under ~/.COMPSs/<app>_NN/jobs/, which
    #: is outside work_dir and so invisible to the inherited collect_logs. That
    #: is where the real failure lives: the master can exit 0 while a task has
    #: failed, so a run that reports "completed" with no deliverable is
    #: diagnosed here or not at all.
    COMPSS_HOME_LOGS = "~/.COMPSs"

    def collect_logs(self, max_files: int = 4, tail: int = 1500) -> dict:
        """Inherited work-directory logs, plus the newest COMPSs job logs."""
        import glob

        logs = super().collect_logs(max_files=max_files, tail=tail)

        base = os.path.expanduser(self.COMPSS_HOME_LOGS)
        paths = [
            p for p in glob.glob(os.path.join(base, "*", "jobs", "*"))
            if os.path.isfile(p) and os.path.getsize(p) > 0
        ]
        paths.sort(key=os.path.getmtime, reverse=True)
        for p in paths[:max_files]:
            try:
                with open(p, errors="replace") as f:
                    logs[os.path.join("~/.COMPSs", os.path.relpath(p, base))] = \
                        f.read()[-tail:]
            except OSError:
                continue
        return logs


if __name__ == "__main__":
    PyCompssServer.main()