#!/usr/bin/env python3
"""Remove everything the CVA6 Verilator run scripts generate: the dated
verif/sim/out_<date>/ folders, work-ver/ and all of results/. Only the fixed
names below are removed, results/ at each search root and __pycache__ where a
Verilator runner sits beside it. Launch it from the CVA6 root:

  python3 scripts/clean_CVA6_runs.py              # list, then ask
  python3 scripts/clean_CVA6_runs.py -y           # no confirmation
  python3 scripts/clean_CVA6_runs.py --dry-run    # list only
  python3 scripts/clean_CVA6_runs.py --keep-build # spare work-ver
  python3 scripts/clean_CVA6_runs.py my_results   # plus a custom --out-dir
"""
import os
import sys
import glob
import shutil
import argparse

# Every folder a run writes, under results/ at the top of each search root.
# One .dockerignore line covers the lot, and the pull menu still offers them
# one at a time. work-ver is the upstream Verilator tree and stays put.
ROOT_DIRS = {
    "work-ver":                "the Verilator build, remade by the next run",
    "results/run":             "run_CVA6.py: the files worth keeping",
    "results/batch":           "run_all_CVA6_benchmarks.py",
    "results/sweep_CVA6Flow":  "run_CVA6Flow_sweep.py",
    "results/verif":           "run_CVA6.py: simulation output that survived",
}

# Date-stamped simulation output: logs, disassembly, binaries and VCDs.
# Matched only at this path under a search root, so an unrelated out_* folder
# elsewhere is left alone.
OUT_GLOB = "verif/sim/out_*"
OUT_REASON = "run_CVA6.py: simulation output, logs and binaries"

# Folders that appear beside a runner script. Matched at any depth, but only
# when one of the Verilator runners sits in the same folder.
SIBLING_DIRS = {
    "__pycache__": "left behind by python",
}

RUNNERS = {"run_CVA6.py", "run_all_CVA6_benchmarks.py",
           "run_CVA6Flow_sweep.py"}

# Never descended into: heavy trees that cannot hold a generated folder.
PRUNE_DIRS = {".git", "build", "vendor", "node_modules", "install"}


def repo_root():
    """The repository this script sits in, found by walking up to the nearest
    .git. The script lives in scripts/, so counting parents would be one more
    thing to fix the next time the tree moves."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = here
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return here
        path = parent


REPO_ROOT = repo_root()

# Where run_CVA6.py expects the checkout. Outside the container that path
# does not exist and the repository this script lives in is used instead.
CVA6_ROOT = "/CVA6" if os.path.isdir("/CVA6") else REPO_ROOT


def search_roots():
    """The CVA6 root, this repository and the working directory. The
    simulation writes under the CVA6 root, results/ lands next to the
    runner, and a batch collects into the directory it was launched from."""
    roots = []
    seen = set()
    for root in (CVA6_ROOT, REPO_ROOT, os.getcwd()):
        real = os.path.realpath(root)
        # Refuse to walk from a place where a stray match would be a disaster.
        if real in ("/", os.path.expanduser("~")):
            print(f"[WARN] Skipping the search root {real}: too broad.")
            continue
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            roots.append(root)
    return roots


def find_targets(roots, keep_build, extra=()):
    """Collect every generated folder under the roots, plus any named by
    hand. A match is never descended into, it is about to be deleted whole so
    its contents cannot add anything."""
    found = []
    seen = set()

    def add(path, reason):
        real = os.path.realpath(path)
        if real not in seen and os.path.isdir(path):
            seen.add(real)
            found.append((path, reason))

    for path in extra:
        if os.path.isdir(path):
            add(path, "named on the command line")
        else:
            print(f"[WARN] Not a folder, ignored: {path}")

    for root in roots:
        for name, reason in ROOT_DIRS.items():
            if name == "work-ver" and keep_build:
                continue
            add(os.path.join(root, name), reason)

        for path in glob.glob(os.path.join(root, OUT_GLOB)):
            add(path, OUT_REASON)

        for dirpath, dirnames, filenames in os.walk(root):
            beside_runner = RUNNERS.intersection(filenames)
            keep = []
            for name in dirnames:
                full = os.path.join(dirpath, name)
                if os.path.realpath(full) in seen:
                    continue          # already taken, and taken whole
                if name == "work-ver":
                    continue          # spared, and nothing inside is a target
                if name in SIBLING_DIRS and beside_runner:
                    add(full, SIBLING_DIRS[name])
                elif name not in PRUNE_DIRS and not name.startswith("."):
                    keep.append(name)
            dirnames[:] = keep

    return sorted(found)


def folder_size(path):
    """Bytes held under path. Broken links and races are counted as zero."""
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def human(size):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024


def main():
    parser = argparse.ArgumentParser(
        description="Delete the folders the CVA6 Verilator run scripts "
                    "generate.")
    parser.add_argument("extra", nargs="*",
                        help="Extra folders to delete, for a batch or a sweep "
                             "run with a custom --out-dir")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Delete without asking for confirmation")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="List what would be deleted and stop")
    parser.add_argument("--keep-build", action="store_true",
                        help="Spare work-ver/, so the next run can reuse it "
                             "with run_CVA6.py --keep-build instead of "
                             "recompiling the model")
    args = parser.parse_args()

    roots = search_roots()
    if not roots:
        print("[ERROR] No usable search root")
        sys.exit(1)

    print("[INFO] Searching in: " + ", ".join(os.path.abspath(r)
                                              for r in roots))
    targets = find_targets(roots, args.keep_build, args.extra)

    if not targets:
        print("[INFO] Nothing to clean")
        return

    print("\n" + "=" * 70)
    print("TO DELETE")
    print("=" * 70)
    total = 0
    for path, reason in targets:
        size = folder_size(path)
        total += size
        print(f"{human(size):>10}  {os.path.abspath(path)}")
        print(f"{'':>10}  ({reason})")
    print("=" * 70)
    print(f"{len(targets)} folder(s), {human(total)}\n")

    if args.dry_run:
        print("[INFO] Dry run, nothing was deleted")
        return

    if not args.yes:
        try:
            reply = input("Delete these? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[INFO] Cancelled")
            return
        if reply not in ("y", "yes"):
            print("[INFO] Cancelled")
            return

    deleted = 0
    for path, _ in targets:
        try:
            shutil.rmtree(path)
            deleted += 1
        except OSError as e:
            print(f"[WARN] Could not delete {path}: {e}")

    print(f"[INFO] Deleted {deleted} of {len(targets)} folder(s), "
          f"{human(total)} freed")


if __name__ == "__main__":
    main()
