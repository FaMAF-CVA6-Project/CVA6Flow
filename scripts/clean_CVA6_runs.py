#!/usr/bin/env python3
"""Remove what the CVA6 Verilator run scripts generate: the dated
verif/sim/out_<date>/ folders, work-ver/ and the results/ subfolders named
below, at each search root, plus every __pycache__ under a search root.
work-ver is asked about on its own, since a run that leaves the
configuration alone can reuse it. Launch it from the CVA6 root:

  python3 scripts/clean_CVA6_runs.py              # list, then ask
  python3 scripts/clean_CVA6_runs.py -y           # delete without asking
  python3 scripts/clean_CVA6_runs.py --dry-run    # list only
  python3 scripts/clean_CVA6_runs.py --keep-build # spare work-ver, no ask
  python3 scripts/clean_CVA6_runs.py my_results   # plus a folder named by hand
"""
import argparse
import glob
import os
import shutil
import sys

# Every folder a run writes at the top of each search root. work-ver is asked
# about separately.
ROOT_DIRS = {
    "work-ver":                "the Verilator build, remade by the next run",
    "results/run":             "run_CVA6.py: the files worth keeping",
    "results/batch":           "run_all_CVA6_benchmarks.py",
    "results/sweep_CVA6Flow":  "run_CVA6Flow_sweep.py",
    "results/sweep_CVA6_config": "run_CVA6_config_sweep.py",
    "results/verif":           "run_CVA6.py: simulation output that survived",
}

# Date-stamped simulation output: logs, disassembly, binaries and VCDs.
# Matched only at this path under a search root, so an unrelated out_* folder
# elsewhere is left alone.
OUT_GLOB = "verif/sim/out_*"
OUT_REASON = "run_CVA6.py: simulation output, logs and binaries"

# Deleted wherever they appear under a search root. A container collects
# these under every folder it runs a script from, not only beside the runners.
ANY_DEPTH_DIRS = {
    "__pycache__": "left behind by Python",
}

# Never descended into: heavy trees that cannot hold a generated folder, and
# work-ver, which the top of each root already takes or spares whole.
PRUNE_DIRS = {".git", "build", "vendor", "node_modules", "install", "work-ver"}


# SHARED BEGIN py-repo-root

# Needs: os


def repo_root():
    """The nearest folder above this script holding a .git, so a moved tree
    needs no parent count fixed. Without one, as in a release archive, the
    parent of the script's folder, which is the documented layout."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = here
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return os.path.dirname(here)
        path = parent

# SHARED END py-repo-root


REPO_ROOT = repo_root()


def cva6_checkout():
    """The CVA6 root run_CVA6.py runs in: /CVA6 in the image, else the
    nearest folder above this script holding verif/sim, found the way
    run_CVA6.py finds it, else this repository."""
    if os.path.isdir("/CVA6"):
        return "/CVA6"
    path = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.isdir(os.path.join(path, "verif", "sim")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return REPO_ROOT
        path = parent


CVA6_ROOT = cva6_checkout()


def search_roots():
    """The CVA6 root, this repository and the working directory. The
    simulation and results/ land under the CVA6 root, and a batch collects
    into the directory it was launched from."""
    roots = []
    seen = set()
    for root in (CVA6_ROOT, REPO_ROOT, os.getcwd()):
        real = os.path.realpath(root)
        # Refuse to walk from a place where a stray match would be a disaster.
        if real in ("/", os.path.expanduser("~")):
            print(f"[WARN] Skipping the search root {real}: too broad. "
                  f"Run this from the CVA6 root instead.")
            continue
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            roots.append(root)
    return roots


def find_targets(roots, keep_build, extra=()):
    """Collect every generated folder under the roots, plus any named by
    hand. A match is never descended into. It is about to be deleted whole,
    so its contents cannot add anything."""
    found = []
    seen = set()

    def add(path, reason):
        """Take a folder once, and say whether path is a folder at all."""
        if not os.path.isdir(path):
            return False
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            found.append((path, reason))
        return True

    for path in extra:
        if not add(path, "named on the command line"):
            print(f"[WARN] Not a folder, ignored: {path}")

    for root in roots:
        for name, reason in ROOT_DIRS.items():
            if name == "work-ver" and keep_build:
                continue
            add(os.path.join(root, name), reason)

        for path in glob.glob(os.path.join(root, OUT_GLOB)):
            add(path, OUT_REASON)

        for dirpath, dirnames, _ in os.walk(root):
            keep = []
            for name in dirnames:
                full = os.path.join(dirpath, name)
                if os.path.realpath(full) in seen:
                    continue
                if name in ANY_DEPTH_DIRS:
                    add(full, ANY_DEPTH_DIRS[name])
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


# SHARED BEGIN py-human-size

# Needs: none


def human(size):
    """A size in bytes as whole B, or as KiB, MiB or GiB with one decimal."""
    if size < 1024:
        return f"{size:.0f} B"
    for unit in ("KiB", "MiB", "GiB"):
        size /= 1024
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"

# SHARED END py-human-size


def ask_about_build(targets):
    """work-ver takes about twelve minutes to remake, and a run that leaves
    the configuration alone reuses it, so it is asked about on its own. None
    when the question is cancelled."""
    build = [t for t in targets if os.path.basename(t[0]) == "work-ver"]
    if not build:
        return targets
    size = human(sum(folder_size(path) for path, _ in build))
    try:
        reply = input(f"Delete work-ver as well ({size}), so the next run "
                      f"recompiles the model? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n[INFO] Cancelled")
        return None
    if reply in ("y", "yes"):
        return targets
    print("[INFO] Keeping work-ver, so run with run_CVA6.py --keep-build. "
          "Pass --keep-build here to skip the question.")
    return [t for t in targets if t not in build]


def main():
    parser = argparse.ArgumentParser(
        description="Delete the folders the CVA6 Verilator run scripts "
                    "generate.")
    parser.add_argument("extra", nargs="*",
                        help="Extra folders to delete, for the output of a "
                             "batch or sweep given --out-dir")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Delete without asking for confirmation")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be deleted and stop")
    parser.add_argument("--keep-build", action="store_true",
                        help="Spare work-ver/, so the next run can reuse it "
                             "with run_CVA6.py --keep-build instead of "
                             "recompiling the model, and do not ask about it")
    args = parser.parse_args()

    roots = search_roots()
    if not roots:
        print("[ERROR] No usable search root", file=sys.stderr)
        return 1

    print("[INFO] Searching in: " + ", ".join(os.path.abspath(r)
                                              for r in roots))
    targets = find_targets(roots, args.keep_build, args.extra)

    if not targets:
        print("[INFO] Nothing to clean")
        return 0

    if not args.keep_build and not args.yes and not args.dry_run:
        targets = ask_about_build(targets)
        if targets is None:
            return 0

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
        return 0

    if not args.yes:
        try:
            reply = input("Delete these? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[INFO] Cancelled")
            return 0
        if reply not in ("y", "yes"):
            print("[INFO] Cancelled")
            return 0

    deleted = 0
    for path, _ in targets:
        try:
            shutil.rmtree(path)
            deleted += 1
        except OSError as e:
            print(f"[ERROR] Could not delete {path}: {e}", file=sys.stderr)

    print(f"[INFO] Deleted {deleted} of {len(targets)} folder(s), "
          f"{human(total)} freed")
    return 0 if deleted == len(targets) else 1


if __name__ == "__main__":
    sys.exit(main())
