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

    for group in sorted(groups):
        states = groups[group]
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


if __name__ == "__main__":
    main()
