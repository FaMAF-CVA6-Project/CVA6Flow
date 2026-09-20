#!/usr/bin/env python3
"""Format this repository's Python, Markdown and benchmarks, and nothing else.

autopep8 at 79 columns for Python and Prettier for Markdown, plus a third
pass over the benchmarks. autopep8 is pinned, Prettier is whatever is
installed.

    python3 scripts/format_CVA6Flow_repo.py            # format in place
    python3 scripts/format_CVA6Flow_repo.py --check    # report only
    python3 scripts/format_CVA6Flow_repo.py --python   # one language
    python3 scripts/format_CVA6Flow_repo.py -v         # name every file

The benchmarks get the parent repository's .editorconfig rules, no trailing
whitespace and a final newline, in C and assembly alike, and the assembly also
gets a two-space indent and aligned operands. No C style is imposed, because
no C formatter is configured here.

Scope is check_CVA6Flow_repo.py's owned(), which skips FROZEN, so the frozen
artefacts under docs/ are never touched. The CVA6 fork carries the same tool
for itself, and its copy covers this repository too as a submodule.
"""
import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# The scope and the file list come from the checker, so the two cannot drift.
_spec = importlib.util.spec_from_file_location(
    "check_CVA6Flow_repo", os.path.join(HERE, "check_CVA6Flow_repo.py"))
_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_check)
REPO = _check.REPO

# Matches MAX_COLS in the checker. A different value here would make the
# formatter and the width budget disagree on every long line.
PY_COLS = _check.MAX_COLS


def python_files():
    return [r for r in _check.owned() if r.endswith(".py")]


def markdown_files():
    return [r for r in _check.owned() if r.endswith(".md")]


def benchmark_files():
    """The benchmark sources, which no third-party formatter here covers."""
    return [r for r in _check.owned()
            if r.endswith((".S", ".c"))
            and (r.startswith("benchmarks/") or "/benchmarks/" in r)]


# An instruction or directive line: indent, mnemonic, operands.
ASM_INSTR = re.compile(r"^(\s+)(\S+)(\s+)(\S.*)$")

# Comment lines, which would otherwise be realigned as if their marker were a
# mnemonic.
ASM_COMMENT_MARKS = ("#", "/*", "*", "//")


def format_asm(text):
    """Operands one space past the file's longest mnemonic, indented two
    spaces. Per file rather than per block, which is what the tree follows."""
    lines = [ln.rstrip() for ln in text.split("\n")]

    def instruction(ln):
        return (ASM_INSTR.match(ln) and not ln[:1].strip()
                and not ln.lstrip().startswith(ASM_COMMENT_MARKS))

    widest = max((len(ASM_INSTR.match(ln).group(2))
                  for ln in lines if instruction(ln)), default=0)
    out = []
    for ln in lines:
        if instruction(ln):
            m = ASM_INSTR.match(ln)
            out.append("  " + m.group(2).ljust(widest + 1) + m.group(4))
        else:
            out.append(ln)
    return "\n".join(out)


def format_c(text):
    """Trailing whitespace only. No C formatter is configured for this tree,
    so imposing a style would be inventing one."""
    return "\n".join(ln.rstrip() for ln in text.split("\n"))


def run_benchmarks(files, check, verbose):
    """Whitespace and a final newline on every benchmark, plus operand
    alignment on the assembly. The parent repository's .editorconfig asks
    for the first two."""
    changed = []
    for rel in files:
        path = os.path.join(REPO, rel)
        with open(path, encoding="utf-8") as handle:
            src = handle.read()
        out = format_asm(src) if rel.endswith(".S") else format_c(src)
        if out and not out.endswith("\n"):
            out += "\n"
        if out != src:
            changed.append(rel)
            if not check:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(out)
        if verbose:
            print(f"  bench {rel}")
    return changed, None


# The toolchain this tree was formatted with. autopep8's output moves with its
# own version, pycodestyle's and Python's, so a check on another toolchain
# would report drift that is not there, and is skipped there instead.
FORMATTED_WITH = {"autopep8": "2.3.2", "pycodestyle": "2.14.0",
                  "python": "3.10"}


def toolchain():
    """This machine's Python, autopep8 and pycodestyle versions, asked of
    autopep8 once, or None when autopep8 does not run."""
    done = subprocess.run([sys.executable, "-m", "autopep8", "--version"],
                          capture_output=True, text=True)
    if done.returncode != 0:
        return None
    have = {"python": "%d.%d" % sys.version_info[:2]}
    found = re.search(r"autopep8 (\S+) \(pycodestyle: ([^)\s]+)\)",
                      done.stdout)
    if found:
        have["autopep8"], have["pycodestyle"] = found.groups()
    return have


def toolchain_drift(have):
    """How this machine differs from FORMATTED_WITH, as 'name have, not want'
    pieces, or an empty list when it formats the way the tree was formatted."""
    return [f"{name} {have.get(name, 'missing')}, not {want}"
            for name, want in FORMATTED_WITH.items()
            if have.get(name) != want]


def autopep8_cmd(check):
    cmd = [sys.executable, "-m", "autopep8", "--max-line-length", str(PY_COLS)]
    return cmd + (["--diff"] if check else ["--in-place"])


def prettier_base():
    """The argv prefix that runs Prettier here, or None.

    Probed rather than assumed. npx exits 0 while refusing to install a
    missing package, so a command that merely exists is not one that runs,
    and taking its failure for unformatted Markdown would be a false report.
    Prettier is usually the editor's copy, hence npx as the fallback."""
    for base in (["prettier"], ["npx", "--no-install", "prettier"]):
        if not shutil.which(base[0]):
            continue
        done = subprocess.run(base + ["--version"],
                              capture_output=True, text=True)
        if done.returncode == 0 and done.stdout.strip():
            return base
    return None


def run_python(files, check, verbose):
    """Returns the files that changed, or that would change under --check."""
    if not files:
        return [], None
    have = toolchain()
    if have is None:
        return [], "autopep8 is not installed (pip install autopep8)"
    drift = toolchain_drift(have)
    if drift and check:
        return [], ("autopep8 check skipped, the toolchain differs from the "
                    "one this tree was formatted with (" + ", ".join(drift)
                    + "). pip install autopep8=="
                    + FORMATTED_WITH["autopep8"] + " pycodestyle=="
                    + FORMATTED_WITH["pycodestyle"] + " on Python "
                    + FORMATTED_WITH["python"])
    if drift:
        print("[WARN] Formatting with " + ", ".join(drift) + ", so files "
              "may change only because the toolchain does.")
    changed = []
    for rel in files:
        path = os.path.join(REPO, rel)
        # Asked first either way, because --in-place is silent and a file
        # compared with itself afterwards is always clean.
        diff = subprocess.run(autopep8_cmd(True) + [path],
                              capture_output=True, text=True)
        if diff.stdout.strip():
            changed.append(rel)
            if not check:
                subprocess.run(autopep8_cmd(False) + [path],
                               capture_output=True, text=True)
        if verbose:
            print(f"  py {rel}")
    return changed, None


def run_markdown(files, check, verbose):
    if not files:
        return [], None
    base = prettier_base()
    if base is None:
        return [], "prettier is not reachable (npm i -g prettier)"
    # Asked first either way, because --write reports every file it wrote
    # rather than only the ones it changed.
    done = subprocess.run(base + ["--check"] + files, capture_output=True,
                          text=True, cwd=REPO)
    if verbose and done.stdout:
        print("  " + done.stdout.strip().replace("\n", "\n  "))
    if done.returncode == 0:
        return [], None
    # Prettier names each offender on stderr as "[warn] path".
    listed = [ln.split("]", 1)[1].strip() for ln in done.stderr.splitlines()
              if ln.startswith("[warn]") and ln.strip().endswith(".md")]
    if not listed:
        err = done.stderr.strip().splitlines()
        return [], f"prettier failed: {err[0][:100] if err else 'no output'}"
    if not check:
        wrote = subprocess.run(base + ["--write"] + listed,
                               capture_output=True, text=True, cwd=REPO)
        if wrote.returncode != 0:
            return [], f"prettier failed: {wrote.stderr.strip()[:100]}"
    return listed, None


def main():
    parser = argparse.ArgumentParser(
        description="Format this repository's Python, Markdown and "
                    "benchmark sources.")
    parser.add_argument("--check", action="store_true",
                        help="Report what is unformatted and change nothing. "
                             "Exits non-zero when anything would change")
    parser.add_argument("--python", action="store_true",
                        help="Python only")
    parser.add_argument("--markdown", action="store_true",
                        help="Markdown only")
    parser.add_argument("--benchmarks", action="store_true",
                        help="Benchmark sources only")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Name every file as it is handled")
    args = parser.parse_args()

    every_language = not (args.python or args.markdown or args.benchmarks)
    changed, skipped = [], []

    # Outside a git checkout the file list cannot be read, and formatting
    # nothing would look like a clean tree.
    try:
        _check.owned()
    except RuntimeError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2

    if args.python or every_language:
        files = python_files()
        got, why = run_python(files, args.check, args.verbose)
        if why:
            skipped.append(why)
        else:
            changed += got
            print(f"[INFO] Python: {len(files)} file(s) at {PY_COLS} columns, "
                  f"{len(got)} {'unformatted' if args.check else 'changed'}")

    if args.markdown or every_language:
        files = markdown_files()
        got, why = run_markdown(files, args.check, args.verbose)
        if why:
            skipped.append(why)
        else:
            changed += got
            print(f"[INFO] Markdown: {len(files)} file(s), "
                  f"{len(got)} {'unformatted' if args.check else 'changed'}")

    if args.benchmarks or every_language:
        files = benchmark_files()
        got, why = run_benchmarks(files, args.check, args.verbose)
        if why:
            skipped.append(why)
        else:
            changed += got
            print(f"[INFO] Benchmarks: {len(files)} file(s), "
                  f"{len(got)} {'unformatted' if args.check else 'changed'}")

    for why in skipped:
        print(f"[SKIP] {why}")
    for rel in changed:
        print(f"  {rel}")

    if args.check and changed:
        print("[ERROR] Run 'python3 scripts/format_CVA6Flow_repo.py' to "
              "fix these.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
