#!/usr/bin/env python3
"""Inspect SLURM jobs across all your associated projects. Refreshes every 20 seconds."""

import subprocess
import os
import sys
import time
import shutil


def run(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def get_username():
    out, _, rc = run("whoami")
    if rc == 0 and out:
        return out
    return os.environ.get("USER") or os.environ.get("USERNAME", "unknown")


def get_associated_accounts(username):
    """Get all SLURM accounts/projects the user is associated with."""
    out, _, rc = run(f"sacctmgr -n -P show associations user={username} format=Account")
    if rc != 0 or not out:
        # Fallback: try to parse from squeue or environment
        out2, _, rc2 = run("sacctmgr -n -P show associations format=Account,User")
        if rc2 == 0 and out2:
            accounts = set()
            for line in out2.splitlines():
                parts = line.split("|")
                if len(parts) >= 2 and parts[1] == username:
                    accounts.add(parts[0])
            return sorted(accounts)
        return []
    return sorted(set(line.strip() for line in out.splitlines() if line.strip()))


def get_jobs_for_accounts(accounts):
    """Query squeue for all jobs in the given accounts."""
    if not accounts:
        return []

    account_str = ",".join(accounts)
    fmt = "JobID:|Account:|User:|JobName:|State:|Partition:|NumNodes:|NumCPUs:|Gres:|TimeUsed:|TimeLimit:|Reason:"
    out, _, rc = run(
        f'squeue -A {account_str} --format="%i|%a|%u|%j|%T|%P|%D|%C|%b|%M|%l|%R" --noheader'
    )
    if rc != 0 or not out:
        return []

    jobs = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) >= 12:
            jobs.append({
                "id": parts[0].strip(),
                "account": parts[1].strip(),
                "user": parts[2].strip(),
                "name": parts[3].strip(),
                "state": parts[4].strip(),
                "partition": parts[5].strip(),
                "nodes": parts[6].strip(),
                "cpus": parts[7].strip(),
                "gres": parts[8].strip(),
                "time_used": parts[9].strip(),
                "time_limit": parts[10].strip(),
                "reason": parts[11].strip(),
            })
    return jobs


REASON_EXPLANATIONS = {
    "Resources": "Waiting for required resources (CPUs/memory/GPUs) to free up on a matching node.",
    "Priority": "Other queued jobs have higher priority and will run first.",
    "ReqNodeNotAvail": "The specific node(s) the job needs are unavailable — often because they're drained, down, or held by a reservation. The parenthesized note (e.g. 'Reserved for maintenance') usually says why.",
    "Reserved for maintenance": "Some nodes are blocked by an admin reservation for maintenance; the job will run after the reservation ends.",
    "ReqNodeNotAvail, UnavailableNodes": "Requested nodes are currently unreachable (down or drained).",
    "Dependency": "Waiting for another job listed in --dependency to finish.",
    "DependencyNeverSatisfied": "A dependency job failed or was cancelled — this job will never run and should be cancelled manually.",
    "JobHeldUser": "You (the user) put this job on hold with `scontrol hold`. Release with `scontrol release <jobid>`.",
    "JobHeldAdmin": "An administrator put this job on hold.",
    "BeginTime": "Job has a --begin time in the future and is waiting for that moment.",
    "QOSMaxJobsPerUserLimit": "You've hit the per-user job count limit for this QOS; one of your jobs must finish first.",
    "QOSMaxCpuPerUserLimit": "You've hit the per-user CPU limit for this QOS.",
    "AssocMaxJobsLimit": "You've hit the maximum number of running jobs allowed for your account/association.",
    "PartitionDown": "The target partition is currently down.",
    "PartitionNodeLimit": "The job requests more nodes than the partition allows.",
    "PartitionTimeLimit": "The job's time limit exceeds what the partition allows.",
    "NodeDown": "An assigned node went down.",
    "Licenses": "Waiting for a software license tracked by SLURM.",
    "ReqNodeNotAvail, Reserved for maintenance": "Requested nodes are blocked by a maintenance reservation; the job will start after maintenance ends.",
}


def extract_reason_codes(jobs):
    """Pick out known SLURM reason codes that appear in the current job list.

    For each job we keep only the most specific (longest) matching key, so a
    compound reason like "ReqNodeNotAvail, Reserved for maintenance" doesn't
    also drag in its substrings.
    """
    found = []
    seen = set()
    keys_by_specificity = sorted(REASON_EXPLANATIONS, key=len, reverse=True)
    for job in jobs:
        raw = job["reason"].strip().strip("()")
        if not raw:
            continue
        for code in keys_by_specificity:
            if code in raw:
                if code not in seen:
                    seen.add(code)
                    found.append((code, REASON_EXPLANATIONS[code]))
                break
    return found


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def format_table(jobs):
    if not jobs:
        return "  No jobs found."

    columns = [
        ("JobID", "id"),
        ("Account", "account"),
        ("User", "user"),
        ("Name", "name"),
        ("State", "state"),
        ("Partition", "partition"),
        ("Nodes", "nodes"),
        ("CPUs", "cpus"),
        ("GPUs/Gres", "gres"),
        ("Used", "time_used"),
        ("Limit", "time_limit"),
        ("Reason/Nodelist", "reason"),
    ]

    term_width = shutil.get_terminal_size((120, 40)).columns

    # Calculate column widths
    widths = {}
    for header, key in columns:
        widths[key] = max(len(header), *(len(j[key]) for j in jobs))

    # Truncate reason column if table is too wide
    total = sum(widths[k] for _, k in columns) + 3 * (len(columns) - 1)
    if total > term_width:
        excess = total - term_width
        widths["reason"] = max(8, widths["reason"] - excess)

    header_line = " | ".join(h.ljust(widths[k]) for h, k in columns)
    sep_line = "-+-".join("-" * widths[k] for _, k in columns)

    lines = [header_line, sep_line]
    for job in jobs:
        row = " | ".join(job[k][:widths[k]].ljust(widths[k]) for _, k in columns)
        lines.append(row)

    return "\n".join(lines)


def main():
    refresh_interval = 20

    username = get_username()
    print(f"Detecting accounts for user: {username}")

    accounts = get_associated_accounts(username)
    if not accounts:
        print("Could not detect any SLURM accounts. Is sacctmgr available?")
        print("You can pass accounts manually: inspectjobs.py account1 account2 ...")
        if len(sys.argv) > 1:
            accounts = sys.argv[1:]
            print(f"Using manually provided accounts: {', '.join(accounts)}")
        else:
            sys.exit(1)

    print(f"Monitoring accounts: {', '.join(accounts)}")
    print(f"Refreshing every {refresh_interval}s. Press Ctrl+C to quit.\n")
    time.sleep(1)

    try:
        while True:
            jobs = get_jobs_for_accounts(accounts)

            clear_screen()
            print(f"=== SLURM Job Inspector === User: {username} | Accounts: {', '.join(accounts)}")
            print(f"    Last refresh: {time.strftime('%Y-%m-%d %H:%M:%S')} | Next in {refresh_interval}s")
            print()

            # Group by account
            by_account = {}
            for job in jobs:
                by_account.setdefault(job["account"], []).append(job)

            if not jobs:
                print("  No jobs found across any accounts.")
            else:
                print(f"  Total jobs: {len(jobs)}")
                running = sum(1 for j in jobs if j["state"] == "RUNNING")
                pending = sum(1 for j in jobs if j["state"] == "PENDING")
                if running or pending:
                    print(f"  Running: {running} | Pending: {pending}")
                print()
                for acct in sorted(by_account):
                    acct_jobs = by_account[acct]
                    print(f"  [{acct}] ({len(acct_jobs)} jobs)")
                    for line in format_table(acct_jobs).splitlines():
                        print(f"  {line}")
                    print()

                explanations = extract_reason_codes(jobs)
                if explanations:
                    print("  Reason codes:")
                    for code, text in explanations:
                        print(f"    - {code}: {text}")
                    print()

            time.sleep(refresh_interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
