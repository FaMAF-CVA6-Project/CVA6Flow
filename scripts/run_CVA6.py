#!/usr/bin/env python3
"""Run a CVA6 Verilator simulation and consolidate the metrics.

Accepts both C (.c) and assembly (.S/.s/.asm/.sx) tests. The input type is
detected from the extension and can be forced with --lang. The overhead
table subtracted comes from the .overhead_suite beside the test, or --suite:

    python3 scripts/run_CVA6.py benchmarks/daxpy.S
"""
import argparse
import ast
import datetime
import glob
import operator
import os
import re
import shlex
import shutil
import subprocess
import sys

# The target the overhead tables below were measured on, and the one the
# calibration runs against.
DEFAULT_TARGET = "cv64a6_imafdc_sv39_hpdcache_wb"

# =============================================================================
# OVERHEAD PROFILES (DEFAULT_TARGET)
# =============================================================================
# Scaffolding around the measured region, subtracted to get NET. 'config' is
# the fork's gem5_config_CVA6/CVA6/benchmarks/, 'viewer' this repository's
# benchmarks/. Their templates differ, so the tables are not interchangeable.
OVERHEAD_SUITES = {
    "config": {
        "c": {
            'x18': 180,  # Cycles
            'x19': 33,   # Instructions
            'x20': 9,    # I-cache misses
            'x21': 8,    # D-cache misses
            'x22': 62,   # I-cache accesses
            'x23': 32,   # D-cache accesses
            'x24': 1,    # Branches
            'x25': 0,    # Mispredicts + unpredicted
        },
        "asm": {
            'x18': 40,   # Cycles
            'x19': 18,   # Instructions
            'x20': 4,    # I-cache misses
            'x21': 0,    # D-cache misses
            'x22': 40,   # I-cache accesses
            'x23': 9,    # D-cache accesses
            'x24': 1,    # Branches
            'x25': 0,    # Mispredicts + unpredicted
        },
    },
    "viewer": {
        "c": {
            'x18': 183,  # Cycles
            'x19': 32,   # Instructions
            'x20': 8,    # I-cache misses
            'x21': 8,    # D-cache misses
            'x22': 56,   # I-cache accesses
            'x23': 32,   # D-cache accesses
            'x24': 0,    # Branches
            'x25': 0,    # Mispredicts + unpredicted
        },
        "asm": {
            'x18': 40,   # Cycles
            'x19': 17,   # Instructions
            'x20': 3,    # I-cache misses
            'x21': 0,    # D-cache misses
            'x22': 34,   # I-cache accesses
            'x23': 9,    # D-cache accesses
            'x24': 0,    # Branches
            'x25': 0,    # Mispredicts + unpredicted
        },
    },
}


# A one-line file in a benchmark directory naming the overhead suite its
# programs belong to, so the suite travels with them into the Docker images.
# The gem5 driver uses the same marker and rules.
SUITE_MARKER = ".overhead_suite"


# SHARED BEGIN py-suite-marker

# Needs: os, SUITE_MARKER, OVERHEAD_SUITES


def read_suite_marker(src_file):
    """The suite declared beside the test, or None.

    Looks in the test's own directory and the two above it, so a benchmark in
    a subdirectory still finds its set's marker."""
    if not src_file:
        return None
    d = os.path.dirname(os.path.abspath(src_file))
    for _ in range(3):
        marker = os.path.join(d, SUITE_MARKER)
        if os.path.isfile(marker):
            try:
                with open(marker) as f:
                    name = f.read().strip()
            except OSError:
                return None
            if name in OVERHEAD_SUITES:
                return name
            print(f"[WARN] {marker} names '{name}', which is not one of "
                  f"{sorted(OVERHEAD_SUITES)}. Ignoring it.")
            return None
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None

# SHARED END py-suite-marker


def default_suite(src_file):
    """The suite the .overhead_suite beside the test names, or None after
    saying why. The suite decides what is subtracted from every reported
    cycle count, so it is never guessed from a path."""
    named = read_suite_marker(src_file)
    if named is None:
        choices = " or --suite ".join(sorted(OVERHEAD_SUITES))
        print(f"[ERROR] No {SUITE_MARKER} beside {src_file} or in the two "
              f"folders above it, so the overhead table to subtract is "
              f"unknown. Pass --suite {choices}, or add a {SUITE_MARKER} "
              f"file naming one beside the benchmarks.", file=sys.stderr)
    return named


# =============================================================================
# CONFIGURATION
# =============================================================================
METRICS_MAP = {
    'x18': 'Cycles',                # s2
    'x19': 'Instructions',          # s3
    'x20': 'I-cache misses',        # s4
    'x21': 'D-cache misses',        # s5
    'x22': 'I-cache accesses',      # s6
    'x23': 'D-cache accesses',      # s7
    'x24': 'Branches',              # s8
    'x25': 'Mispredicts + unpredicted',  # s9
    'x26': 'Time (us)'              # s10
}

ORDERED_KEYS = ['x18', 'x19', 'x20', 'x21', 'x22', 'x23', 'x24', 'x25', 'x26']

# Where each run leaves what is worth keeping, and where a surviving
# out_<date>/ is moved, both under the CVA6 root rather than beside this
# script, so everything a run writes is in one place.
RESULTS_DIR = os.path.join("results", "run")
VERIF_RESULTS_DIR = os.path.join("results", "verif")

CODELIST_PROFILES = {
    "c": {
        "start": ["// MAIN PROGRAM"],
        "end":   ["// FINAL SNAPSHOT", "// END OF MAIN PROGRAM"],
        "keep_discriminator": True,
        "strip_dash_rule": False,
    },
    "asm": {
        "start": ["# MAIN PROGRAM"],
        "end":   ["# FINAL SNAPSHOT", "# END OF MAIN PROGRAM"],
        "keep_discriminator": False,
        "strip_dash_rule": True,
    },
}

# Extensions recognised per input type (.S handled separately, case-sensitive).
C_EXTS = {".c"}
ASM_EXTS = {".s", ".asm", ".sx"}

# The _report.txt holds two sections: the measured region of the disassembly,
# then the metrics table.
RULE = "=" * 70
CODE_BANNER = [RULE, "DISASSEMBLED CODE", RULE]
CODE_END_BANNER = [RULE, "END OF DISASSEMBLED CODE", RULE]

# Lines of the simulation log echoed when a run fails. The whole of it stays
# on disk either way. This is only what the terminal is worth.
ERROR_TAIL_LINES = 40


def print_log_tail(path, lines=ERROR_TAIL_LINES):
    """Print the end of a log, which is where the cause of a failure is."""
    try:
        with open(path, errors="replace") as f:
            content = f.read().splitlines()
    except OSError as e:
        print(f"[WARN] Could not read {path}: {e}")
        return

    if not content:
        print("[ERROR] The simulation produced no output at all, so it "
              "failed before it started. Check the target and the "
              "environment.", file=sys.stderr)
        return

    shown = content[-lines:]
    if len(content) > len(shown):
        print(f"[ERROR] --- last {len(shown)} of {len(content)} log lines ---",
              file=sys.stderr)
    else:
        print(f"[ERROR] --- log ({len(content)} line(s)) ---", file=sys.stderr)
    for line in shown:
        print(f"  {line}", file=sys.stderr)


def format_cache_size(value):
    """Render a cache size as KiB or MiB, from a byte count."""
    text = str(value).strip()
    if not text.isdigit():
        return text or "?"
    num = int(text)
    for unit, step in (("MiB", 1024 * 1024), ("KiB", 1024)):
        if num >= step and num % step == 0:
            return f"{num // step}{unit}"
    return f"{num}B"


# How each test spells the clock it divides by, in assembly and in C.
FREQ_PATTERNS = (r"\.equ\s+CPU_FREQ\s*,\s*(\d+)",
                 r"#define\s+CPU_FREQ_HZ\s+(\d+)")


def read_cpu_freq(src_path):
    """The clock the test turns cycles into microseconds with. Every test
    carries it, '.equ CPU_FREQ' in assembly and '#define CPU_FREQ_HZ' in C, so
    the time here comes from the same constant the program divided by."""
    try:
        with open(src_path, errors="replace") as f:
            text = f.read()
    except OSError as e:
        print(f"[WARN] Could not read {src_path}: {e}. The time is reported "
              f"as the counter left it.")
        return None

    for pattern in FREQ_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))

    print(f"[WARN] No CPU_FREQ in {os.path.basename(src_path)}. The time is "
          f"reported as the counter left it.")
    return None


def format_metric(value, decimals=4):
    """Render a table value: thousands grouped, decimals only when it has any,
    so a count reads as 1,234,567 and an IPC as 0.8523 down the same column. A
    real number landing on a whole one drops the trailing zeros."""
    try:
        number = round(float(value), decimals)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.{decimals}f}"


def repo_checkout():
    """The CVA6 checkout this script sits in, found by walking up for
    verif/sim."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = here
    while True:
        if os.path.isdir(os.path.join(path, "verif", "sim")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return here
        path = parent


def find_config_pkg(cva6_root, target):
    """Locate the target's SystemVerilog config package under core/include/,
    in the CVA6 root or this checkout, falling back to a recursive search for a
    generated one. Returns None when the target names no package."""
    name = f"{target}_config_pkg.sv"
    repo_root = repo_checkout()
    roots = [cva6_root]
    if os.path.realpath(repo_root) != os.path.realpath(cva6_root):
        roots.append(repo_root)

    for root in roots:
        path = os.path.join(root, "core", "include", name)
        if os.path.isfile(path):
            return path

    for root in roots:
        for path in sorted(glob.glob(os.path.join(root, "**", name),
                                     recursive=True)):
            return path

    known = {os.path.basename(p)[:-len("_config_pkg.sv")]
             for root in roots
             for p in glob.glob(os.path.join(root, "core", "include",
                                             "*_config_pkg.sv"))}
    known.discard("build")              # build_config_pkg.sv is not a target
    print(f"[WARN] No config package '{name}' found under "
          f"{' or '.join(roots)}. The cache geometry is reported as '?'.")
    if known:
        print("[WARN] Targets with a package here: " +
              ", ".join(sorted(known)))
    return None


_FOLD_OPS = {ast.Mult: operator.mul, ast.Add: operator.add,
             ast.Sub: operator.sub, ast.LShift: operator.lshift,
             ast.Div: operator.floordiv, ast.FloorDiv: operator.floordiv}


def _fold(node):
    """Constant-fold an arithmetic tree, or None if it is not one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _FOLD_OPS:
        left, right = _fold(node.left), _fold(node.right)
        if left is not None and right is not None:
            return _FOLD_OPS[type(node.op)](left, right)
    return None


def sv_int(text):
    """Turn a SystemVerilog integer expression into a Python int: a plain or
    sized literal and the small arithmetic a config package uses. The tree is
    folded by hand rather than evaluated, so nothing in the file can run."""
    text = text.strip().rstrip(";").strip()
    if not text:
        return None

    sized = re.fullmatch(r"(?:\d+)?\s*'\s*[sS]?([bodhBODH])([0-9a-fA-F_]+)",
                         text)
    if sized:
        base = {"b": 2, "o": 8, "d": 10, "h": 16}[sized.group(1).lower()]
        try:
            return int(sized.group(2).replace("_", ""), base)
        except ValueError:
            return None

    try:
        return _fold(ast.parse(text, mode="eval").body)
    except (SyntaxError, ValueError, TypeError):
        return None


def read_cache_geometry(cva6_root, target):
    """Read the L1 geometry from the target's config package. Both forms are
    accepted, the config struct's field, literal or naming a localparam, and
    the CVA6Config<field> localparam on its own."""
    path = find_config_pkg(cva6_root, target)
    if not path:
        return {}
    try:
        with open(path) as f:
            text = f.read()
    except OSError as e:
        print(f"[WARN] Could not read {path}: {e}")
        return {}

    def localparam(name):
        """The value of a localparam, whatever type qualifiers it carries."""
        match = re.search(rf"localparam\b[^=;\n]*?\b{re.escape(name)}\s*=\s*"
                          rf"([^;]+);", text)
        return sv_int(match.group(1)) if match else None

    def selected_config():
        """What a swept package selected, or None when it sweeps nothing."""
        match = re.search(r"localparam\s+int\s+CVA6_CONFIG_SEL\s*=\s*(\w+)",
                          text)
        return match.group(1) if match else None

    chosen = selected_config()

    def swept(name):
        """The value a conditional localparam takes under the selection. A
        swept package writes the parameter as a chain of 'SEL == CFG_X ? value'
        terms ending in the unswept default, so the first term naming the
        selection is the one the build used."""
        match = re.search(rf"localparam\b[^=;\n]*?\b{re.escape(name)}\s*=\s*"
                          rf"([^;]+);", text)
        if not match or "?" not in match.group(1):
            return None
        body = match.group(1)
        if chosen:
            for condition, value in re.findall(r"\(([^?]*)\)\s*\?\s*(\w+)",
                                               body):
                if re.search(rf"\b{re.escape(chosen)}\b", condition):
                    found = sv_int(value)
                    return found if found is not None else localparam(value)
        # Nothing selected it, so the chain falls through to its last term.
        tail = body.rsplit(":", 1)[-1].strip()
        found = sv_int(tail)
        return found if found is not None else localparam(tail)

    def resolve(field):
        match = re.search(rf"\b{field}\s*:\s*([^,\n]+)", text)
        if match:
            # Drop an 'unsigned'(...)' style cast, then the parentheses, to
            # leave either a literal or the name of a localparam.
            token = re.sub(r"^\w+\s*'\s*(?=\()", "", match.group(1).strip())
            # Several fields may share a line, so the tail of the struct can
            # come along with the value. Trim the punctuation off both ends.
            token = token.strip("(){}; \t")
            value = sv_int(token)
            if value is None:
                value = swept(token)
            if value is None:
                value = localparam(token)
            if value is not None:
                return value
        # Packages that declare the parameter but do not spell the struct
        # field out the same way are still readable through the localparam.
        for name in (f"CVA6Config{field}", field):
            value = swept(name)
            if value is None:
                value = localparam(name)
            if value is not None:
                return value
        return None

    geometry = {}
    missing = []
    for name, prefix in (("icache", "Icache"), ("dcache", "Dcache")):
        fields = {"size": f"{prefix}ByteSize", "assoc": f"{prefix}SetAssoc"}
        geometry[name] = {key: resolve(field)
                          for key, field in fields.items()}
        missing += [field for key, field in fields.items()
                    if geometry[name][key] is None]
    if missing:
        print(f"[WARN] Could not resolve {', '.join(missing)} in {path}")
    return geometry


def build_table_header(engine, core, program, geometry, build=""):
    """The table title, over three lines. What was measured goes on the first,
    the core on the second and the checkout on the third, a target name being
    long enough to push a single title past the width of the table.
    """
    parts = [f"RESULTS TABLE {engine} {program}"]
    for name, label in (("icache", "I-cache"), ("dcache", "D-cache")):
        cache = geometry.get(name) or {}
        size = format_cache_size(cache.get("size") or "")
        assoc = cache.get("assoc") or "?"
        parts.append(f"{label}: {size}/{assoc}")
    lines = ["  ".join(parts), f"Core: {core}"]
    if build:
        lines.append(f"Build: {build}")
    return lines


def resolve_out_dir(sim_dir, predicted, log_rel):
    """The out_<date>/ folder holding this run's log.

    cva6.py names the folder from the date at its own start, and a Verilator
    build takes long enough to cross midnight, so the date computed before the
    run is a prediction rather than a fact.
    """
    if os.path.isfile(os.path.join(sim_dir, predicted, log_rel)):
        return predicted

    candidates = sorted(
        (d for d in glob.glob(os.path.join(sim_dir, "out_*"))
         if os.path.isfile(os.path.join(d, log_rel))),
        key=os.path.getmtime, reverse=True)
    if not candidates:
        return predicted

    found = os.path.basename(candidates[0])
    print(f"[WARN] No log in {predicted}/, using {found}/ instead. A run that "
          f"crosses midnight lands in the next day's folder.")
    return found


def detect_lang(src_file, override):
    """Decide whether the input is C or assembly."""
    if override in ("c", "asm"):
        return override
    _, ext = os.path.splitext(src_file)
    if ext == ".S":
        return "asm"
    low = ext.lower()
    if low in C_EXTS:
        return "c"
    if low in ASM_EXTS:
        return "asm"
    print(f"[WARN] Unrecognised extension '{ext}'. Assuming C. "
          f"Use --lang c|asm to force.")
    return "c"


# The toolchain prefix depends on the checkout: the CVA6 flow's own builder
# makes riscv-none-elf, apt's bare-metal package riscv64-unknown-elf. Both
# read the same ELF, so the first one present wins.
OBJDUMPS = ("riscv-none-elf-objdump", "riscv64-unknown-elf-objdump")


def find_objdump():
    """The disassembler this checkout has, on PATH or under $RISCV."""
    riscv = os.environ.get("RISCV")
    for name in OBJDUMPS:
        found = shutil.which(name)
        if found:
            return found
        if riscv:
            candidate = os.path.join(riscv, "bin", name)
            if os.path.isfile(candidate):
                return candidate
    return None


def write_listing(binary_path, list_path):
    """objdump -d -S -l of the binary into list_path. True on success."""
    objdump = find_objdump()
    if objdump is None:
        print("[ERROR] No RISC-V objdump found. Tried: " + ", ".join(OBJDUMPS),
              file=sys.stderr)
        return False
    print(f"\n[INFO] Generating disassembled code in: {list_path}")
    try:
        with open(list_path, "w") as f:
            subprocess.run([objdump, "-d", "-S", "-l", binary_path],
                           stdout=f, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return False
    except OSError as e:
        print(f"[ERROR] Could not run {objdump}: {e}", file=sys.stderr)
        return False
    return True


def marker_phrases(markers):
    """The words of each marker comment, without its comment characters."""
    if not isinstance(markers, (list, tuple)):
        markers = [markers]
    return [re.sub(r"\s+", " ", m.lstrip("#/ \t").strip()) for m in markers]


def first_hit(line, phrases):
    norm = re.sub(r"\s+", " ", line).strip()
    for phrase in phrases:
        if phrase and phrase in norm:
            return phrase
    return None


def region_lines(lines, codelist):
    """The listing lines between the start and end markers, as the report
    keeps them, and whether each marker was found."""
    start_phrases = marker_phrases(codelist["start"])
    end_phrases = marker_phrases(codelist["end"])
    kept = []
    printing = found_start = found_end = False
    for line in lines:
        if printing and first_hit(line, end_phrases):
            found_end = True
            break
        if not found_start:
            if (first_hit(line, start_phrases)
                    and not first_hit(line, end_phrases)):
                printing = found_start = True
            continue
        if (codelist["strip_dash_rule"]
                and re.search(r"#\s*-{5,}", line)):
            continue
        # Source lines objdump -S interleaves start with a path. A C test
        # keeps the discriminator lines, which say which loop a block is.
        if line.strip().startswith("/") and not (
                codelist["keep_discriminator"] and "(discriminator" in line):
            continue
        kept.append(line)
    return kept, found_start, found_end


def generate_codelist(binary_path, codelist):
    """Write the .list with objdump and the measured region of it to the
    _report.txt. Returns the report path, or None on failure."""
    if not os.path.exists(binary_path):
        print(f"[ERROR] Binary to disassemble not found: {binary_path}",
              file=sys.stderr)
        return None
    list_path = os.path.splitext(binary_path)[0] + ".list"
    report_path = os.path.splitext(binary_path)[0] + "_report.txt"
    if not write_listing(binary_path, list_path):
        return None
    try:
        with open(list_path) as f:
            kept, found_start, found_end = region_lines(f.readlines(),
                                                        codelist)
        with open(report_path, "w") as report:
            report.write("\n".join(CODE_BANNER) + "\n")
            report.writelines(kept)
            if kept and not kept[-1].endswith("\n"):
                report.write("\n")
            report.write("\n".join(CODE_END_BANNER) + "\n")
    except OSError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return None
    if not found_start:
        print(f"[WARN] No start marker found (searched "
              f"{marker_phrases(codelist['start'])!r}), so no program body "
              f"was extracted. Check that the source uses one of these as a "
              f"comment line.")
    elif not found_end:
        print(f"[WARN] No end marker found after the start (searched "
              f"{marker_phrases(codelist['end'])!r}). Written through the end "
              f"of the file.")
    print(f"[INFO] Disassembly ({len(kept)} lines) saved in: {report_path}")
    return report_path


def keep_sim_output(sim_dir, out_name, base):
    """Move a surviving out_<date>/ under results/.

    Only if it is still there. The verif flow and the cleaners both delete
    it, and a run that stopped early leaves nothing worth keeping."""
    source = os.path.join(sim_dir, out_name)
    if not os.path.isdir(source):
        return
    target = os.path.join(base, VERIF_RESULTS_DIR, out_name)
    moved = 0
    try:
        # Merged into whatever is already there, overwriting a file of the
        # same name, which is what out_<date>/ did when the flow wrote into
        # it directly. A batch's runs then gather in one folder per day.
        for root, _, files in os.walk(source):
            rel = os.path.relpath(root, source)
            into = target if rel == os.curdir else os.path.join(target, rel)
            os.makedirs(into, exist_ok=True)
            for name in files:
                os.replace(os.path.join(root, name),
                           os.path.join(into, name))
                moved += 1
        shutil.rmtree(source, ignore_errors=True)
    except OSError as e:
        print(f"[WARN] Could not move {source}: {e}")
        return
    print(f"[INFO] Moved {moved} file(s) from {source} to {target}")


def collect_results(test_name, vcd_path, list_path, report_path, base):
    """Gather the three files worth keeping in results/run/. The VCD is what
    the tracer reads, the .list the listing it needs, and the _report.txt
    the measured region plus the metrics table. The VCD is moved, not copied,
    since it can run to tens of GiB and a copy would need that much again."""
    results_dir = os.path.join(base, RESULTS_DIR)
    try:
        os.makedirs(results_dir, exist_ok=True)
    except OSError as e:
        print(f"[WARN] Could not create {results_dir}: {e}")
        return

    moved, copied = [], []
    for source, name, move in ((vcd_path, f"{test_name}.vcd", True),
                               (list_path, f"{test_name}.list", False),
                               (report_path, f"{test_name}_report.txt",
                                False)):
        # With --no-vcd there is no VCD to gather, so a missing source here is
        # expected rather than a problem.
        if not source or not os.path.isfile(source):
            continue
        target = os.path.join(results_dir, name)
        try:
            if move:
                shutil.move(source, target)
                moved.append(name)
            else:
                shutil.copy2(source, target)
                copied.append(name)
        except OSError as e:
            print(f"[WARN] Could not gather {source}: {e}")

    if moved:
        print(f"[INFO] Moved to {results_dir}: {', '.join(moved)}")
    if copied:
        print(f"[INFO] Copied to {results_dir}: {', '.join(copied)}")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run a CVA6 test (C or assembly) and extract metrics.")
    parser.add_argument("target", nargs="?", default=DEFAULT_TARGET,
                        help=f"Architecture target. Defaults to "
                             f"{DEFAULT_TARGET}, the one the overhead tables "
                             f"were measured on")
    parser.add_argument("src_file",
                        help="Path to the test: C (.c) or assembly "
                             "(.S/.s/.asm/.sx), relative or absolute")
    parser.add_argument("--cva6-root", default=None, metavar="DIR",
                        help="The CVA6 checkout to run: the one holding "
                             "verif/sim. Defaults to /CVA6 inside the "
                             "container, or the repository this script sits "
                             "in when that does not exist")
    parser.add_argument("--suite", choices=sorted(OVERHEAD_SUITES),
                        default=None,
                        help=f"Which overhead table to subtract. 'config' is "
                             f"the calibration benchmarks, 'viewer' the "
                             f"CVA6Flow development set. Defaults to the "
                             f"{SUITE_MARKER} file beside the test or up to "
                             f"two folders above it, and the run stops "
                             f"without either")
    parser.add_argument("--lang", choices=["auto", "c", "asm"],
                        default="auto",
                        help="Force the input type, which selects both the "
                             "overhead profile and the disassembly markers. "
                             "Defaults to auto, detection by extension")
    parser.add_argument("--no-keep-sim-output", action="store_true",
                        help="Leave out_<date>/ in verif/sim instead of "
                             "moving it under results/verif/ at the end. The "
                             "batch and the sweep pass this, since they "
                             "discard the tree themselves")
    parser.add_argument("--no-vcd", action="store_true",
                        help="Do not write the VCD, and report metrics only")
    parser.add_argument("--keep-build", action="store_true",
                        help="Reuse the existing work-ver Verilator build "
                             "instead of deleting it first. The model does "
                             "not depend on the test, so this saves a full "
                             "rebuild per run. Use it only when the target "
                             "and the VCD setting are unchanged since the "
                             "build was made")
    return parser


def prepare_build(cva6_root, keep_build):
    """Delete the Verilator build folder, forcing a full recompilation,
    unless the caller asked to reuse it."""
    work_ver_path = os.path.join(cva6_root, "work-ver")
    if keep_build:
        if os.path.isdir(work_ver_path):
            print(f"[INFO] Reusing the Verilator build in {work_ver_path}")
    elif os.path.exists(work_ver_path):
        try:
            shutil.rmtree(work_ver_path)
        except OSError as e:
            print(f"[WARN] {e}")


def simulation_command(args, lang, rel_src_path, setup_script, env):
    """The bash command that sources the flow's environment and runs cva6.py
    on the test. The test flag is the only per-language difference."""
    if args.no_vcd:
        # Both emptied, so cva6.py writes neither a .vcd nor an .fst.
        env["TRACE_FAST"] = ""
        env["TRACE_COMPACT"] = ""
        trace_injection = "export TRACE_FAST= && export TRACE_COMPACT= &&"
        print("[INFO] VCD (.vcd/.fst) generation disabled.")
    else:
        env["TRACE_FAST"] = "1"
        trace_injection = "export TRACE_FAST=1 &&"
    test_flag = "--c_tests" if lang == "c" else "--asm_tests"
    py_cmd = shlex.join([
        "python3", "cva6.py",
        "--target", args.target,
        f"--iss={env['DV_SIMULATORS']}",
        "--iss_yaml=cva6.yaml",
        test_flag, rel_src_path,
        "--linker=../../config/gen_from_riscv_config/linker/link.ld",
        "--gcc_opts=-static -mcmodel=medany -fvisibility=hidden -nostdlib "
        "-nostartfiles -g ../tests/custom/common/syscalls.c "
        "../tests/custom/common/crt.S -lgcc -I../tests/custom/env "
        "-I../tests/custom/common"
    ])
    return f"source {shlex.quote(setup_script)} && {trace_injection} {py_cmd}"


def run_simulation(command, sim_dir, sim_out_dir, run_log, env):
    """Run the simulation with both streams in run_log, printing its tail
    when it fails. True on success."""
    try:
        os.makedirs(sim_out_dir, exist_ok=True)
        with open(run_log, "w") as log:
            subprocess.run(command, cwd=sim_dir, check=True, env=env,
                           stdout=log, stderr=subprocess.STDOUT, shell=True,
                           executable="/bin/bash")
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] The simulation failed with exit code {e.returncode}",
              file=sys.stderr)
        print_log_tail(run_log)
        print(f"[ERROR] Full output: {run_log}", file=sys.stderr)
        return False
    except OSError as e:
        print(f"[ERROR] Could not run the simulation: {e}", file=sys.stderr)
        return False
    return True


def read_register_values(log_path):
    """The metric registers the test left in the simulation log, the last
    occurrence of each, or None when the log cannot be read."""
    values = {}
    try:
        with open(log_path) as f:
            for line in f:
                match = re.search(r"x\s*(\d+)\s+(0x[0-9a-fA-F]+)", line)
                if match and f"x{match.group(1)}" in METRICS_MAP:
                    values[f"x{match.group(1)}"] = int(match.group(2), 16)
    except OSError as e:
        print(f"[ERROR] Could not read {log_path}: {e}", file=sys.stderr)
        return None
    return values


def metrics_table(values, overhead, src_path, header):
    """The boxed metrics table as lines, OFFICIAL and NET side by side, with
    the clean result lists under it."""
    raw_cycles = values.get("x18", 0)
    net_cycles = max(0, raw_cycles - overhead.get("x18", 0))
    raw_inst = values.get("x19", 0)
    net_inst = max(0, raw_inst - overhead.get("x19", 0))
    ipc_official = raw_inst / raw_cycles if raw_cycles else 0.0
    ipc_corrected = net_inst / net_cycles if net_cycles else 0.0

    # The test computes Time (us) as cycles * 1e6 / CPU_FREQ with an integer
    # divide, so x26 arrives with the fraction cut off. The same division
    # here, from the same constant, keeps it.
    cpu_freq = read_cpu_freq(src_path)
    time_us = net_time_us = None
    if cpu_freq:
        time_us = raw_cycles * 1_000_000 / cpu_freq
        counter = values.get("x26", 0)
        if counter and int(time_us) != int(counter):
            print(f"[WARN] Time from cycles ({int(time_us)} us) disagrees "
                  f"with the counter ({int(counter)} us). The counter is "
                  f"reported. Check how the test computes it.")
            time_us = None
        else:
            # The net time is the net cycles read through the same clock.
            net_time_us = net_cycles * 1_000_000 / cpu_freq

    width = max(70, max(len(line) for line in header))
    lines = ["=" * width, *header, "=" * width,
             f"{'METRIC':<25} | {'OFFICIAL':>15} | {'NET':>15}", "=" * width]
    clean_official, clean_corrected = [], []
    for key in ORDERED_KEYS:
        official = values.get(key, 0)
        corrected = max(0, official - overhead.get(key, 0))
        if key == "x26":
            if time_us is not None:
                official, corrected = time_us, net_time_us
            else:
                corrected = (official * net_cycles / raw_cycles
                             if raw_cycles else 0)
        # round() leaves a count alone and only bites on the one metric that
        # carries a fraction.
        clean_official.append(round(official, 4))
        clean_corrected.append(round(corrected, 4))
        lines.append(f"{METRICS_MAP[key]:<25} | "
                     f"{format_metric(official):>15} | "
                     f"{format_metric(corrected):>15}")
    lines.append(f"{'IPC':<25} | {format_metric(ipc_official):>15} | "
                 f"{format_metric(ipc_corrected):>15}")
    clean_official.append(round(ipc_official, 4))
    clean_corrected.append(round(ipc_corrected, 4))
    lines.append("=" * width)
    lines.append(f"\nClean result (OFFICIAL):  {clean_official}")
    lines.append(f"Clean result (NET):       {clean_corrected}\n")
    return lines


def append_to_report(report_path, lines):
    if not report_path or not os.path.exists(report_path):
        return
    try:
        with open(report_path, "a") as report:
            report.write("\n")
            for line in lines:
                report.write(line + "\n")
    except OSError as e:
        print(f"[WARN] Could not save the metrics to the file: {e}")
        return
    print(f"[INFO] Metrics successfully consolidated in: {report_path}")


def main():
    args = build_parser().parse_args()
    cva6_root = args.cva6_root or ("/CVA6" if os.path.isdir("/CVA6")
                                   else repo_checkout())
    if not os.path.isdir(os.path.join(cva6_root, "verif", "sim")):
        print(f"[ERROR] '{cva6_root}' does not look like a CVA6 checkout: no "
              f"verif/sim inside it. Point --cva6-root at one.",
              file=sys.stderr)
        return 1
    print(f"[INFO] CVA6 root: {cva6_root}")
    sim_dir = os.path.join(cva6_root, "verif", "sim")
    setup_script = os.path.join(sim_dir, "setup-env.sh")

    abs_src_path = os.path.abspath(args.src_file)
    if not os.path.exists(abs_src_path):
        print(f"[ERROR] Test not found: {abs_src_path}", file=sys.stderr)
        pkg = os.path.join(cva6_root, "core", "include",
                           f"{args.src_file}_config_pkg.sv")
        if os.path.isfile(pkg):
            print(f"[ERROR] '{args.src_file}' is a target, not a test. The "
                  f"test is the last argument, and the target before it can "
                  f"be left out to get {DEFAULT_TARGET}.", file=sys.stderr)
        return 1
    # Resolved after parsing rather than as an argparse default, since it
    # reads the test's path.
    suite = args.suite or default_suite(abs_src_path)
    if suite is None:
        return 1
    prepare_build(cva6_root, args.keep_build)

    lang = detect_lang(abs_src_path, args.lang)
    overhead = OVERHEAD_SUITES[suite][lang]
    print(f"[INFO] Overhead table: {suite}/{lang}")
    rel_src_path = os.path.relpath(abs_src_path, sim_dir)
    test_name = os.path.splitext(os.path.basename(abs_src_path))[0]

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # cva6.py's stdout is block-buffered once it goes to a file, which would
    # land its output in the log well after the stderr it belongs next to.
    env["PYTHONUNBUFFERED"] = "1"
    env["DV_SIMULATORS"] = "veri-testharness"

    today = datetime.date.today().strftime("%Y-%m-%d")
    log_main = f"{test_name}.{args.target}.log"
    # A stale log from an earlier run today would otherwise be parsed if this
    # run fails before writing its own.
    for name in (log_main, log_main + ".iss"):
        stale = os.path.join(sim_dir, f"out_{today}", "veri-testharness_sim",
                             name)
        if os.path.exists(stale):
            try:
                os.remove(stale)
            except OSError:
                pass

    # Both streams go to a log under out_<date>/ rather than the terminal,
    # and its tail is printed when the run fails. Merged, so an error sits
    # next to the step it interrupted.
    sim_out_dir = os.path.join(sim_dir, f"out_{today}")
    run_log = os.path.join(sim_out_dir, f"{test_name}_run.log")
    command = simulation_command(args, lang, rel_src_path, setup_script, env)
    print(f"[INFO] Running Verilator simulation with "
          f"'{os.path.basename(abs_src_path)}'")
    print(f"[INFO] The build is quiet. Everything it writes goes to "
          f"{run_log}\n")
    if not run_simulation(command, sim_dir, sim_out_dir, run_log, env):
        return 1

    # The run is over, so the folder it actually wrote to is known.
    out_name = resolve_out_dir(
        sim_dir, f"out_{today}",
        os.path.join("veri-testharness_sim", log_main))
    log_dir = os.path.join(sim_dir, out_name, "veri-testharness_sim")
    binary_dir = os.path.join(sim_dir, out_name, "directed_tests")

    report_path = generate_codelist(
        os.path.join(binary_dir, f"{test_name}.o"), CODELIST_PROFILES[lang])
    log_path = os.path.join(log_dir, log_main)
    if not os.path.exists(log_path):
        print(f"[ERROR] Simulation log not found: {log_path}",
              file=sys.stderr)
        return 1
    values = read_register_values(log_path)
    if values is None:
        return 1

    print("[INFO] Extracting statistics\n")
    geometry = read_cache_geometry(cva6_root, args.target)
    header = build_table_header(
        "CVA6", args.target, os.path.basename(abs_src_path), geometry,
        f"{cva6_root}  (overhead: {suite}/{lang})")
    lines = metrics_table(values, overhead, abs_src_path, header)
    for line in lines:
        print(line)
    append_to_report(report_path, lines)

    # Done last, so the _report.txt copied out already carries the table.
    collect_results(
        test_name, os.path.join(log_dir, f"{test_name}.{args.target}.vcd"),
        os.path.join(binary_dir, f"{test_name}.list"), report_path,
        cva6_root)
    # Last of all, since the files above are gathered from inside it.
    if not args.no_keep_sim_output:
        keep_sim_output(sim_dir, out_name, cva6_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
