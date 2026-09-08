#!/usr/bin/env python3
"""Run every benchmark in a folder through run_CVA6.py, dropping the
templates and printing a pass/fail summary. Only the first test pays for the
Verilator build, the rest reuse it. --rebuild-each rebuilds every time.
"""
import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import time

# ==============================================================================
# CONFIGURATION
# ==============================================================================
# Default folder, as laid out inside the manuel313/cva6 image.
DEFAULT_TESTS_DIR = "/cva6/benchmarks"

# The driver this script delegates to, looked up next to it and then in cwd.
RUNNER_NAME = "run_CVA6.py"

# Matches run_CVA6.py's own default, the target the overhead tables were
# measured on.
DEFAULT_TARGET = "cv64a6_imafdc_sv39_hpdcache_wb"

CVA6_ROOT = "/cva6"

# Where the batch gathers what it keeps, one folder for the whole run.
DEFAULT_OUT_DIR = "batch_results"

# Recognised test extensions, matching run_CVA6.py. Case-sensitive: .S
# is assembly and .s is too, but .c is the only C spelling accepted.
SOURCE_EXTS = {".c", ".S", ".s", ".asm", ".sx"}

# A file whose name contains this is a starting point, not a benchmark.
TEMPLATE_MARKER = "template"

# What run_CVA6.py writes above its metrics table, and where the batch
# gathers every one of those tables once the runs are done.
METRICS_MARKER = "RESULTS TABLE"

SEP = "=" * 70


def slug(text, limit=40):
    """Turn a value into something safe for a file name: word characters and
    single dashes, trimmed."""
    out = re.sub(r"[^A-Za-z0-9]+", "-", str(text)).strip("-")
    return out[:limit].strip("-")


def metrics_filename(parts):
    """The gathered metrics file, named after the run that produced it.

    A batch used to write a bare metrics.txt, so two runs of different targets
    landed on the same name and nothing inside the file told them apart.
    """
    tags = [slug(p) for p in parts if p]
    return "metrics" + ("_" if tags else "") + "_".join(tags) + ".txt"


def find_runner():
    """Locate run_CVA6.py next to this script, then in the cwd."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, RUNNER_NAME),
                      os.path.abspath(RUNNER_NAME)):
        if os.path.isfile(candidate):
            return candidate
    print(f"[ERROR] {RUNNER_NAME} not found next to this script or in the "
          f"current directory.")
    sys.exit(2)


def discover(folder, recursive):
    """Return (tests, templates), both sorted lists of paths."""
    found = []
    if recursive:
        for root, _, names in os.walk(folder):
            found.extend(os.path.join(root, n) for n in names)
    else:
        found.extend(os.path.join(folder, n) for n in os.listdir(folder)
                     if os.path.isfile(os.path.join(folder, n)))

    sources = sorted(p for p in found
                     if os.path.splitext(p)[1] in SOURCE_EXTS)

    tests, templates = [], []
    for path in sources:
        stem = os.path.splitext(os.path.basename(path))[0]
        if TEMPLATE_MARKER in stem.lower():
            templates.append(path)
        else:
            tests.append(path)
    return tests, templates


def warn_duplicates(tests, folder):
    """Warn about tests sharing a name, since their outputs collide."""
    by_stem = {}
    for path in tests:
        stem = os.path.splitext(os.path.basename(path))[0]
        by_stem.setdefault(stem, []).append(path)

    duplicates = {s: p for s, p in by_stem.items() if len(p) > 1}
    if not duplicates:
        return

    print(f"[WARN] {len(duplicates)} test name(s) appear more than once. "
          f"Outputs are named after the test, so these runs overwrite each "
          f"other's collected .vcd, .list and _report.txt:")
    for stem in sorted(duplicates):
        print(f"[WARN]   '{stem}':")
        for path in duplicates[stem]:
            print(f"[WARN]     {os.path.relpath(path, folder)}")
    print("[WARN] They will all be run. Keep the last one's results only, or "
          "rename them.\n")


def driver_results_dir(runner):
    """The run_results/ folder run_CVA6.py copies its keepers into."""
    return os.path.join(os.path.dirname(os.path.abspath(runner)),
                        "run_results")


def sim_output_dir():
    """The simulation tree run_CVA6.py writes: logs, VCD, binaries."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    return os.path.join(CVA6_ROOT, "verif/sim", f"out_{today}")


def output_paths(results_dir, test_name):
    """The three files run_CVA6.py leaves in run_results/ for this test."""
    return {
        "vcd": os.path.join(results_dir, f"{test_name}.vcd"),
        "list": os.path.join(results_dir, f"{test_name}.list"),
        "report": os.path.join(results_dir, f"{test_name}_report.txt"),
    }


def collect(results_dir, test_name, out_dir, want_vcd):
    """Move this run's three files into the batch's out folder."""
    collected = 0
    for key, source in output_paths(results_dir, test_name).items():
        if key == "vcd" and not want_vcd:
            continue
        if not os.path.isfile(source):
            print(f"[WARN] Expected output missing: {source}")
            continue
        try:
            shutil.move(source, os.path.join(out_dir,
                                             os.path.basename(source)))
            collected += 1
        except OSError as e:
            print(f"[WARN] Could not collect {source}: {e}")

    if collected:
        print(f"[INFO] Collected {collected} file(s) into {out_dir}")
    return collected


def sim_run_files(test_name, target):
    """This test's files inside the simulation tree."""
    log_dir = os.path.join(sim_output_dir(), "veri-testharness_sim")
    bin_dir = os.path.join(sim_output_dir(), "directed_tests")
    return [
        os.path.join(log_dir, f"{test_name}.{target}.vcd"),
        os.path.join(log_dir, f"{test_name}.{target}.log"),
        # run_CVA6.py's own capture of the build and the simulation.
        os.path.join(sim_output_dir(), f"{test_name}_run.log"),
        os.path.join(bin_dir, f"{test_name}.o"),
        os.path.join(bin_dir, f"{test_name}.list"),
        os.path.join(bin_dir, f"{test_name}_report.txt"),
    ]


def discard_run(results_dir, test_name, target):
    """Delete what this run left behind once it has been collected. A VCD runs
    to hundreds of megabytes per test. Only this test's files go, so a failed
    test's output survives the rest of the batch."""
    if os.path.isdir(results_dir):
        shutil.rmtree(results_dir, ignore_errors=True)
    for path in sim_run_files(test_name, target):
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass


def discard_sim_tree():
    """Remove the simulation tree, once nothing in it is worth keeping."""
    if os.path.isdir(sim_output_dir()):
        shutil.rmtree(sim_output_dir(), ignore_errors=True)


def clear_stale_outputs(results_dir, test_name):
    """Remove the previous run's files so nothing stale gets collected."""
    for path in output_paths(results_dir, test_name).values():
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass


def extract_metrics(report_path):
    """The metrics section of a _report.txt, or None if it holds none. The
    file is the measured disassembly then the metrics table, so everything from
    the rule above the table's title to the end is what is wanted."""
    try:
        with open(report_path) as f:
            lines = f.read().splitlines()
    except OSError as e:
        print(f"[WARN] Could not read {report_path}: {e}")
        return None

    for i, line in enumerate(lines):
        if line.startswith(METRICS_MARKER):
            # Take the rule above the title too, so the block arrives boxed.
            start = i - 1 if i and set(lines[i - 1]) == {"="} else i
            return "\n".join(lines[start:]).rstrip()

    return None


def write_metrics_file(out_dir, entries, info, filename):
    """Gather every run's metrics table into one metrics.txt. entries is
    [(label, report file)] in the order the runs were listed, so the file reads
    like the summary. A run with no table is named, not skipped."""
    blocks, missing = [], []
    for label, report_path in entries:
        block = extract_metrics(report_path)
        if block is None:
            missing.append(label)
            continue
        blocks.append(f">>> {label}\n{block}")

    if missing:
        print(f"[WARN] No metrics table for: {', '.join(missing)}")
    if not blocks:
        print(f"[WARN] No metrics tables found, so no {filename} written")
        return None

    path = os.path.join(out_dir, filename)
    try:
        with open(path, "w") as f:
            f.write(f"{SEP}\nALL METRICS\n{SEP}\n")
            for line in info:
                f.write(line + "\n")
            f.write(f"{SEP}\n\n")
            f.write("\n\n".join(blocks) + "\n")
    except OSError as e:
        print(f"[WARN] Could not write {path}: {e}")
        return None

    print(f"[INFO] {len(blocks)} metrics table(s) gathered in {path}")
    return path


def format_duration(seconds):
    minutes, secs = divmod(int(seconds), 60)
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{seconds:.1f}s"


def print_summary(results, total_elapsed):
    print("\n" + SEP)
    print("BENCHMARK SUMMARY")
    print(SEP)
    print(f"{'TEST':<35} | {'STATUS':>10} | {'TIME':>10}")
    print(SEP)

    for name, code, elapsed in results:
        status = "OK" if code == 0 else f"FAILED ({code})"
        print(f"{name[:35]:<35} | {status:>10} | "
              f"{format_duration(elapsed):>10}")

    passed = sum(1 for _, code, _ in results if code == 0)
    failed = len(results) - passed

    print(SEP)
    print(f"{len(results)} run, {passed} passed, {failed} failed, "
          f"total {format_duration(total_elapsed)}")
    print(SEP + "\n")

    if failed:
        print("[WARN] Failed tests: " +
              ", ".join(n for n, c, _ in results if c != 0))
    return failed


def main():
    parser = argparse.ArgumentParser(
        description="Run every benchmark in a folder through "
                    "run_CVA6.py.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Templates (any file with '{TEMPLATE_MARKER}' in its name) "
               f"are skipped.\nWith no folder given, "
               f"{DEFAULT_TESTS_DIR} is used.")
    parser.add_argument("--target", default=DEFAULT_TARGET,
                        help=f"Architecture target passed to run_CVA6.py. "
                             f"Defaults to {DEFAULT_TARGET}")
    parser.add_argument("folder", nargs="?", default=DEFAULT_TESTS_DIR,
                        help=f"Folder holding the tests. "
                             f"Defaults to {DEFAULT_TESTS_DIR}")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                        help=f"Where to gather the results. Defaults to "
                             f"{DEFAULT_OUT_DIR}/")
    parser.add_argument("--suite", choices=["config", "viewer"], default=None,
                        help="Forwarded to run_CVA6.py: which overhead table "
                             "to subtract")
    parser.add_argument("--cva6-root", default=None, metavar="DIR",
                        help="Forwarded to run_CVA6.py: the CVA6 checkout to "
                             "run")
    parser.add_argument("--no-vcd", action="store_true",
                        help="Forwarded to run_CVA6.py: no .vcd trace, "
                             "metrics only")
    parser.add_argument("--rebuild-each", action="store_true",
                        help="Rebuild the Verilated core before every test. "
                             "By default only the first test builds it and "
                             "the rest reuse it with --keep-build, which is "
                             "safe here because the target and the trace "
                             "setting are the same for the whole batch")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="Also pick up tests in subfolders")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would run, and the name clashes, "
                             "without running anything")
    args = parser.parse_args()

    # Keep our own output interleaved correctly with each run_CVA6.py
    # run. Redirected to a file, stdout would otherwise be block-buffered
    # here while the children write straight through, scrambling the log.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        print(f"[ERROR] The folder {folder} does not exist")
        root = args.cva6_root or CVA6_ROOT
        if os.path.isfile(os.path.join(root, "core", "include",
                                       f"{args.folder}_config_pkg.sv")):
            print(f"[ERROR] '{args.folder}' is a target. It is --target now, "
                  f"and the only positional is the folder, so leaving both "
                  f"out runs {DEFAULT_TESTS_DIR} on {DEFAULT_TARGET}.")
        sys.exit(2)

    runner = find_runner()
    tests, templates = discover(folder, args.recursive)

    print(SEP)
    print("BENCHMARK BATCH")
    print(SEP)
    print(f"Folder   : {folder}")
    print(f"Target   : {args.target}")
    print(f"Runner   : {runner}")
    print(f"Out dir  : {os.path.abspath(args.out_dir)}")
    print(f"Tracing  : {'disabled (--no-vcd)' if args.no_vcd else 'enabled'}")
    print(f"Build    : "
          f"{'rebuilt before every test' if args.rebuild_each else 'built once, then reused'}")
    print(SEP + "\n")

    if templates:
        print(f"[INFO] Skipping {len(templates)} template(s): " +
              ", ".join(os.path.basename(p) for p in templates))

    if not tests:
        print(f"[ERROR] No tests found in {folder}. Looked for: " +
              ", ".join(sorted(SOURCE_EXTS)))
        sys.exit(2)

    print(f"[INFO] {len(tests)} test(s) to run:")
    for path in tests:
        print(f"[INFO]   {os.path.relpath(path, folder)}")
    print()

    warn_duplicates(tests, folder)

    if args.dry_run:
        print("[INFO] Dry run, nothing executed.")
        return 0

    results_dir = driver_results_dir(runner)
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    results = []
    batch_start = time.time()

    for index, path in enumerate(tests, 1):
        name = os.path.basename(path)
        test_name = os.path.splitext(name)[0]
        print("\n" + SEP)
        print(f"[{index}/{len(tests)}] {name}")
        print(SEP + "\n")

        clear_stale_outputs(results_dir, test_name)

        cmd = [sys.executable, runner, args.target, path]
        if args.suite:
            cmd.extend(["--suite", args.suite])
        if args.cva6_root:
            cmd.extend(["--cva6-root", args.cva6_root])
        if args.no_vcd:
            cmd.append("--no-vcd")
        # The Verilated model does not depend on the test, and the target and
        # the trace setting are fixed for the batch, so only the first test
        # pays for the build.
        if index > 1 and not args.rebuild_each:
            cmd.append("--keep-build")

        start = time.time()
        try:
            code = subprocess.run(cmd).returncode
        except KeyboardInterrupt:
            print(f"\n[WARN] Interrupted during '{name}'. "
                  f"Stopping the batch.")
            results.append((name, 130, time.time() - start))
            break
        elapsed = time.time() - start

        if code != 0:
            # Leave this one where the simulation put it: its output is what
            # there is to debug with.
            print(f"\n[WARN] '{name}' failed with exit code {code}. "
                  f"Its output is left in place, including "
                  f"{os.path.join(sim_output_dir(), test_name + '_run.log')}. "
                  f"Continuing with the rest.")
        else:
            collect(results_dir, test_name, out_dir, not args.no_vcd)
            discard_run(results_dir, test_name, args.target)
        results.append((name, code, elapsed))

    failed = print_summary(results, time.time() - batch_start)

    # Only a run that passed left a table behind to gather.
    write_metrics_file(
        out_dir,
        [(name, os.path.join(out_dir,
                             f"{os.path.splitext(name)[0]}_report.txt"))
         for name, code, _ in results if code == 0],
        [f"Folder    : {folder}",
         f"Target    : {args.target}",
         f"CVA6 root : {args.cva6_root or '(run_CVA6.py default)'}",
         f"Suite     : {args.suite or '(run_CVA6.py default)'}",
         f"Runs      : {len(results)}, {len(results) - failed} passed"],
        metrics_filename([args.target, args.suite]))

    print(f"[INFO] Results in {out_dir}")
    if failed:
        print(f"[INFO] The failed test(s) left their output under "
              f"{sim_output_dir()}")
    else:
        # Nothing in there is worth keeping now, so take the tree with it.
        discard_sim_tree()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
