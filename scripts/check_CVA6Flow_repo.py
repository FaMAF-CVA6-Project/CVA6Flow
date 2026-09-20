#!/usr/bin/env python3
"""Check the CVA6Flow repository.

The same tool the CVA6 fork runs over itself, with the checks a viewer
repository can answer. Its helpers, its check protocol and its main are shared
blocks, identical in scripts/check_CVA6_repo.py in that fork and in the other
viewer's checker, and the shared-blocks check keeps them that way. A check
returns a list of messages, empty when it passes, and a message starting with
"SKIP " means it could not run.

    python3 scripts/check_CVA6Flow_repo.py
    python3 scripts/check_CVA6Flow_repo.py --list
    python3 scripts/check_CVA6Flow_repo.py -k formatting
    python3 scripts/check_CVA6Flow_repo.py --write-shared-manifest
"""
import argparse
import ast
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize


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


REPO = repo_root()

# The whole repository. Everything in it belongs to this project.
OWN_PATHS = (".",)

# Frozen artefacts, the viewer's earlier states, held to no convention.
FROZEN = ("docs/old_versions", "docs/parser_phases")

# Scripts named in our text that do not live here. The CVA6 fork's tools are
# listed rather than looked up: a standalone clone must still be checkable,
# and a rename in the fork makes this text stale, which is what says so.
EXTERNAL_SCRIPTS = {
    "cva6.py",                          # the verif/sim driver
    # In the CVA6 fork:
    "check_CVA6_repo.py",
    "docker_run.py",
    "make_containers.py",
    "create_all_CVA6_repo_jsons.py",
    "run_CVA6_config_sweep.py",
}

# The style is 79 columns and every line keeps it. The budget is a ratchet
# at zero, so any wider line fails.
MAX_COLS = 79
WIDTH_BUDGET = 0

# Comment prose. A semicolon becomes a comma or a period, the tree is ASCII,
# and a comment on a line of code runs to three lines at most.
MAX_COMMENT_LINES = 3

# Files this check knows how to read. Anything else is data or a licence.
COMMENTED = (".patch", ".py", ".md", ".html", ".c", ".h", ".cc", ".hh",
             ".sv", ".js", ".S", ".yml")

# Non-ASCII that stays: accented letters spell people's names. Every other
# character in a comment or a document is ASCII.
NON_ASCII = re.compile("[^\x00-\x7f\u00c0-\u024f]")

# A row of a table, and a line of code quoted inside a comment. Both keep
# their own punctuation, so neither is held to the prose rules.
TABULAR = re.compile(r"\S {2,}\S")

# A section heading, which introduces what follows rather than explaining a
# line of code, so it neither joins a block nor counts towards its length.
BANNER = re.compile(r"^[-=_*]{3,}")
CODEISH = re.compile(r"//|\bfor\b.*;|^\s*[\"\'].*[\"\']\s*,?$"
                     r"|=\s*\w+\s*;|\w+\(.*\)\s*;|^\s*[-|+]{3,}")

# Licence text is boilerplate the file is required to carry, semicolons and
# all, so the prose rules stop at its edges.
LICENCE_START = re.compile(r"Copyright \(c\)|Licensed under|SPDX-License")
LICENCE_END = re.compile(r"SUCH DAMAGE|limitations under the License")

# A docstring, as far as the tokeniser can tell: a string that opens a logical
# line, which is where every file, class and function docstring sits.
DOCSTRING_AFTER = (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                   tokenize.NL, tokenize.ENCODING)

# A quoted path to a script, relative to the repository. An absolute one is a
# destination inside a container, not a file here.
SCRIPT_PATH = re.compile(
    r'"((?!/)[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+\.py)"')

# The formatter whose --check the formatter check runs.
FORMATTER_SCRIPT = os.path.join("scripts", "format_CVA6Flow_repo.py")

# A container's documentation keeps paths of its own, and no document here
# is one, so the links check skips nothing.
CONTAINER_DOCS = ()


# SHARED BEGIN py-check-files

# Needs: os, subprocess, REPO, OWN_PATHS, FROZEN


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
        # is exists and not isdir: isdir would skip the submodules whole.
        inner = full if os.path.exists(os.path.join(full, ".git")) else REPO
        rel = "." if inner == full else root
        # --others --exclude-standard adds files not yet staged, respecting
        # .gitignore. Without it a freshly moved tree is invisible here, which
        # is exactly when the checks are worth most.
        r = subprocess.run(["git", "-C", inner, "ls-files", "--cached",
                            "--others", "--exclude-standard", rel],
                           capture_output=True, text=True)
        # Outside a checkout git lists nothing and every check would pass on
        # zero files, so this raises for the check protocol to report.
        if r.returncode != 0:
            why = (r.stderr.strip().splitlines() or ["git ls-files failed"])
            raise RuntimeError(f"not a git checkout: {inner}, {why[0]}")
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

# SHARED END py-check-files


# SHARED BEGIN py-check-comment-scan

# Needs: io, re, tokenize, BANNER, DOCSTRING_AFTER, LICENCE_START, LICENCE_END


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
        # A comment is passed over like a blank line, so a module docstring
        # under a #! line still counts as one.
        if token.type not in (tokenize.NL, tokenize.COMMENT):
            previous = token.type
    return rows


def html_comments(text):
    """The page's own comments, and those of the JavaScript and the CSS inside
    every script and style tag."""
    rows = comment_scan(text, (), ("<!--", "-->"), "")
    for tag, marks in (("script", ("//",)), ("style", ())):
        for match in re.finditer(rf"<{tag}[^>]*>(.*?)</{tag}>", text,
                                 re.S | re.I):
            base = text[:match.start(1)].count("\n")
            rows += [(base + n, body, kind) for n, body, kind
                     in comment_scan(match.group(1), marks, ("/*", "*/"))]
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

# SHARED END py-check-comment-scan


# SHARED BEGIN py-check-common

# Needs: ast, os, re, shutil, subprocess, sys, tempfile, CODEISH, COMMENTED,
# EXTERNAL_SCRIPTS, MAX_COLS, MAX_COMMENT_LINES, NON_ASCII, REPO, SCRIPT_PATH,
# TABULAR, WIDTH_BUDGET, py-check-files, py-check-comment-scan


def check_pyflakes():
    """Every script we own is clean under pyflakes."""
    if subprocess.run([sys.executable, "-m", "pyflakes", "--version"],
                      capture_output=True).returncode != 0:
        return ["SKIP pyflakes is not installed (pip install pyflakes)"]
    files = [os.path.join(REPO, p) for p in owned(".py")]
    r = subprocess.run([sys.executable, "-m", "pyflakes"] + files,
                       capture_output=True, text=True)
    return [line for line in r.stdout.splitlines() if line.strip()]


def check_compiles():
    """Every Python file we own parses, configurations included."""
    bad = []
    for rel in owned(".py"):
        try:
            ast.parse(read(rel))
        except SyntaxError as e:
            bad.append(f"{rel}:{e.lineno}: {e.msg}")
    return bad


def is_cli(rel):
    """A script with a command line. A gem5 configuration imports m5, runs
    only inside gem5 and cannot answer --help here."""
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
        # -B, so the modules a script imports leave no __pycache__ behind.
        r = subprocess.run([sys.executable, "-B", os.path.join(REPO, rel),
                            "--help"],
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


def heading_anchors(path):
    """The fragments GitHub gives a document's headings: lower case, anything
    but letters, digits, hyphens and spaces dropped, spaces as hyphens, and a
    repeated heading numbered from -1. Fenced code holds no headings."""
    found, seen, fence = set(), {}, False
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle.read().split("\n"):
            if line.lstrip().startswith("```"):
                fence = not fence
                continue
            heading = (None if fence
                       else re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line))
            if not heading:
                continue
            slug = re.sub(r"[^\w\- ]", "", heading.group(1).lower())
            slug = slug.replace(" ", "-")
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            found.add(slug if count == 0 else f"{slug}-{count}")
    return found


def check_script_names():
    """Every script named in our docs, Dockerfiles and scripts exists.

    Every quoted path to a script inside one of our scripts also resolves,
    which catches a path left stale by a script moving folder while its name
    alone would still match."""
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


def check_comments():
    """Comment prose: no semicolons, ASCII, and three lines to a comment.

    File headers, licences and tables are prose of another kind and are not
    held to the line limit. Docstrings are not comments, so module and
    function docstrings are exempt from it too. Every other comment over
    three lines fails, and names its file and line."""
    bad = []
    for rel in owned():
        if not (rel.endswith(COMMENTED)
                or os.path.basename(rel).startswith("Dockerfile")):
            continue
        text = read(rel)
        rows = comment_rows(rel, text)
        licensed = licence_lines(rows)
        # Where the file's own content starts. Everything above it introduces
        # the file rather than a line of code, however long the banner runs,
        # which a fixed line count would get wrong for the Dockerfiles.
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
            bad.append(f"{rel}:{start}: comment of {len(lines)} lines, over "
                       f"{MAX_COMMENT_LINES}")
    return bad


def check_formatting():
    """No trailing whitespace, a final newline, and no new over-long lines."""
    bad, wide = [], 0
    for rel in owned():
        name = os.path.basename(rel)
        if not (rel.endswith((".py", ".md", ".html", ".js", ".sv", ".c", ".h",
                              ".S", ".sh", ".cff", "Dockerfile"))
                or name.startswith("LICENSE")
                or name in (".dockerignore", ".gitignore")):
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

# SHARED END py-check-common


# SHARED BEGIN py-check-links

# Needs: os, re, CONTAINER_DOCS, REPO, py-check-files, py-check-common


def check_links():
    """Every relative link in our Markdown resolves, anchors included.

    The documents under CONTAINER_DOCS are skipped, since their paths are a
    container's own."""
    bad = []
    link = re.compile(r"\]\(([^)\s]+)\)")
    for rel in owned(".md"):
        if rel.startswith(CONTAINER_DOCS):
            continue
        base = os.path.dirname(os.path.join(REPO, rel))
        for target in link.findall(read(rel)):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path, _, anchor = target.partition("#")
            full = (os.path.join(base, path) if path
                    else os.path.join(REPO, rel))
            if not os.path.exists(full):
                bad.append(f"{rel}: {target}")
            elif (anchor and full.endswith(".md")
                    and anchor not in heading_anchors(full)):
                bad.append(f"{rel}: {target} names no heading there")
    return bad

# SHARED END py-check-links


# SHARED BEGIN py-check-shared-blocks

# Needs: difflib, hashlib, json, os, re, sys, REPO, py-check-files

# One fence syntax per language, each closing its comment, and a Python
# fence at column 0. A line that looks like a fence and fits none fails.
SHARED_FENCES = {
    ".html": (
        re.compile(r"^(?P<indent>\s*)/\* SHARED (?P<edge>BEGIN|END) "
                   r"(?P<name>\S+) \*/$"),
        re.compile(r"^(?P<indent>\s*)<!-- SHARED (?P<edge>BEGIN|END) "
                   r"(?P<name>\S+) -->$"),
    ),
    ".py": (
        re.compile(r"^(?P<indent>)# SHARED (?P<edge>BEGIN|END) "
                   r"(?P<name>\S+)$"),
    ),
}
SHARED_FENCE_LIKE = re.compile(r"\bSHARED (?:BEGIN|END)\b")
SHARED_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHARED_SUFFIXES = (".html", ".py")
SHARED_SIBLINGS = ("MinorFlow", "CVA6Flow", "FlowCompare.html")
# The fork's checker, which carries the check blocks, seen from a viewer
# inside the fork.
SHARED_FORK_CHECKER = os.path.join("scripts", "check_CVA6_repo.py")
SHARED_SKIP_DIRS = {".git", "docs", "tests", "node_modules", "__pycache__"}
SHARED_MANIFEST = os.path.join("scripts", "shared_blocks.json")
SHARED_MANIFEST_FORMAT = 1
SHARED_DIFF_LINES = 8


def shared_blocks_in(text, where):
    """The fenced blocks of one file as {name: (line, text)}, each text
    dedented to its fence, and the fence errors found."""
    blocks, errors, begun = {}, [], None
    fences = SHARED_FENCES[os.path.splitext(where)[1]]
    lines = text.split("\n")
    for number, line in enumerate(lines, 1):
        match = next((m for m in (f.match(line) for f in fences) if m), None)
        if not match:
            if SHARED_FENCE_LIKE.search(line):
                errors.append(f"{where}:{number}: a fence line that follows "
                              f"no fence syntax for this file")
            continue
        name, indent = match.group("name"), match.group("indent")
        if not SHARED_NAME.match(name):
            errors.append(f"{where}:{number}: {name} is not a block name")
        if match.group("edge") == "BEGIN":
            if begun:
                errors.append(f"{where}:{number}: {name} begins inside "
                              f"{begun[0]}")
            begun = (name, number, indent)
            continue
        if not begun or begun[0] != name:
            errors.append(f"{where}:{number}: {name} ends without a begin")
            begun = None
            continue
        start, begun = begun[1], None
        body = lines[start:number - 1]
        if len(body) < 2 or body[0].strip() or body[-1].strip():
            errors.append(f"{where}:{start}: {name} needs a blank line "
                          f"after its begin and before its end")
        rows = []
        for offset, row in enumerate(body):
            if row.strip() and not row.startswith(indent):
                errors.append(f"{where}:{start + offset + 1}: {name} has a "
                              f"line indented less than its fence")
            rows.append(row[len(indent):] if row.strip() else "")
        if name in blocks:
            errors.append(f"{where}:{start}: {name} appears twice")
        blocks[name] = (start, "\n".join(rows) + "\n")
    if begun:
        errors.append(f"{where}:{begun[1]}: {begun[0]} never ends")
    return blocks, errors


def shared_hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def sibling_files():
    """{absolute path: label} of the .html and .py files of the sibling
    trees: in viewers/ inside the fork, beside a viewer outside it, and the
    fork's checker above a viewer inside the fork."""
    here = os.path.realpath(REPO)
    mine = {os.path.realpath(os.path.join(REPO, rel)) for rel in owned()}
    found = {}
    for base in (os.path.join(REPO, "viewers"), os.path.dirname(here)):
        for sibling in SHARED_SIBLINGS:
            top = os.path.join(base, sibling)
            if not os.path.exists(top) or os.path.realpath(top) == here:
                continue
            paths = [top] if os.path.isfile(top) else []
            for root, dirs, files in os.walk(top):
                dirs[:] = sorted(d for d in dirs if d not in SHARED_SKIP_DIRS)
                paths += [os.path.join(root, f) for f in sorted(files)
                          if f.endswith(SHARED_SUFFIXES)]
            for path in paths:
                real = os.path.realpath(path)
                if real not in mine:
                    found[real] = os.path.relpath(path, base)
    fork = os.path.dirname(os.path.dirname(here))
    checker = os.path.join(fork, SHARED_FORK_CHECKER)
    if os.path.isfile(checker) and os.path.realpath(checker) not in mine:
        found[os.path.realpath(checker)] = SHARED_FORK_CHECKER
    return found


def collect_shared_blocks():
    """Own and sibling blocks as {name: [(where, line, text)]}, and every
    fence error found in either."""
    own, siblings, errors = {}, {}, []
    sources = [(rel, read(rel)) for rel in owned()
               if rel.endswith(SHARED_SUFFIXES)]
    for rel, text in sources:
        blocks, bad = shared_blocks_in(text, rel)
        errors += bad
        for name, (line, body) in blocks.items():
            own.setdefault(name, []).append((rel, line, body))
    for path, label in sibling_files().items():
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            continue
        blocks, bad = shared_blocks_in(text, label)
        errors += bad
        for name, (line, body) in blocks.items():
            siblings.setdefault(name, []).append((label, line, body))
    return own, siblings, errors


def shared_difference(name, first, second):
    """A failure naming both copies, with the start of their diff."""
    (where_a, line_a, text_a), (where_b, line_b, text_b) = first, second
    diff = [row for row in difflib.unified_diff(
        text_a.splitlines(), text_b.splitlines(), lineterm="", n=0)
        if row[:1] in "+-" and row[:3] not in ("+++", "---")]
    return ([f"{name}: {where_a}:{line_a} and {where_b}:{line_b} differ"]
            + [f"    {row}" for row in diff[:SHARED_DIFF_LINES]])


def check_shared_blocks():
    """Shared blocks match their other copies and the manifest."""
    own, siblings, bad = collect_shared_blocks()
    for name, copies in sorted(own.items()):
        for other in copies[1:] + siblings.get(name, []):
            if other[2] != copies[0][2]:
                bad += shared_difference(name, copies[0], other)
    manifest_path = os.path.join(REPO, SHARED_MANIFEST)
    if not os.path.isfile(manifest_path):
        return bad + [f"{SHARED_MANIFEST} is missing. Run "
                      f"--write-shared-manifest to create it."] if own else bad
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("format") != SHARED_MANIFEST_FORMAT:
        return bad + [f"{SHARED_MANIFEST} is format {manifest.get('format')}, "
                      f"and this check reads format "
                      f"{SHARED_MANIFEST_FORMAT}. Run --write-shared-manifest "
                      f"to rewrite it."]
    listed = manifest["blocks"]
    for name, copies in sorted(own.items()):
        if listed.get(name) != shared_hash(copies[0][2]):
            bad.append(f"{name}: changed since {SHARED_MANIFEST} was written. "
                       f"If the change is deliberate, make it in every "
                       f"repository that carries the block and run "
                       f"--write-shared-manifest in each.")
    bad += [f"{SHARED_MANIFEST} lists {name}, which no file here carries"
            for name in sorted(set(listed) - set(own))]
    return bad


def write_shared_manifest():
    """Rewrite the manifest from this repository's own blocks."""
    own, _siblings, errors = collect_shared_blocks()
    for name, copies in sorted(own.items()):
        for other in copies[1:]:
            if other[2] != copies[0][2]:
                errors += shared_difference(name, copies[0], other)
    if errors:
        for problem in errors:
            print(f"[ERROR] {problem}", file=sys.stderr)
        return 1
    blocks = {name: shared_hash(copies[0][2])
              for name, copies in sorted(own.items())}
    with open(os.path.join(REPO, SHARED_MANIFEST), "w") as handle:
        json.dump({"format": SHARED_MANIFEST_FORMAT, "blocks": blocks},
                  handle, indent=2)
        handle.write("\n")
    print(f"[INFO] Wrote {SHARED_MANIFEST} with {len(blocks)} blocks")
    return 0

# SHARED END py-check-shared-blocks


# SHARED BEGIN py-check-formatter

# Needs: os, subprocess, sys, FORMATTER_SCRIPT, REPO


def check_formatter():
    """Our Python, Markdown and benchmarks are what the formatter produces.

    Python is autopep8's at MAX_COLS. Markdown is Prettier's and is checked
    only where Prettier can run, since it is usually the editor's copy rather
    than a tool on PATH. The benchmarks are the formatter's own pass."""
    script = os.path.join(REPO, FORMATTER_SCRIPT)
    if not os.path.isfile(script):
        return [f"SKIP {FORMATTER_SCRIPT} is missing"]
    done = subprocess.run([sys.executable, script, "--check"],
                          capture_output=True, text=True, cwd=REPO)
    out = done.stdout
    # A half that could not run meaningfully, because autopep8 or Prettier is
    # missing or is not the toolchain the tree was formatted with, is a SKIP,
    # never a pass and never a list of files that are in fact formatted.
    skips = [line[len("[SKIP] "):] for line in out.splitlines()
             if line.startswith(("[SKIP] autopep8", "[SKIP] prettier"))]
    if done.returncode == 0:
        return [f"SKIP {skip}" for skip in skips]
    files = [ln.strip() for ln in out.splitlines() if ln.startswith("  ")]
    return [f"{f}: not what the formatter produces" for f in files] or [
        "some files are not what the formatter produces"]

# SHARED END py-check-formatter


CHECKS = (
    ("compiles", check_compiles),
    ("pyflakes", check_pyflakes),
    ("help", check_help),
    ("viewer-js", check_viewer_js),
    ("shared-blocks", check_shared_blocks),
    ("script-names", check_script_names),
    ("links", check_links),
    ("comments", check_comments),
    ("formatting", check_formatting),
    ("formatter", check_formatter),
)
OPTIONAL = ()

# What --help says this checker covers. It differs between the three
# repositories, as do the constants above and the checks outside the shared
# blocks.
DESCRIPTION = ("Check the CVA6Flow repository's own files. docs/old_versions "
               "and docs/parser_phases are frozen and skipped.")


# SHARED BEGIN py-check-main

# Needs: argparse, sys, CHECKS, OPTIONAL, DESCRIPTION, py-check-shared-blocks


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("-k", "--only", metavar="TEXT",
                        help="Run only the checks whose name contains TEXT")
    # OPTIONAL is empty in the viewers, which carry no gem5 patch, so this
    # option and the getattr below never apply there. They stay so that main
    # is one shared block in all three repositories.
    if OPTIONAL:
        parser.add_argument("--patch-roundtrip", action="store_true",
                            help="Also apply and revert MinorCPU_CVA6.patch "
                                 "against pristine gem5. Needs the network")
    parser.add_argument("--list", action="store_true",
                        help="Name the checks and stop")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Print only the checks that fail, and the "
                             "summary")
    parser.add_argument("--write-shared-manifest", action="store_true",
                        help="Rewrite scripts/shared_blocks.json from this "
                             "repository's shared blocks and stop")
    args = parser.parse_args()

    if args.write_shared_manifest:
        return write_shared_manifest()
    optional = getattr(args, "patch_roundtrip", False)
    checks = list(CHECKS) + (list(OPTIONAL) if optional else [])
    if args.only:
        checks = [c for c in checks if args.only in c[0]]
    if args.list:
        for name, function in list(CHECKS) + list(OPTIONAL):
            print(f"  {name:16} {(function.__doc__ or '').splitlines()[0]}")
        return 0
    if not checks:
        print(f"[ERROR] No check matches '{args.only}'", file=sys.stderr)
        return 2

    failed = skipped = 0
    for name, function in checks:
        try:
            problems = function()
        except Exception as e:  # so one raising check cannot stop the rest
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

# SHARED END py-check-main


if __name__ == "__main__":
    sys.exit(main())
