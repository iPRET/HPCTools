#!/usr/bin/env python3
"""One-shot snapshot of cluster utilization: node states and top users per group.

Usage:
    clusterstatus.py                 # group by partition (default)
    clusterstatus.py --by features   # group by node Features instead
"""

import subprocess
import sys
from collections import defaultdict


def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.stdout.strip(), r.returncode


STATE_LABELS = {
    "idle":   "idle",
    "alloc":  "allocated",
    "mix":    "mixed (partial)",
    "comp":   "completing",
    "drain":  "drained",
    "drng":   "draining",
    "drai":   "drained",
    "down":   "down",
    "maint":  "maintenance",
    "resv":   "reserved",
    "boot":   "booting",
    "fail":   "failed",
    "failg":  "failing",
    "unk":    "unknown",
    "plnd":   "planned",
    "pow_dn": "powered down",
    "pow_up": "powering up",
}

STATE_EXPLANATIONS = {
    "idle":   "Node is up and has no jobs running on it — fully available for new work.",
    "alloc":  "Node is fully allocated to one or more running jobs (no free CPUs left for the partition).",
    "mix":    "Node is partially allocated — some CPUs/resources are in use, others are still free.",
    "comp":   "Job(s) on the node are in the process of finishing up (epilog/cleanup).",
    "drain":  "Admin marked the node as drained: existing jobs may finish, but no new jobs will be scheduled on it.",
    "drng":   "Same as drained, but a job is still running — the node will become 'drain' once it finishes.",
    "drai":   "Drained (alternate sinfo abbreviation).",
    "down":   "Node is unreachable or considered failed by the controller — not usable.",
    "maint":  "Node is held by a maintenance reservation; will return to service when the reservation ends.",
    "resv":   "Node is held by a (non-maintenance) reservation for specific users/jobs.",
    "boot":   "Node is currently booting and will be available shortly.",
    "fail":   "Node has been flagged as failing and will be removed from service.",
    "failg":  "Node is in the process of failing — running jobs may be affected.",
    "unk":    "Controller has not heard from the node recently; state is unknown.",
    "plnd":   "Reserved by the scheduler for a planned (future) job — not free for new submissions.",
    "pow_dn": "Node has been powered down to save energy; it will be powered back on when needed.",
    "pow_up": "Node is powering back on after being suspended.",
}


def clean_state(s):
    # sinfo appends flags like *, ~, #, $, @, %, !, +, &, ^, - to state codes.
    return s.rstrip("*~#$@%!+&^-")


def get_nodes_by_group(group_by):
    """Return ({group: {state: count}}, total_unique_nodes)."""
    out, rc = run('sinfo -h -N -o "%N|%t|%P|%f"')
    if rc != 0 or not out:
        return {}, 0

    groups = defaultdict(lambda: defaultdict(set))
    all_nodes = set()
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 4:
            continue
        node, state, partition, feats = parts
        state = clean_state(state)
        # sinfo marks the default partition with a trailing '*' (e.g. "booster*"),
        # but squeue reports the same partition without it. Normalize so the
        # top-users join below actually matches.
        partition = partition.rstrip("*")
        all_nodes.add(node)
        key = partition if group_by == "partition" else (feats or "(none)")
        groups[key][state].add(node)

    return (
        {g: {s: len(ns) for s, ns in states.items()} for g, states in groups.items()},
        len(all_nodes),
    )


def get_top_users_by_partition(top_n=5):
    """Return {partition: [(user, nodes, pct_of_partition_running_nodes)]}."""
    out, rc = run('squeue -h -t R -o "%u|%D|%P"')
    if rc != 0 or not out:
        return {}

    by_part = defaultdict(lambda: defaultdict(int))
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        user, nodes, partition = parts
        try:
            n = int(nodes)
        except ValueError:
            continue
        by_part[partition][user] += n

    result = {}
    for partition, users in by_part.items():
        total = sum(users.values())
        ranked = sorted(users.items(), key=lambda x: x[1], reverse=True)[:top_n]
        result[partition] = [
            (u, n, (100 * n / total) if total else 0) for u, n in ranked
        ]
    return result


def main():
    group_by = "partition"
    if "--by" in sys.argv:
        i = sys.argv.index("--by")
        if i + 1 < len(sys.argv):
            group_by = sys.argv[i + 1]
    if group_by not in ("partition", "features"):
        print("--by must be 'partition' or 'features'")
        sys.exit(1)

    groups, total_unique = get_nodes_by_group(group_by)
    if not groups:
        print("No node data returned from sinfo. Is SLURM available?")
        sys.exit(1)

    # Top users only meaningful when grouping by partition (squeue gives partition, not features).
    top_users = get_top_users_by_partition() if group_by == "partition" else {}

    print(f"=== Cluster snapshot ===  unique nodes: {total_unique}  grouped by: {group_by}\n")

    seen_states = set()
    for group in sorted(groups):
        states = groups[group]
        seen_states.update(states.keys())
        total = sum(states.values())
        print(f"[{group}]  {total} nodes")
        for state in sorted(states, key=lambda s: -states[s]):
            label = STATE_LABELS.get(state, state)
            n = states[state]
            pct = 100 * n / total if total else 0
            print(f"  {n:>5}  {label:<18} {pct:5.1f}%")

        users = top_users.get(group, [])
        if users:
            print("  top users (running nodes):")
            for u, n, pct in users:
                print(f"    {n:>4}  {pct:5.1f}%  {u}")
        print()

    explained = [s for s in seen_states if s in STATE_EXPLANATIONS]
    if explained:
        print("State meanings:")
        for state in sorted(explained, key=lambda s: STATE_LABELS.get(s, s)):
            label = STATE_LABELS.get(state, state)
            print(f"  - {label}: {STATE_EXPLANATIONS[state]}")


if __name__ == "__main__":
    main()
