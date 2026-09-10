#!/usr/bin/env python3
"""Format this repository's Python, Markdown and benchmarks, and nothing else.

Two formatters, because two languages. autopep8 at 79 columns for Python,
Prettier for Markdown. Both are what the tree was last formatted with, so
running this leaves a clean tree clean.

    python3 scripts/format_CVA6Flow_repo.py            # format in place
    python3 scripts/format_CVA6Flow_repo.py --check    # report, change nothing
    python3 scripts/format_CVA6Flow_repo.py --python   # one language
    python3 scripts/format_CVA6Flow_repo.py -v         # name every file

The benchmarks get a third pass: .editorconfig's trailing whitespace and final
newline on both languages, and operand alignment on the assembly. No C style is
imposed, because no C formatter is configured for this tree.

Scope is check_CVA6Flow_repo.py's OWN_PATHS, so the frozen artefacts under docs/
are never touched. The CVA6 fork carries the same tool for itself, and its
copy covers this repository too when it is checked out as a submodule.
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


def format_asm(text):
    """Operands two spaces past the file's longest mnemonic.

    Per file rather than per block, which is what the tree already follows:
    23 of its 31 assembly sources reproduce under this rule untouched."""
    lines = [ln.rstrip() for ln in text.split("\n")]

    def instruction(ln):
        return (ASM_INSTR.match(ln) and not ln[:1].strip()
                and not ln.lstrip().startswith("#"))

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
    alignment on the assembly. .editorconfig asks for both."""
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


def autopep8_cmd(check):
    cmd = [sys.executable, "-m", "autopep8", "--max-line-length", str(PY_COLS)]
    return cmd + (["--diff"] if check else ["--in-place"])


def have_autopep8():
    done = subprocess.run([sys.executable, "-m", "autopep8", "--version"],
                          capture_output=True, text=True)
    return done.returncode == 0


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
    if not have_autopep8():
        return [], "autopep8 is not installed (pip install autopep8)"
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
        description="Format this repository's Python and Markdown.")
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

    both = not (args.python or args.markdown or args.benchmarks)
    changed, skipped = [], []

    if args.python or both:
        files = python_files()
        got, why = run_python(files, args.check, args.verbose)
        if why:
            skipped.append(why)
        else:
            changed += got
            print(f"[INFO] Python: {len(files)} file(s) at {PY_COLS} columns, "
                  f"{len(got)} {'unformatted' if args.check else 'changed'}")

    if args.markdown or both:
        files = markdown_files()
        got, why = run_markdown(files, args.check, args.verbose)
        if why:
            skipped.append(why)
        else:
            changed += got
            print(f"[INFO] Markdown: {len(files)} file(s), "
                  f"{len(got)} {'unformatted' if args.check else 'changed'}")

    if args.benchmarks or both:
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
              "fix these.")
        return 1
    if skipped and not changed:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
