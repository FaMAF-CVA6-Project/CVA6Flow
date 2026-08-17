#!/usr/bin/env python3
"""
Run a CVA6 Verilator simulation and extract the metrics.
Accepts both C (.c) and assembly (.S/.s/.asm) tests. The input type is
detected from the extension and can be forced with --lang.
"""
import os
import re
import ast
import sys
import glob
import shlex
import shutil
import operator
import argparse
import datetime
import subprocess

# ==============================================================================
# OVERHEAD PROFILES (cv64a6_imafdc_sv39_hpdcache_wb)
# ==============================================================================
OVERHEAD_PROFILES = {
    "c": {
        'x18': 183,  # Cycles
        'x19': 32,   # Instructions
        'x20': 8,    # I-Cache misses
        'x21': 8,    # D-Cache misses
        'x22': 56,   # I-Cache accesses
        'x23': 32,   # D-Cache accesses
        'x24': 0,    # Branches
        'x25': 0,    # Branch mispredicts + unpredicted
        'x26': 3,    # Time (us)
    },
    "asm": {
        'x18': 40,   # Cycles
        'x19': 17,   # Instructions
        'x20': 3,    # I-Cache misses
        'x21': 0,    # D-Cache misses
        'x22': 34,   # I-Cache accesses
        'x23': 9,    # D-Cache accesses
        'x24': 0,    # Branches
        'x25': 0,    # Branch mispredicts + unpredicted
        'x26': 0,    # Time (us)
    },
}

# ==============================================================================
# CONFIGURATION
# ==============================================================================
METRICS_MAP = {
    'x18': 'Cycles',                # s2
    'x19': 'Instructions',          # s3
    'x20': 'I-Cache Misses',        # s4
    'x21': 'D-Cache Misses',        # s5
    'x22': 'I-Cache Accesses',      # s6
    'x23': 'D-Cache Accesses',      # s7
    'x24': 'Branches',              # s8
    'x25': 'Branch Miss + Unpred',  # s9
    'x26': 'Time (us)'              # s10
}

ORDERED_KEYS = ['x18', 'x19', 'x20', 'x21', 'x22', 'x23', 'x24', 'x25', 'x26']

# Folder next to this script where each run leaves a copy of the files worth
# keeping. The originals stay where the simulation puts them.
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "run_results")

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

# The _clean.txt holds two sections: the measured region of the disassembly,
# then the metrics table.
RULE = "=" * 70
METRICS_MARKER = "RESULTS TABLE"
CODE_BANNER = [RULE, "DISASSEMBLED CODE", RULE]
CODE_END_BANNER = [RULE, "END OF DISASSEMBLED CODE", RULE]


def format_cache_size(value):
    """Render a cache size as KiB/MiB from a byte count."""
    text = str(value).strip()
    if not text.isdigit():
        return text or "?"
    num = int(text)
    for unit, step in (("MiB", 1024 * 1024), ("KiB", 1024)):
        if num >= step and num % step == 0:
            return f"{num // step}{unit}"
    return f"{num}B"


def find_config_pkg(cva6_root, target):
    """Locate the target's SystemVerilog config package.

    core/include/ is where it lives, under the CVA6 root the simulation used or
    under this checkout. A target whose package sits elsewhere, or that is
    generated into another folder, is still worth finding, so the checkout is
    searched as a last resort. Returns None when there is no such package,
    which is the case for a target name that is not a package name."""
    name = f"{target}_config_pkg.sv"
    repo_root = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
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
    """Turn a SystemVerilog integer expression into a Python int.

    Covers a plain literal, a sized literal such as 32'd16384, and the small
    arithmetic a config package may use, such as 16 * 1024. An identifier or
    anything else is reported as unknown rather than guessed at. The tree is
    folded by hand instead of evaluated, so nothing in the file can run."""
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
    """Read the L1 geometry from the target's config package.

    Two forms are accepted, because packages use both: the field of the
    config struct the core is built from, whose value is either a literal or
    the name of a localparam declared above it, and the CVA6Config<field>
    localparam on its own. An unreadable package leaves the geometry unknown
    rather than failing the run."""
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
                value = localparam(token)
            if value is not None:
                return value
        # Packages that declare the parameter but do not spell the struct
        # field out the same way are still readable through the localparam.
        for name in (f"CVA6Config{field}", field):
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


def build_table_header(engine, core, program, geometry):
    """The table title, over two lines.

    What was measured goes on the first, and the core the model was built
    from on the second, since a target name is long enough to push the title
    well past the width of the table."""
    parts = [f"RESULTS TABLE {engine} {program}"]
    for name, label in (("icache", "ICache"), ("dcache", "DCache")):
        cache = geometry.get(name) or {}
        size = format_cache_size(cache.get("size") or "")
        assoc = cache.get("assoc") or "?"
        parts.append(f"{label}: {size}/{assoc}")
    return ["  ".join(parts), f"Core: {core}"]


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


def generate_and_show_codelist(binary_path, codelist):
    """
    Generate the .list file with objdump and print the filtered CODE section.
    Returns the path of the clean file for later writing.
    """
    if not os.path.exists(binary_path):
        print(f"[ERROR] Binary to disassemble not found: {binary_path}")
        return None

    list_path = os.path.splitext(binary_path)[0] + ".list"
    clean_path = os.path.splitext(binary_path)[0] + "_clean.txt"

    cmd = f"riscv64-unknown-elf-objdump -d -S -l {binary_path}"

    print(f"\n[INFO] Generating disassembled code in: {list_path}")
    try:
        with open(list_path, "w") as f:
            subprocess.run(shlex.split(cmd), stdout=f, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] {e}")
        return None
    except FileNotFoundError:
        print("[ERROR] 'riscv64-unknown-elf-objdump' not found")
        return None

    # Filtered view.
    print()
    for line in CODE_BANNER:
        print(line)
    print()

    def core_phrase(marker):
        return re.sub(r'\s+', ' ', marker.lstrip('#/ \t').strip())

    def as_list(v):
        return v if isinstance(v, (list, tuple)) else [v]

    start_cores = [core_phrase(m) for m in as_list(codelist["start"])]
    end_cores = [core_phrase(m) for m in as_list(codelist["end"])]

    def first_hit(line, phrases):
        norm = re.sub(r'\s+', ' ', line).strip()
        for p in phrases:
            if p and p in norm:
                return p
        return None

    printing = False
    found_start = False
    start_line_no = None
    end_line_no = None
    start_hit = None
    end_hit = None

    try:
        with open(list_path, "r") as f:
            lines = f.readlines()

        with open(clean_path, "w") as f_clean:
            f_clean.write("\n".join(CODE_BANNER) + "\n")
            written = "\n"
            for idx, line in enumerate(lines, 1):
                if printing:
                    eh = first_hit(line, end_cores)
                    if eh:
                        end_line_no = idx
                        end_hit = eh
                        printing = False
                        break

                if not found_start:
                    sh = first_hit(line, start_cores)
                    if sh and not first_hit(line, end_cores):
                        printing = True
                        found_start = True
                        start_line_no = idx
                        start_hit = sh
                        continue

                if printing:
                    if codelist["strip_dash_rule"] and re.search(r'#\s*-{5,}', line):
                        continue

                    if line.strip().startswith('/'):
                        if codelist["keep_discriminator"] and "(discriminator" in line:
                            print(line, end='')
                            f_clean.write(line)
                            written = line
                            continue
                        else:
                            continue

                    print(line, end='')
                    f_clean.write(line)
                    written = line

            if not written.endswith("\n"):
                f_clean.write("\n")
            f_clean.write("\n".join(CODE_END_BANNER) + "\n")

        if not found_start:
            print(f"[WARN] No start marker found (searched {start_cores!r}), "
                  f"so no program body was extracted. Check that the source "
                  f"uses one of these as a comment line.\n")
        elif end_line_no is None:
            print(f"[WARN] No end marker found after the start (searched "
                  f"{end_cores!r}). Printed through end of file.")

    except Exception as e:
        print(f"[ERROR] {e}")
        return None

    for line in CODE_END_BANNER:
        print(line)
    print()
    print(f"[INFO] Clean file saved in: {clean_path}")
    return clean_path


def collect_results(test_name, vcd_path, list_path, clean_path):
    """Copy the three files worth keeping into run_results/, next to this script.

    The VCD is what the viewer renders, the .list is the listing its tracer
    needs, and the _clean.txt is the measured region plus the metrics table.
    The originals are left where the simulation put them."""
    try:
        os.makedirs(RESULTS_DIR, exist_ok=True)
    except OSError as e:
        print(f"[WARN] Could not create {RESULTS_DIR}: {e}")
        return

    copied = []
    for source, name in ((vcd_path, f"{test_name}.vcd"),
                         (list_path, f"{test_name}.list"),
                         (clean_path, f"{test_name}_clean.txt")):
        # With --no-vcd there is no trace to copy, so a missing source here is
        # expected rather than a problem.
        if not source or not os.path.isfile(source):
            continue
        try:
            shutil.copy2(source, os.path.join(RESULTS_DIR, name))
            copied.append(name)
        except OSError as e:
            print(f"[WARN] Could not copy {source}: {e}")

    if copied:
        print(f"[INFO] Copied to {RESULTS_DIR}: {', '.join(copied)}")


def main():
    # Parse arguments
    parser = argparse.ArgumentParser(
        description="Run a CVA6 test (C or assembly) and extract metrics.")
    parser.add_argument("target",
                        help="Architecture target (e.g. cv64a6_imafdc_sv39_hpdcache)")
    parser.add_argument("src_file",
                        help="Path to the test: C (.c) or assembly (.S/.s/.asm), "
                             "relative or absolute")
    parser.add_argument("--lang", choices=["c", "asm"], default="auto",
                        help="Force the input type and overhead/filter profile. "
                             "Defaults to detection by extension.")
    parser.add_argument("--no-vcd", action="store_true",
                        help="Prevent the .vcd trace file from being generated")
    parser.add_argument("--keep-build", action="store_true",
                        help="Reuse the existing work-ver Verilator build "
                             "instead of deleting it first. The model does "
                             "not depend on the test, so this saves a full "
                             "rebuild per run. Use it only when the target "
                             "and the trace setting are unchanged since the "
                             "build was made.")
    args = parser.parse_args()

    # Directory configuration
    cva6_root = "/cva6"
    sim_dir = os.path.join(cva6_root, "verif/sim")
    setup_script = os.path.join(sim_dir, "setup-env.sh")

    # Verilator build folder. It is removed to force a full recompilation,
    # unless the caller asked to reuse it
    work_ver_path = os.path.join(cva6_root, "work-ver")

    if args.keep_build:
        if os.path.isdir(work_ver_path):
            print(f"[INFO] Reusing the Verilator build in {work_ver_path}")
    elif os.path.exists(work_ver_path):
        try:
            shutil.rmtree(work_ver_path)
        except OSError as e:
            print(f"[WARN] {e}")

    # Validate the source file exists
    abs_src_path = os.path.abspath(args.src_file)
    if not os.path.exists(abs_src_path):
        print(f"[ERROR] The file {abs_src_path} does not exist")
        sys.exit(1)

    lang = detect_lang(abs_src_path, args.lang)
    overhead = OVERHEAD_PROFILES[lang]
    codelist = CODELIST_PROFILES[lang]

    rel_src_path = os.path.relpath(abs_src_path, sim_dir)
    test_name = os.path.splitext(os.path.basename(abs_src_path))[0]

    # Prepare environment
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["DV_SIMULATORS"] = "veri-testharness"

    # Output paths
    today = datetime.date.today().strftime("%Y-%m-%d")
    log_dir_prediction = os.path.join(
        sim_dir, f"out_{today}", "veri-testharness_sim")
    binary_dir_compilation = os.path.join(
        sim_dir, f"out_{today}", "directed_tests")

    log_main = f"{test_name}.{args.target}.log"
    log_iss = f"{test_name}.{args.target}.log.iss"

    # Clean previous logs
    files_to_clean = [log_main, log_iss]
    for fname in files_to_clean:
        fpath = os.path.join(log_dir_prediction, fname)
        if os.path.exists(fpath):
            try:
                os.remove(fpath)
            except OSError:
                pass

    # VCD / TRACE_FAST flag handling
    if args.no_vcd:
        env["TRACE_FAST"] = ""
        env["TRACE_COMPACT"] = ""
        trace_injection = "export TRACE_FAST= && export TRACE_COMPACT= &&"
        print("[INFO] Trace file (.vcd/.fst) generation disabled.")
    else:
        env["TRACE_FAST"] = "1"
        trace_injection = "export TRACE_FAST=1 &&"

    # Build command. The only per-language difference is the test flag.
    test_flag = "--c_tests" if lang == "c" else "--asm_tests"
    py_cmd_list = [
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
    ]

    py_cmd_str = shlex.join(py_cmd_list)
    final_shell_cmd = f"source {setup_script} && {trace_injection} {py_cmd_str}"

    ext_label = "c" if lang == "c" else "S"
    try:
        print(
            f"[INFO] Running Verilator simulation with '{test_name}.{ext_label}'\n")
        subprocess.run(
            final_shell_cmd,
            cwd=sim_dir,
            check=True,
            env=env,
            stdout=subprocess.DEVNULL,
            shell=True,
            executable='/bin/bash'
        )
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Exit code: {e.returncode}")
        sys.exit(1)

    # --------------------------------------------------------------------------
    # GENERATE AND SHOW CODE
    # --------------------------------------------------------------------------
    binary_path = os.path.join(binary_dir_compilation, f"{test_name}.o")
    clean_path = generate_and_show_codelist(binary_path, codelist)

    # --------------------------------------------------------------------------
    # PARSE METRICS
    # --------------------------------------------------------------------------
    log_path = os.path.join(log_dir_prediction, log_main)

    if not os.path.exists(log_path):
        print(f"[ERROR] Simulation log not found: {log_path}")
        sys.exit(1)

    final_values = {}
    try:
        with open(log_path, 'r') as f:
            for line in f:
                # Match patterns: x<number> <hex>. If a register appears more
                # than once, the last occurrence is kept.
                match = re.search(r'x\s*(\d+)\s+(0x[0-9a-fA-F]+)', line)
                if match:
                    reg_key = f"x{match.group(1)}"
                    if reg_key in METRICS_MAP:
                        final_values[reg_key] = int(match.group(2), 16)
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    # --------------------------------------------------------------------------
    # CALCULATIONS AND PRINTING
    # --------------------------------------------------------------------------
    print("[INFO] Extracting statistics\n")

    clean_official = []
    clean_corrected = []

    # Pre-compute corrected IPC
    raw_inst = final_values.get('x19', 0)
    ovh_inst = overhead.get('x19', 0)
    net_inst = max(0, raw_inst - ovh_inst)

    raw_cycles = final_values.get('x18', 1)  # avoid div by 0
    ovh_cycles = overhead.get('x18', 0)
    net_cycles = max(1, raw_cycles - ovh_cycles)

    ipc_official = raw_inst / raw_cycles if raw_cycles > 0 else 0.0
    ipc_corrected = net_inst / net_cycles if net_cycles > 0 else 0.0

    geometry = read_cache_geometry(cva6_root, args.target)
    header = build_table_header("CVA6", args.target,
                                os.path.basename(abs_src_path), geometry)
    # The rule is widened when the title is longer, so the box never breaks.
    width = max(70, max(len(line) for line in header))

    output_buffer = []
    output_buffer.append("=" * width)
    output_buffer.extend(header)
    output_buffer.append("=" * width)
    output_buffer.append(f"{'METRIC':<25} | {'OFFICIAL':>15} | {'NET':>15}")
    output_buffer.append("=" * width)

    # Iterate metrics
    for key in ORDERED_KEYS:
        metric_name = METRICS_MAP[key]
        val_official = final_values.get(key, 0)
        ovh = overhead.get(key, 0)

        # Net value
        val_corrected = max(0, val_official - ovh)

        # Format and store
        clean_official.append(val_official)
        clean_corrected.append(val_corrected)

        output_buffer.append(
            f"{metric_name:<25} | {val_official:>15} | {val_corrected:>15}")

    # Print IPC
    output_buffer.append(
        f"{'IPC':<25} | {ipc_official:>15.4f} | {ipc_corrected:>15.4f}")

    # Add IPC to the clean lists
    clean_official.append(round(ipc_official, 4))
    clean_corrected.append(round(ipc_corrected, 4))

    output_buffer.append("=" * width)
    output_buffer.append(f"\nClean result (OFFICIAL):  {clean_official}")
    output_buffer.append(f"Clean result (NET):       {clean_corrected}\n")

    # Print everything to the terminal
    for line in output_buffer:
        print(line)

    # Append the same block to the _clean.txt file
    if clean_path and os.path.exists(clean_path):
        try:
            with open(clean_path, "a") as f_clean:
                f_clean.write("\n")
                for line in output_buffer:
                    f_clean.write(line + "\n")
            print(f"[INFO] Metrics successfully consolidated in: {clean_path}")
        except Exception as e:
            print(f"[WARN] Could not save the metrics to the file: {e}")

    # Done last, so the _clean.txt copied out already carries the table.
    collect_results(
        test_name,
        os.path.join(log_dir_prediction, f"{test_name}.{args.target}.vcd"),
        os.path.join(binary_dir_compilation, f"{test_name}.list"),
        clean_path)


if __name__ == "__main__":
    main()
