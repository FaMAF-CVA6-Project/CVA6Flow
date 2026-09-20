#!/usr/bin/env python3
"""Find the tracer JSONs too big to push, and add them to .gitignore.

A tracer JSON runs to hundreds of megabytes. GitHub warns above 50 MiB and
refuses above 100 MiB, and git has no size test of its own: .gitignore matches
a path, never a size.

Re-run it after a sweep. A file already tracked is reported, not ignored:
.gitignore has no effect on a file git is already carrying, so a committed
file stays visible.

    python3 scripts/ignore_big_CVA6Flow_jsons.py            # list, then ask
    python3 scripts/ignore_big_CVA6Flow_jsons.py -y         # without asking
    python3 scripts/ignore_big_CVA6Flow_jsons.py --dry-run  # list only
    python3 scripts/ignore_big_CVA6Flow_jsons.py -l 20      # limit in MiB
    python3 scripts/ignore_big_CVA6Flow_jsons.py --prune    # drop stale ones
"""
import argparse
import importlib.util
import os
import re
import subprocess
import sys

# A tracer JSON, and the sample .js make_CVA6Flow_sample.py wraps one in.
# Both hold a JSON and grow at the same rate.
SUFFIXES = (".json", ".js")

# GitHub warns here and refuses at 100.
DEFAULT_LIMIT_MIB = 50

# The block this script owns. Everything else in .gitignore is left alone.
BEGIN = "## BEGIN oversized JSONs"
END = "## END oversized JSONs"

# The frozen trees are the checker's, so the two scripts skip the same ones.
_spec = importlib.util.spec_from_file_location(
    "check_CVA6Flow_repo",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "check_CVA6Flow_repo.py"))
_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_check)
FROZEN = _check.FROZEN

# Never descended into: git's own store and any Node install.
SKIP_DIRS = {".git", "node_modules"}


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
GITIGNORE = os.path.join(REPO_ROOT, ".gitignore")


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


def find_candidates():
    """Every tracer file in the repository outside the frozen trees, as
    (relpath, size)."""
    found = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        here = os.path.relpath(dirpath, REPO_ROOT)
        dirnames[:] = [
            d for d in dirnames if d not in SKIP_DIRS
            and os.path.normpath(os.path.join(here, d)).replace(os.sep, "/")
            not in FROZEN]
        for filename in filenames:
            if not filename.endswith(SUFFIXES):
                continue
            full = os.path.join(dirpath, filename)
            try:
                size = os.lstat(full).st_size
            except OSError:
                continue
            found.append((os.path.relpath(full, REPO_ROOT), size))
    return sorted(found, key=lambda item: -item[1])


def git(*args, stdin=None):
    """Run git in the repository. Returns (returncode, stdout)."""
    try:
        result = subprocess.run(("git",) + args, cwd=REPO_ROOT, input=stdin,
                                capture_output=True, text=True)
    except OSError as e:
        print(f"[WARN] Could not run git: {e}")
        return 1, ""
    return result.returncode, result.stdout


def tracked_paths():
    """Every path git is already carrying."""
    code, out = git("ls-files", "-z")
    if code != 0:
        return set()
    return {p for p in out.split("\0") if p}


def ignored_paths(paths):
    """The subset git already ignores, by whatever rule."""
    if not paths:
        return set()
    code, out = git("check-ignore", "-z", "--stdin",
                    stdin="\0".join(paths) + "\0")
    if code not in (0, 1):
        print("[WARN] git check-ignore failed, treating nothing as ignored")
        return set()
    return {p for p in out.split("\0") if p}


def pattern_for(rel):
    """The .gitignore line matching exactly this path, anchored at the root."""
    escaped = re.sub(r"([\[\]*?\\ #!])", r"\\\1", rel.replace(os.sep, "/"))
    return "/" + escaped


def path_for(pattern):
    """The path a line of ours came from, so --prune can measure it again."""
    return re.sub(r"\\(.)", r"\1", pattern.lstrip("/"))


def owns(pattern):
    """Whether this line is one this script could have written: a rooted path
    to a single tracer file that is not a directory."""
    if not pattern.startswith("/") or pattern.endswith("/"):
        return False
    path = path_for(pattern)
    if not path.endswith(SUFFIXES):
        return False
    return not os.path.isdir(os.path.join(REPO_ROOT, path))


def read_gitignore():
    """(lines before our block, our entries, lines after our block), or None
    when the block has no end, which is left for a person to fix."""
    if not os.path.isfile(GITIGNORE):
        return [], [], []

    with open(GITIGNORE, encoding="utf-8") as f:
        lines = f.read().splitlines()

    if BEGIN not in lines:
        return lines, [], []

    start = lines.index(BEGIN)
    rest = lines[start + 1:]
    if END not in rest:
        print(f"[ERROR] {GITIGNORE} has our opening marker but no closing "
              f"'{END}'. Fix that by hand first, refusing to guess where the "
              f"block ends.", file=sys.stderr)
        return None

    stop = start + 1 + rest.index(END)
    entries = [line.strip() for line in lines[start + 1:stop]
               if line.strip() and not line.strip().startswith("#")]
    return lines[:start], entries, lines[stop + 1:]


def write_gitignore(before, entries, after):
    """Put the block back. Blank lines around it are normalised, and the rest
    of the file is left as it was."""
    block = [BEGIN, *entries, END]

    while before and not before[-1].strip():
        before.pop()
    body = before + ([""] if before else []) + block
    tail = list(after)
    while tail and not tail[0].strip():
        tail.pop(0)
    if tail:
        body += [""] + tail

    with open(GITIGNORE, "w", encoding="utf-8") as f:
        f.write("\n".join(body).rstrip("\n") + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Add the tracer JSONs too big for GitHub to .gitignore.")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Write .gitignore without asking")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be added and stop")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="List the files under the limit too")
    parser.add_argument("-l", "--limit", type=float,
                        default=DEFAULT_LIMIT_MIB, metavar="MIB",
                        help=f"Size a file has to reach to be listed, in MiB "
                             f"(default {DEFAULT_LIMIT_MIB})")
    parser.add_argument("--prune", action="store_true",
                        help="Drop entries whose file is gone or now smaller "
                             "than the limit")
    args = parser.parse_args()

    limit = int(args.limit * 1024 * 1024)
    print(f"[INFO] Searching in: {REPO_ROOT}")
    print(f"[INFO] Limit: {args.limit:g} MiB")

    candidates = find_candidates()
    big = [(rel, size) for rel, size in candidates if size >= limit]
    small = len(candidates) - len(big)

    current = read_gitignore()
    if current is None:
        return 1
    before, entries, after = current
    listed = set(entries)
    tracked = tracked_paths()
    ignored = ignored_paths([rel for rel, _ in big])

    rows, added, warned = [], [], []
    for rel, size in big:
        pattern = pattern_for(rel)
        if rel in tracked:
            state = "tracked, cannot ignore"
            warned.append(rel)
        elif pattern in listed:
            state = "already listed"
        elif rel in ignored:
            state = "already ignored"
        else:
            state = "to add"
            added.append(pattern)
        rows.append((size, rel, state))

    dropped, hand_written = [], []
    if args.prune:
        for pattern in entries:
            if not owns(pattern):
                hand_written.append(pattern)
                continue
            full = os.path.join(REPO_ROOT, path_for(pattern))
            try:
                if os.lstat(full).st_size >= limit:
                    continue
            except OSError:
                pass
            dropped.append(pattern)

    print("\n" + "=" * 78)
    print(f"OVER {args.limit:g} MiB")
    print("=" * 78)
    if rows:
        for size, rel, state in rows:
            print(f"{human(size):>10}  {rel:<52} {state}")
    else:
        print("(none)")
    if args.verbose:
        for rel, size in candidates:
            if size < limit:
                print(f"{human(size):>10}  {rel:<52} under the limit")
    for pattern in dropped:
        print(f"{'-':>10}  {path_for(pattern):<52} to drop")
    print("=" * 78)
    print(f"{len(big)} over the limit, {small} under, "
          f"{len(added)} to add, {len(dropped)} to drop\n")

    if warned:
        print(f"[WARN] {len(warned)} file(s) over the limit are already "
              f"tracked. .gitignore does not untrack anything, so these stay "
              f"in the history until they are removed by hand:")
        for rel in warned:
            print(f"           git rm --cached {rel}")
        print()

    if hand_written:
        print(f"[INFO] {len(hand_written)} line(s) were written by hand, "
              f"not by this script, so --prune leaves them alone:")
        for pattern in hand_written:
            print(f"           {pattern}")
        print()

    if not added and not dropped:
        print("[INFO] .gitignore is already up to date")
        return 0

    if args.dry_run:
        print("[INFO] Dry run, .gitignore was not changed")
        return 0

    if not args.yes:
        try:
            reply = input("Write these to .gitignore? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[INFO] Cancelled")
            return 0
        if reply not in ("y", "yes"):
            print("[INFO] Cancelled")
            return 0

    kept = [e for e in entries if e not in dropped]
    written = sorted(set(kept + added))
    try:
        write_gitignore(before, written, after)
    except OSError as e:
        print(f"[ERROR] Could not write {GITIGNORE}: {e}", file=sys.stderr)
        return 1
    print(f"[INFO] .gitignore updated: {len(added)} added, "
          f"{len(dropped)} dropped, {len(written)} listed in total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
