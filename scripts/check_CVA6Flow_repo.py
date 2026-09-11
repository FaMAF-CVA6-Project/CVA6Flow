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
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize


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

# Scripts named in our text that do not live here. The CVA6 fork's tools are
# listed rather than looked up: a standalone clone must still be checkable,
# and a rename in the fork makes this text stale, which is what says so.
EXTERNAL_SCRIPTS = {
    "cva6.py",                          # the verif/sim driver
    "my_config.py",                     # an example name
    "MinorFlow_tracer.py",           # the sibling viewer
    # In the CVA6 fork:
    "check_CVA6_repo.py",
    "create_all_CVA6_repo_jsons.py",
    "run_config_search_sweep.py",
    "check_patch_parity.py",
    "gem5_config_CVA6.py",
    "gem5_config_CVA6_patch.py",
}

# The style is 79 columns. The budget is a ratchet: it may fall but never
# rise, so new sprawl fails while old sprawl is not a standing red mark.
MAX_COLS = 79
WIDTH_BUDGET = 71

# Comment prose. A semicolon becomes a comma or a period, the tree is ASCII,
# and a comment on a line of code runs to three lines at most.
MAX_COMMENT_LINES = 3

# The tracers and the viewer pages are design notes throughout, citing RTL
# lines and measured counts, so cutting those to three lines would drop the
# evidence. A ratchet instead: the count may fall but never rise.
DESIGN_NOTES = ("_tracer.py", ".html")
DESIGN_NOTE_BUDGET = 44

# Files this check knows how to read. Anything else is data or a licence.
COMMENTED = (".patch", ".py", ".md", ".html", ".c", ".h", ".cc", ".hh",
             ".sv", ".js", ".S", ".yml")

# Non-ASCII that stays: accented letters spell people's names, and each glyph
# named here is one the page renders or the tracer prints, so the comment
# naming it is right to use it.
NON_ASCII = re.compile("[^\x00-\x7f\u00c0-\u024f\u00b5\u25aa\u2550]")

# A row of a table, and a line of code quoted inside a comment. Both keep
# their own punctuation, so neither is held to the prose rules.
TABULAR = re.compile(r"\S {2,}\S")
# A section heading, which introduces what follows rather than explaining a
# line of code, so it neither joins a block nor counts toward its length.
BANNER = re.compile(r"^[-=_*]{3,}")
CODEISH = re.compile(r"//|\bfor\b.*;|^\s*[\"\'].*[\"\']\s*,?$"
                     r"|=\s*\w+\s*;|\w+\(.*\)\s*;|^\s*[-|+]{3,}")

# Licence text is boilerplate the file is required to carry, semicolons and
# all, so the prose rules stop at its edges.
LICENCE_START = re.compile(r"Copyright \(c\)|Licensed under|SPDX-License")
LICENCE_END = re.compile(r"SUCH DAMAGE|limitations under the License")

# A docstring is the string token that opens a file, a class or a function.
DOCSTRING_AFTER = (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                   tokenize.NL, tokenize.ENCODING)


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
# Comment extraction. Each returns (line, text, kind), where kind is own for a
# comment on its own line, inline for one after code, and doc for a file
# header, a docstring or a document, which the line limit does not reach.
# ---------------------------------------------------------------------------
def comment_scan(text, marks, block=None, quotes="\"'"):
    """C-family and hash comments in one pass, so a marker inside a string is
    not a comment and an apostrophe inside prose does not open a string."""
    rows, i, n, col, end = [], 0, 1, 0, len(text)
    while i < end:
        char = text[i]
        if char == "\n":
            n, col, i = n + 1, 0, i + 1
            continue
        if any(text.startswith(mark, i) for mark in marks):
            stop = text.find("\n", i)
            stop = end if stop < 0 else stop
            rows.append((n, text[i:stop],
                         "own" if not text[i - col:i].strip() else "inline"))
            col, i = col + stop - i, stop
            continue
        if block and text.startswith(block[0], i):
            stop = text.find(block[1], i + len(block[0]))
            stop = end if stop < 0 else stop + len(block[1])
            body = text[i:stop]
            head = "own" if not text[i - col:i].strip() else "inline"
            for offset, line in enumerate(body.split("\n")):
                rows.append((n + offset, line.strip(),
                             head if offset == 0 else "own"))
            n += body.count("\n")
            col = (len(body) - body.rfind("\n") - 1 if "\n" in body
                   else col + len(body))
            i = stop
            continue
        if char in quotes:
            i, col = i + 1, col + 1
            while i < end and text[i] not in (char, "\n"):
                i += 2 if text[i] == "\\" else 1
                col += 1
            if i < end and text[i] == "\n":
                n, col, i = n + 1, 0, i + 1
                continue
        i, col = i + 1, col + 1
    return rows


def python_comments(text):
    """From the tokeniser, so a # inside a string stays a string. Docstrings
    are prose too, but they head a file or a function, so they come back as
    doc and are held to everything except the line limit."""
    rows = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return rows
    lines = text.split("\n")
    previous = tokenize.NEWLINE
    for token in tokens:
        if token.type == tokenize.COMMENT:
            head = lines[token.start[0] - 1][:token.start[1]].strip()
            rows.append((token.start[0], token.string,
                         "inline" if head else "own"))
        elif token.type == tokenize.STRING and previous in DOCSTRING_AFTER:
            for offset, line in enumerate(token.string.split("\n")):
                rows.append((token.start[0] + offset, line, "doc"))
        if token.type != tokenize.NL:
            previous = token.type
    return rows


def html_comments(text):
    """The page's own comments, and the JavaScript inside every script tag."""
    rows = comment_scan(text, (), ("<!--", "-->"), "")
    for match in re.finditer(r"<script[^>]*>(.*?)</script>", text,
                             re.S | re.I):
        base = text[:match.start(1)].count("\n")
        rows += [(base + n, body, kind) for n, body, kind
                 in comment_scan(match.group(1), ("//",), ("/*", "*/"))]
    return sorted(rows)


def markdown_prose(text):
    """A document is prose throughout, minus its code blocks."""
    rows, fenced = [], False
    for n, line in enumerate(text.split("\n"), 1):
        indented = line.startswith((" " * 4, "\t"))
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and line.strip() and not indented:
            rows.append((n, line, "doc"))
    return rows


def patch_comments(text):
    """Only the lines the patch adds are ours, the context is gem5's. A file
    the patch creates opens with a licence and a header, so those are doc."""
    added, preamble, heading = [], set(), False
    for n, line in enumerate(text.split("\n"), 1):
        if line.startswith("--- "):
            heading = line.startswith("--- /dev/null")
        elif line.startswith("+") and not line.startswith("+++"):
            added.append((n, line[1:]))
            body = line[1:].strip()
            if heading and body and not body.startswith(("/*", "*", "//")):
                heading = False
            elif heading:
                preamble.add(n)
    numbers = [n for n, _ in added]
    joined = "\n".join(body for _, body in added)
    return [(numbers[n - 1], body,
             "doc" if numbers[n - 1] in preamble else kind)
            for n, body, kind in comment_scan(joined, ("//",), ("/*", "*/"))]


def comment_rows(rel, text):
    """Every comment in the file, whatever it is written in."""
    if rel.endswith(".patch"):
        return patch_comments(text)
    if rel.endswith(".py"):
        return python_comments(text)
    if rel.endswith(".md"):
        return markdown_prose(text)
    if rel.endswith(".html"):
        return html_comments(text)
    if rel.endswith((".c", ".h", ".cc", ".hh", ".sv", ".js")):
        return comment_scan(text, ("//",), ("/*", "*/"))
    return comment_scan(text, ("#",), None, "\"")


def comment_text(body):
    """The prose alone: the marker gone, and quoted code with it, since code
    shown inside a comment keeps its own punctuation."""
    text = re.sub(r"^(#+|/\*+|\*+|//+|<!--|-->)", "", body.strip()).strip()
    return re.sub(r"https?://\S+", " ", re.sub(r"`[^`]*`", " ", text))


def comment_blocks(rows):
    """Runs of own-line comment lines. A bare marker is a paragraph break and
    ends the block: three lines is the limit per comment, not per run."""
    blocks, current = [], []
    for n, body, kind in rows:
        text = comment_text(body)
        keep = (kind == "own" and text.strip("-=*#_ ")
                and not BANNER.match(text))
        if keep and current and n == current[-1][0] + 1:
            current.append((n, body))
            continue
        if current:
            blocks.append(current)
        current = [(n, body)] if keep else []
    return blocks + ([current] if current else [])


def licence_lines(rows):
    """Lines inside a licence header, which the file is required to carry
    exactly as written, so the prose rules stop at its edges."""
    inside, left = set(), 0
    for n, body, _ in rows:
        if LICENCE_START.search(body):
            left = 40
        if left:
            inside.add(n)
            left = 0 if LICENCE_END.search(body) else left - 1
    return inside


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


def check_comments():
    """Comment prose: no semicolons, ASCII, and three lines to a comment.

    File headers, docstrings, licences and tables are prose of another kind
    and are not held to the line limit. The tracers and the viewer pages
    document mechanisms rather than lines of code, so their long comments are
    ratcheted like the column budget instead of being cut."""
    bad, notes = [], 0
    for rel in owned():
        if not (rel.endswith(COMMENTED)
                or os.path.basename(rel).startswith("Dockerfile")):
            continue
        text = read(rel)
        rows = comment_rows(rel, text)
        licensed = licence_lines(rows)
        # Where the file's own content starts. Everything above it introduces
        # the file rather than a line of code, however long the banner runs,
        # which a fixed line count got wrong for the Dockerfiles.
        commented = {n for n, _, _ in rows}
        content = next((n for n, line in enumerate(text.split("\n"), 1)
                        if line.strip() and n not in commented), 1)
        for n, body, _ in rows:
            if body != body.rstrip():
                bad.append(f"{rel}:{n}: trailing whitespace in a comment")
            odd = sorted(set(NON_ASCII.findall(body)))
            if odd:
                bad.append(f"{rel}:{n}: non-ASCII in a comment, "
                           + " ".join(f"U+{ord(c):04X}" for c in odd))
            text = comment_text(body)
            if ";" in text and n not in licensed and not CODEISH.search(text):
                bad.append(f"{rel}:{n}: semicolon in prose, {text[:44]}")
        for block in comment_blocks(rows):
            start, lines = block[0][0], [body for _, body in block]
            if len(lines) <= MAX_COMMENT_LINES or start < content:
                continue
            if sum(bool(TABULAR.search(b)) for b in lines) * 2 >= len(lines):
                continue
            if rel.endswith(DESIGN_NOTES):
                notes += 1
            else:
                bad.append(f"{rel}:{start}: comment of {len(lines)} lines, "
                           f"over {MAX_COMMENT_LINES}")
    if notes > DESIGN_NOTE_BUDGET:
        bad.append(f"{notes} design-note comments over {MAX_COMMENT_LINES} "
                   f"lines, up from {DESIGN_NOTE_BUDGET}. Shorten the new "
                   f"ones, or raise DESIGN_NOTE_BUDGET deliberately")
    elif DESIGN_NOTE_BUDGET - notes >= 10:
        bad.append(f"SKIP {notes} design-note comments over "
                   f"{MAX_COMMENT_LINES} lines, down from "
                   f"{DESIGN_NOTE_BUDGET}. Lower DESIGN_NOTE_BUDGET to "
                   f"hold the gain")
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


# A quoted path to a script, relative to the repository. An absolute one is a
# destination inside a container, not a file here.
SCRIPT_PATH = re.compile(
    r'"((?!/)[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+\.py)"')


def check_script_names():
    """Every script named in our docs, Dockerfiles and scripts exists, and
    every path to one resolves.

    A name that moved is caught by the first half. The second catches a path
    that went stale when a script moved folder, which a name alone cannot:
    the cleaners moving into scripts/ left one behind for a while."""
    known = {os.path.basename(p) for p in owned(".py")}
    bad = []
    for rel in owned():
        if not rel.endswith((".md", ".py", "Dockerfile")):
            continue
        text = read(rel)
        for match in re.finditer(r"(?<![\w>])([A-Za-z][A-Za-z0-9_]*\.py)\b",
                                 text):
            name = match.group(1)
            if name in known or name in EXTERNAL_SCRIPTS:
                continue
            line = text[:match.start()].count("\n") + 1
            bad.append(f"{rel}:{line}: {name} does not exist here")
        if not rel.endswith(".py"):
            continue
        for match in SCRIPT_PATH.finditer(text):
            named = match.group(1)
            if (os.path.isfile(os.path.join(REPO, named))
                    or os.path.basename(named) in EXTERNAL_SCRIPTS):
                continue
            line = text[:match.start()].count("\n") + 1
            bad.append(f"{rel}:{line}: {named} is not a path here")
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
    # CYCLE_KEYS is one, so a field missing from it keeps un-rebased values.
    # CVA6Flow's SPAN_FIELDS only sizes a window, hence the name apart.
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


def check_formatter():
    """Our Python is what autopep8 at MAX_COLS produces.

    Markdown is Prettier's and is checked only where Prettier can run, since
    it is usually the editor's copy rather than a tool on PATH."""
    script = os.path.join(REPO, "scripts", "format_CVA6Flow_repo.py")
    if not os.path.isfile(script):
        return ["SKIP scripts/format_CVA6Flow_repo.py is missing"]
    done = subprocess.run([sys.executable, script, "--check"],
                          capture_output=True, text=True, cwd=REPO)
    out = done.stdout
    # The Python half could not run meaningfully, because autopep8 is missing
    # or is not the toolchain the tree was formatted with. Either is a SKIP,
    # never a pass and never a list of files that are in fact formatted.
    python_skip = next((line[len("[SKIP] "):] for line in out.splitlines()
                        if line.startswith("[SKIP] autopep8")), None)
    if done.returncode == 0:
        return [f"SKIP {python_skip}"] if python_skip else []
    files = [ln.strip() for ln in out.splitlines() if ln.startswith("  ")]
    return [f"{f}: not what the formatter produces" for f in files] or [
        "some files are not what the formatter produces"]


CHECKS = (
    ("compiles", check_compiles),
    ("pyflakes", check_pyflakes),
    ("help", check_help),
    ("viewer-js", check_viewer_js),
    ("cycle-fields", check_cycle_fields),
    ("script-names", check_script_names),
    ("links", check_links),
    ("comments", check_comments),
    ("formatting", check_formatting),
    ("formatter", check_formatter),
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
