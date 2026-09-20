#!/usr/bin/env python3
"""Remove the heavy run artefacts from this viewer repository.

Deletes every .list, .vcd and .fst file, and every __pycache__ folder,
anywhere under the repository this script sits in. No .json or .js is
touched, wherever it is, and docs/ is kept whole.

  python3 scripts/clean_CVA6Flow_repo.py             # list, then ask
  python3 scripts/clean_CVA6Flow_repo.py -y          # delete without asking
  python3 scripts/clean_CVA6Flow_repo.py --dry-run   # list only
"""
import argparse
import os
import shutil
import sys

# Files removed, matched on the end of the name. A CVA6 run writes no gem5
# debug trace, so none is looked for.
FILE_SUFFIXES = (".list", ".vcd", ".fst")

# Folders removed whole.
DIR_NAMES = {"__pycache__"}

# Kept, whatever is in them, relative to the repository root.
KEEP_DIRS = ("docs",)


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


def kind_of(path):
    """The label a target is grouped under in the listing."""
    name = os.path.basename(path)
    if name in DIR_NAMES:
        return name
    for suffix in FILE_SUFFIXES:
        if name.endswith(suffix):
            return f"*{suffix}"
    return "?"


def is_kept(path):
    """True for anything under a folder the script must not touch."""
    rel = os.path.relpath(path, REPO_ROOT)
    return any(rel == keep or rel.startswith(keep + os.sep)
               for keep in KEEP_DIRS)


def find_targets():
    """Every artefact in the repository, as a list of paths. A __pycache__ is
    taken whole and not descended into, since it is about to be deleted and
    nothing inside it can add a separate target."""
    targets = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        keep = []
        for dirname in dirnames:
            full = os.path.join(dirpath, dirname)
            if dirname in DIR_NAMES and not is_kept(full):
                targets.append(full)
            elif dirname != ".git":
                keep.append(dirname)
        dirnames[:] = keep

        for filename in filenames:
            full = os.path.join(dirpath, filename)
            if filename.endswith(FILE_SUFFIXES) and not is_kept(full):
                targets.append(full)
    return sorted(targets)


def size_of(path):
    """Bytes held by a file or a folder. A race or a broken link counts
    zero."""
    if os.path.isfile(path) or os.path.islink(path):
        try:
            return os.lstat(path).st_size
        except OSError:
            return 0

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


def group(targets):
    """Collapse the targets into (folder, kind) rows, so a sweep that produced
    hundreds of files reads as a few lines rather than a wall of paths."""
    rows = {}
    for path in targets:
        folder = os.path.relpath(os.path.dirname(path), REPO_ROOT)
        key = (folder if folder != "." else "", kind_of(path))
        count, size = rows.get(key, (0, 0))
        rows[key] = (count + 1, size + size_of(path))
    return sorted(rows.items())


def main():
    parser = argparse.ArgumentParser(
        description="Delete the .list, .vcd and .fst files and "
                    "__pycache__ folders in this viewer repository.")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Delete without asking for confirmation")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be deleted and stop")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="List every path instead of one row per folder")
    args = parser.parse_args()

    print(f"[INFO] Searching in: {REPO_ROOT}")
    kept = [k for k in KEEP_DIRS if os.path.isdir(os.path.join(REPO_ROOT, k))]
    if kept:
        print("[INFO] Keeping: " + ", ".join(f"{k}/" for k in kept))

    targets = find_targets()
    if not targets:
        print("[INFO] Nothing to clean")
        return 0

    print("\n" + "=" * 70)
    print("TO DELETE")
    print("=" * 70)

    total = 0
    if args.verbose:
        for path in targets:
            size = size_of(path)
            total += size
            print(f"{human(size):>10}  {os.path.relpath(path, REPO_ROOT)}")
    else:
        for (folder, kind), (count, size) in group(targets):
            total += size
            print(f"{human(size):>10}  {folder + '/' if folder else './'}  "
                  f"{count} {kind}")
    print("=" * 70)
    print(f"{len(targets)} item(s), {human(total)}\n")

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
    for path in targets:
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            deleted += 1
        except OSError as e:
            print(f"[ERROR] Could not delete {path}: {e}", file=sys.stderr)

    print(f"[INFO] Deleted {deleted} of {len(targets)} item(s), "
          f"{human(total)} freed")
    return 0 if deleted == len(targets) else 1


if __name__ == "__main__":
    sys.exit(main())
