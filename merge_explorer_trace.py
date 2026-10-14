"""
Merge a multi-node run's explorer side-car trace into its trace.json.

Why this is needed
------------------
`trace_logger.tracer` is a module-level singleton, so it is per-process. Under
`--env local` and `--env hpc` every agent runs in the launching process and
shares one tracer, so the saved trace.json is already complete.

Under `--env multi-node` the Explorer is a Parsl worker on another node. Its
tracer is a different object, so its agent_start/agent_end, tool_call and
llm_call events never reach the trace the entrypoint saves -- which is why the
summary of such a run reports `Tool calls: 0`, omits `explorer` from
`agents_involved`, and shows no explorer row in the token breakdown, while
still listing `-> explorer ->` in the routing path (the orchestrator logged
that one locally).

The Explorer writes its own events to `<run>_explorer_trace.jsonl` on the
shared filesystem precisely so they are not lost. This script folds that file
into trace.json and recomputes the summary.

Ordering note: `elapsed_s` is measured from each process's own start, so the
Explorer's values are not comparable to the launching process's and sorting by
them would interleave the two traces wrongly. Events are ordered by their
absolute `timestamp` instead, and each merged event keeps its original
`elapsed_s` with a `source` marker added so it is clear which clock it came
from.

Usage:
    python merge_explorer_trace.py runs/20261014_154823_trace.json
    python merge_explorer_trace.py runs/20261014_154823_trace.json --jsonl other.jsonl
    python merge_explorer_trace.py runs/*_trace.json          # batch
"""

import argparse
import glob
import json
import os
import sys


def _sidecar_for(trace_path: str) -> str:
    """runs/<id>_trace.json -> runs/<id>_explorer_trace.jsonl"""
    base = trace_path[: -len("_trace.json")] if trace_path.endswith("_trace.json") \
        else os.path.splitext(trace_path)[0]
    return base + "_explorer_trace.jsonl"


def _load_jsonl(path: str) -> list:
    """Read events, skipping malformed lines.

    A truncated final line is the normal result of a job hitting its walltime
    mid-write, and is not a reason to refuse the rest of the file.
    """
    events, bad = [], 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    if bad:
        print(f"  note: skipped {bad} malformed line(s)", file=sys.stderr)
    return events


def _fingerprint(event: dict) -> str:
    """Stable identity for one event, so re-running this is idempotent."""
    return json.dumps(event, sort_keys=True, default=str)


def _summarize(events: list) -> dict:
    """Recompute the summary over the merged events.

    Mirrors TraceLogger.get_summary so the merged file stays consistent with
    what a single-process run would have produced. total_duration_s is taken
    from the largest local elapsed_s rather than recomputed, since the two
    processes do not share a clock.
    """
    agents, tool_calls, successes, routings = set(), 0, 0, []
    in_tok = out_tok = 0
    by_agent: dict = {}
    local_max = 0.0

    for e in events:
        etype = e.get("type")
        if e.get("source") != "explorer_sidecar":
            local_max = max(local_max, float(e.get("elapsed_s") or 0))
        if etype in ("agent_start", "agent_end"):
            agents.add(e["agent"])
        elif etype == "tool_call":
            tool_calls += 1
            if e.get("succeeded"):
                successes += 1
        elif etype == "routing":
            routings.append(f"{e['from']} -> {e['to']}")
        elif etype == "llm_call":
            in_tok += e.get("input_tokens", 0) or 0
            out_tok += e.get("output_tokens", 0) or 0
            by_agent[e["agent"]] = by_agent.get(e["agent"], 0) + (e.get("total_tokens", 0) or 0)

    return {
        "total_events": len(events),
        "total_duration_s": round(local_max, 2),
        "agents_involved": sorted(agents),
        "tool_calls": tool_calls,
        "tool_successes": successes,
        "routing_path": routings,
        "total_input_tokens": in_tok,
        "total_output_tokens": out_tok,
        "total_tokens": in_tok + out_tok,
        "tokens_by_agent": by_agent,
    }


def merge_into_tracer(tracer, trace_path: str, jsonl_path: str = "") -> int:
    """Fold the side-car into a live TraceLogger, before it saves.

    This is what `academy_agents.main()` calls. Merging into the tracer rather
    than post-processing the saved file means `tracer.save()` validates the
    merged events and recomputes the summary itself, and `make_summary()` --
    which reads `tracer.get_summary()` in memory, not the file -- prints the
    explorer's tool calls and tokens too. Patching the file afterwards would
    fix trace.json and still print a summary claiming `Tool calls: 0`.

    Returns the number of events added. Never raises: a run that finished must
    not fail at the last step over its own bookkeeping.
    """
    try:
        jsonl_path = jsonl_path or _sidecar_for(trace_path)
        if not os.path.isfile(jsonl_path):
            return 0   # local/hpc runs have no side-car, and need none

        seen = {_fingerprint(e) for e in tracer.events}
        added = 0
        for event in _load_jsonl(jsonl_path):
            event.setdefault("source", "explorer_sidecar")
            fingerprint = _fingerprint(event)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            tracer.events.append(event)
            added += 1

        # Absolute timestamps, not elapsed_s: the two processes measure elapsed_s
        # from their own starts, so sorting by it would interleave them wrongly.
        tracer.events.sort(key=lambda e: e.get("timestamp") or "")
        return added
    except Exception as e:  # noqa: BLE001 - bookkeeping must not end a good run
        print(f"[merge_explorer_trace] WARNING: could not merge side-car: {e}",
              file=sys.stderr)
        return 0


def merge(trace_path: str, jsonl_path: str = "", write: bool = True) -> dict:
    with open(trace_path) as f:
        payload = json.load(f)

    jsonl_path = jsonl_path or _sidecar_for(trace_path)
    if not os.path.isfile(jsonl_path):
        print(f"{trace_path}: no side-car at {jsonl_path} -- nothing to merge "
              f"(a local/hpc run has none, and needs none)")
        return payload

    existing = payload.get("trace", [])
    seen = {_fingerprint(e) for e in existing}

    added = []
    for e in _load_jsonl(jsonl_path):
        e.setdefault("source", "explorer_sidecar")
        if _fingerprint(e) in seen:
            continue
        seen.add(_fingerprint(e))
        added.append(e)

    merged = existing + added
    # Absolute timestamps, not elapsed_s -- see the module docstring.
    merged.sort(key=lambda e: e.get("timestamp") or "")

    payload["trace"] = merged
    payload["summary"] = _summarize(merged)

    if write:
        with open(trace_path, "w") as f:
            json.dump(payload, f, indent=2, default=str)

    s = payload["summary"]
    print(f"{trace_path}: +{len(added)} event(s) from side-car "
          f"({len(existing)} -> {len(merged)})")
    print(f"  agents:     {', '.join(s['agents_involved'])}")
    print(f"  tool calls: {s['tool_calls']} ({s['tool_successes']} succeeded)")
    print(f"  tokens:     {s['total_tokens']:,} total")
    for agent, n in sorted(s["tokens_by_agent"].items()):
        print(f"      {agent}: {n:,}")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("trace", nargs="+", help="path(s) to <run>_trace.json")
    ap.add_argument("--jsonl", default="",
                    help="explicit side-car path (only valid with a single trace)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be merged without writing")
    args = ap.parse_args()

    paths = [p for pat in args.trace for p in (glob.glob(pat) or [pat])]
    if args.jsonl and len(paths) > 1:
        ap.error("--jsonl only makes sense with a single trace file")

    for p in paths:
        if not os.path.isfile(p):
            print(f"{p}: not found", file=sys.stderr)
            continue
        merge(p, args.jsonl, write=not args.dry_run)


if __name__ == "__main__":
    main()
