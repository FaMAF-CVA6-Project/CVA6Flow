#!/usr/bin/env python3
"""Check the CVA6Flow repository.

The same tool the CVA6 fork runs over itself, with the checks a viewer
repository can answer. Its helpers and its check protocol are taken
from scripts/check_CVA6_repo.py in that fork unchanged, so the two
cannot drift: a check returns a list of messages, empty when it passes,
and a message starting with "SKIP " means it could not run.

    python3 scripts/check_CVA6Flow_repo.py
    python3 scripts/check_CVA6Flow_repo.py --list
    python3 scripts/check_CVA6Flow_repo.py -k formatting
"""
import argparse
import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile


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


REPO = repo_root()

# The whole repository. Everything in it belongs to this project.
OWN_PATHS = (".",)

# Frozen artefacts, kept as published and held to no convention.
FROZEN = ("docs/CARLA2026", "docs/old_versions", "docs/parser_phases")

# The style is 79 columns. This is a ratchet: the count may fall
# but never rise, so new sprawl fails and old sprawl is not a
# standing red mark.
# Scripts named in our text that do not live here. The CVA6 fork's tools are
# listed rather than looked up: they resolve only when this repository sits
# inside that fork, and a standalone clone must still be checkable. A rename
# in the fork makes this repository's text stale, and this is what says so.
EXTERNAL_SCRIPTS = {
    "cva6.py",                          # the verif/sim driver
    "my_config.py",                     # an example name
    "MinorFlow_tracer.py",           # the sibling viewer
    # In the CVA6 fork:
    "check_CVA6_repo.py",
    "create_all_CVA6_repo_jsons.py",
    "run_CVA6_testing_sweep.py",
    "gem5_config_CVA6.py",
    "gem5_config_CVA6_Patch.py",
}

MAX_COLS = 79
WIDTH_BUDGET = 71


# -------------------------------------------------------------------------
# Helpers, verbatim from the fork's checker.
# -------------------------------------------------------------------------

def owned(pattern=None):
    """Our files, from git so ignored artefacts never reach a check."""
    out = []
    for root in OWN_PATHS:
        full = os.path.join(REPO, root)
        if os.path.isfile(full):
            out.append(root)
            continue
        if not os.path.isdir(full):
            continue
        # A submodule has its own index, so ask the right repository.
        # Its .git is a file rather than a directory, which is why this
        # is exists and not isdir: isdir skipped the submodules whole.
        inner = full if os.path.exists(os.path.join(full, ".git")) else REPO
        rel = "." if inner == full else root
        # --others --exclude-standard adds files not yet staged, respecting
        # .gitignore. Without it a freshly moved tree is invisible here, which
        # is exactly when the checks are worth most.
        r = subprocess.run(["git", "-C", inner, "ls-files", "--cached",
                            "--others", "--exclude-standard", rel],
                           capture_output=True, text=True)
        # Submodule paths come back relative to the submodule, so they
        # need the prefix here. A root of "." already is the repository.
        prefix = root + "/" if inner == full and root != "." else ""
        out += [prefix + p for p in r.stdout.split()]
    # git ls-files reports the index, which still carries files deleted in the
    # working tree. A mid-reorganisation checkout is exactly when this matters.
    out = [p for p in out if not any(f in p for f in FROZEN)
           and os.path.isfile(os.path.join(REPO, p))]
    if pattern:
        out = [p for p in out if p.endswith(pattern)]
    return sorted(set(out))

def read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as handle:
        return handle.read()


# -------------------------------------------------------------------------
# Checks. Each returns a list of failure messages, empty when it passes.
# -------------------------------------------------------------------------

def check_compiles():
    """Every script we own parses, gem5 configurations included."""
    bad = []
    for rel in owned(".py"):
        try:
            ast.parse(read(rel))
        except SyntaxError as e:
            bad.append(f"{rel}:{e.lineno}: {e.msg}")
    return bad


def check_pyflakes():
    """Every script we own is clean under pyflakes."""
    if subprocess.run([sys.executable, "-m", "pyflakes", "--version"],
                      capture_output=True).returncode != 0:
        return ["SKIP pyflakes is not installed (pip install pyflakes)"]
    files = [os.path.join(REPO, p) for p in owned(".py")]
    r = subprocess.run([sys.executable, "-m", "pyflakes"] + files,
                       capture_output=True, text=True)
    return [line for line in r.stdout.splitlines() if line.strip()]


def is_cli(rel):
    """A script with a command line, as opposed to a gem5 configuration, which
    only runs inside gem5 and cannot answer --help here."""
    text = read(rel)
    if "import m5" in text or "from m5" in text or "from gem5" in text:
        return False
    return "argparse" in text and '__main__' in text


def check_help():
    """Every command-line script answers --help.

    It is the cheapest end-to-end test there is: it runs module-level code and
    builds the whole parser, which is where a missing import or an argument
    referenced but never added shows up."""
    bad = []
    for rel in owned(".py"):
        if not is_cli(rel):
            continue
        r = subprocess.run([sys.executable, os.path.join(REPO, rel), "--help"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            first = (r.stderr.strip().splitlines() or ["no output"])[-1]
            bad.append(f"{rel} --help exited {r.returncode}: {first}")
    return bad


def check_viewer_js():
    """The viewer pages' inline JavaScript parses."""
    if not shutil.which("node"):
        return ["SKIP node is not on PATH"]
    bad = []
    block = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)
    for rel in owned(".html"):
        scripts = block.findall(read(rel))
        if not scripts:
            continue
        handle, path = tempfile.mkstemp(suffix=".js")
        with os.fdopen(handle, "w") as out:
            out.write("\n;\n".join(scripts))
        r = subprocess.run(["node", "--check", path],
                           capture_output=True, text=True)
        os.unlink(path)
        if r.returncode != 0:
            first = (r.stderr.strip().splitlines() or ["parse error"])
            detail = next((x for x in first if "Error" in x), first[-1])
            bad.append(f"{rel}: {detail.strip()}")
    return bad


def check_links():
    """Every relative link in our markdown resolves."""
    bad = []
    link = re.compile(r"\]\(([^)\s]+)\)")
    for rel in owned(".md"):
        base = os.path.dirname(os.path.join(REPO, rel))
        for target in link.findall(read(rel)):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            if not os.path.exists(os.path.join(base, target.split("#")[0])):
                bad.append(f"{rel}: {target}")
    return bad


def check_formatting():
    """No trailing whitespace, a final newline, and no new over-long lines."""
    bad, wide = [], 0
    for rel in owned():
        if not rel.endswith((".py", ".md", ".sh", "Dockerfile")):
            continue
        text = read(rel)
        if text and not text.endswith("\n"):
            bad.append(f"{rel}: no newline at end of file")
        for number, line in enumerate(text.split("\n"), 1):
            if line != line.rstrip():
                bad.append(f"{rel}:{number}: trailing whitespace")
            if rel.endswith(".py") and len(line) > MAX_COLS:
                wide += 1
    if wide > WIDTH_BUDGET:
        bad.append(f"{wide} lines over {MAX_COLS} columns, up from "
                   f"{WIDTH_BUDGET}. Wrap the new ones, or raise "
                   f"WIDTH_BUDGET deliberately")
    elif WIDTH_BUDGET - wide >= 10:
        # Only worth saying after a real tidy-up. Wrapping one line while
        # working on something else should not produce a chore.
        bad.append(f"SKIP {wide} lines over {MAX_COLS} columns, down from "
                   f"{WIDTH_BUDGET}. Lower WIDTH_BUDGET to hold the gain")
    return bad


def check_script_names():
    """Every script named in our docs, Dockerfiles and scripts exists."""
    import re
    known = {os.path.basename(p) for p in owned(".py")}
    bad = []
    for rel in owned():
        if not rel.endswith((".md", ".py", "Dockerfile")):
            continue
        for match in re.finditer(r"(?<![\w>])([A-Za-z][A-Za-z0-9_]*\.py)\b",
                                 read(rel)):
            name = match.group(1)
            if name in known or name in EXTERNAL_SCRIPTS:
                continue
            line = read(rel)[:match.start()].count("\n") + 1
            bad.append(f"{rel}:{line}: {name} does not exist here")
    return sorted(set(bad))


def check_cycle_fields():
    """The cycle-typed field lists agree.

    make_<viewer>_sample.py clips them and make_<viewer>_oversized.py shifts
    them, so the two lists have to name the same fields. Adding a field to the
    tracer and updating only one of them leaves the other silently wrong: a
    sample clipped on nine fields out of ten looks fine and is not."""
    import re
    lists = {}
    for rel in owned(".py"):
        if "make_" not in rel:
            continue
        try:
            tree = ast.parse(read(rel))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and getattr(node.targets[0], "id", "") == "CYCLE_FIELDS"):
                lists[rel] = [e.value for e in node.value.elts
                              if isinstance(e, ast.Constant)]
    if len(lists) < 2:
        return [f"SKIP only {len(lists)} CYCLE_FIELDS list(s) found"]

    (first, fields), *rest = sorted(lists.items())
    bad = []
    for rel, other in rest:
        if other != fields:
            missing = [f for f in fields if f not in other]
            extra = [f for f in other if f not in fields]
            bad.append(f"{first} and {rel} disagree")
            if missing:
                bad.append(f"    not in {rel}: {', '.join(missing)}")
            if extra:
                bad.append(f"    not in {first}: {', '.join(extra)}")

    # Only a page list that rebases every field is comparable. MinorFlow's
    # CYCLE_KEYS is one: it subtracts an offset from each, so a field missing
    # from it silently keeps un-rebased values. CVA6Flow's SPAN_FIELDS is not,
    # being the fetch-to-commit envelope that sizes a window, which is why it
    # is named apart rather than held to this.
    for rel in owned(".html"):
        m = re.search(r"const\s+CYCLE_KEYS\s*=\s*\[(.*?)\]", read(rel), re.S)
        if not m:
            continue
        page = re.findall(r"'([^']+)'", m.group(1))
        unknown = [f for f in fields if f not in page]
        if unknown:
            bad.append(f"{rel} rebases on CYCLE_KEYS and does not list "
                       f"{', '.join(unknown)}, which {first} treats as cycle "
                       f"fields")
    return bad


CHECKS = (
    ("compiles", check_compiles),
    ("pyflakes", check_pyflakes),
    ("help", check_help),
    ("viewer-js", check_viewer_js),
    ("cycle-fields", check_cycle_fields),
    ("script-names", check_script_names),
    ("links", check_links),
    ("formatting", check_formatting),
)
OPTIONAL = ()


def main():
    parser = argparse.ArgumentParser(
        description="Check this project's own files. Upstream OpenHW is left "
                    "alone.")
    parser.add_argument("-k", "--only", metavar="TEXT",
                        help="Run only the checks whose name contains TEXT")
    parser.add_argument("--patch-roundtrip", action="store_true",
                        help="Also apply and revert MinorCPU_CVA6.patch "
                             "against pristine gem5. Needs the network")
    parser.add_argument("--list", action="store_true",
                        help="Name the checks and stop")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Print only the checks that fail")
    args = parser.parse_args()

    checks = list(CHECKS) + (list(OPTIONAL) if args.patch_roundtrip else [])
    if args.only:
        checks = [c for c in checks if args.only in c[0]]
    if args.list:
        for name, function in list(CHECKS) + list(OPTIONAL):
            print(f"  {name:16} {(function.__doc__ or '').splitlines()[0]}")
        return 0
    if not checks:
        print(f"[ERROR] No check matches '{args.only}'")
        return 2

    failed = skipped = 0
    for name, function in checks:
        try:
            problems = function()
        except Exception as e:                       # a broken check is news
            problems = [f"the check itself raised {type(e).__name__}: {e}"]
        skips = [p for p in problems if p.startswith("SKIP ")]
        real = [p for p in problems if not p.startswith("SKIP ")]
        if real:
            failed += 1
            print(f"[FAIL] {name}")
            for problem in real[:20]:
                print(f"         {problem}")
            if len(real) > 20:
                print(f"         ... and {len(real) - 20} more")
        elif skips:
            skipped += 1
            if not args.quiet:
                print(f"[SKIP] {name}: {skips[0][5:]}")
        elif not args.quiet:
            print(f"[ ok ] {name}")

    total = len(checks)
    print(f"\n{total - failed - skipped} passed, {failed} failed, "
          f"{skipped} skipped, of {total}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
