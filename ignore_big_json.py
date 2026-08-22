#!/usr/bin/env python3
"""Find the tracer JSONs too big to push, and add them to .gitignore.

A tracer JSON runs to hundreds of megabytes. GitHub warns above 50 MiB and
refuses above 100 MiB, and git has no size test of its own: .gitignore matches
a path, never a size.

Re-run it after a sweep. A file already tracked is reported, not ignored:
.gitignore has no effect on a file git is already carrying, which is what
keeps the committed sample visible.

  python3 ignore_big_json.py             # list, then ask
  python3 ignore_big_json.py -y          # write without asking
  python3 ignore_big_json.py --dry-run   # list only
  python3 ignore_big_json.py -l 20       # a different threshold, in MiB
  python3 ignore_big_json.py --prune     # also drop entries no longer oversized
"""
import os
import re
import sys
import argparse
import subprocess

# The tracer output: the viewer JSON, and the .js that wraps it for local
# loading. Both hold the same trace and grow at the same rate.
SUFFIXES = (".json", ".js")

# GitHub warns here and refuses at 100.
DEFAULT_LIMIT_MIB = 50

# The block this script owns. Everything else in .gitignore is left alone.
BEGIN = "## BEGIN oversized JSONs"
END = "## END oversized JSONs"

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
GITIGNORE = os.path.join(REPO_ROOT, ".gitignore")


def find_candidates():
    """Every tracer file in the repository, as (relpath, size)."""
    found = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d != ".git"]
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


def human(size):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024


def read_gitignore():
    """(lines before our block, our entries, lines after our block)."""
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
              f"block ends.")
        sys.exit(1)

    stop = start + 1 + rest.index(END)
    entries = [l.strip() for l in lines[start + 1:stop]
               if l.strip() and not l.strip().startswith("#")]
    return lines[:start], entries, lines[stop + 1:]


def write_gitignore(before, entries, after, limit_mib):
    """Put the block back, with the rest of the file untouched."""
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
    parser.add_argument("-n", "--dry-run", action="store_true",
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

    before, entries, after = read_gitignore()
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

    dropped = []
    if args.prune:
        for pattern in entries:
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

    if not added and not dropped:
        print("[INFO] .gitignore is already up to date")
        return

    if args.dry_run:
        print("[INFO] Dry run, .gitignore was not changed")
        return

    if not args.yes:
        try:
            reply = input("Write these to .gitignore? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[INFO] Cancelled")
            return
        if reply not in ("y", "yes"):
            print("[INFO] Cancelled")
            return

    kept = [e for e in entries if e not in dropped]
    write_gitignore(before, sorted(set(kept + added)), after, args.limit)
    print(f"[INFO] .gitignore updated: {len(added)} added, "
          f"{len(dropped)} dropped, {len(kept + added)} listed in total")


if __name__ == "__main__":
    main()
