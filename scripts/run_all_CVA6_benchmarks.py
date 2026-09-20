#!/usr/bin/env python3
"""Run every benchmark in a folder through run_CVA6.py, dropping the
templates and printing a pass/fail summary. Only the first test pays for the
Verilator build, the rest reuse it. --rebuild-each rebuilds every time:

    python3 scripts/run_all_CVA6_benchmarks.py benchmarks/
"""
import argparse
import glob
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time

# =============================================================================
# CONFIGURATION
# =============================================================================
# Default folder, relative to the CVA6 root, where the Docker image puts the
# viewer's set, the same one the sweep runs. Outside the image this
# repository's own benchmarks/ stands in for it.
DEFAULT_TESTS_DIR = os.path.join("benchmarks", "viewer")

# The driver this script delegates to, looked up next to it and then in cwd.
RUNNER_NAME = "run_CVA6.py"

# Matches run_CVA6.py's own default, the target the overhead tables were
# measured on.
DEFAULT_TARGET = "cv64a6_imafdc_sv39_hpdcache_wb"

# The CVA6 root run_CVA6.py picks without --cva6-root, when it exists.
IMAGE_CVA6_ROOT = "/CVA6"

# Where the batch gathers what it keeps, one folder for the whole run.
DEFAULT_OUT_DIR = os.path.join("results", "batch")

# Test extensions picked up, a case-sensitive subset of what run_CVA6.py
# accepts, so a stray .C or .ASM is left out of a batch.
SOURCE_EXTS = {".c", ".S", ".s", ".asm", ".sx"}

# A file whose name contains this is a starting point, not a benchmark.
TEMPLATE_MARKER = "template"

# What run_CVA6.py writes above its metrics table, and where the batch
# gathers every one of those tables once the runs are done.
METRICS_MARKER = "RESULTS TABLE"

SEP = "=" * 70


# SHARED BEGIN py-run-helpers

# Needs: os, re, METRICS_MARKER, SEP


def slug(text, limit=40):
    """Turn a value into something safe for a file name: ASCII letters and
    digits kept, each run of anything else one dash, trimmed of dashes at both
    ends and cut to limit characters."""
    out = re.sub(r"[^A-Za-z0-9]+", "-", str(text)).strip("-")
    return out[:limit].strip("-")


def format_duration(seconds):
    # Rounded to the one decimal it prints, before the split, so 59.99 reads
    # 1m00s and never 60.0s.
    seconds = round(seconds, 1)
    minutes, secs = divmod(int(seconds), 60)
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{seconds:.1f}s"


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


def metrics_filename(parts):
    """The gathered metrics file, named after the run that produced it."""
    tags = [slug(p) for p in parts if p]
    return "metrics" + ("_" if tags else "") + "_".join(tags) + ".txt"


def write_metrics_file(out_dir, entries, info, filename):
    """Gather every run's metrics table into one file, named filename.
    entries is [(label, report file)] in the order of the summary, so the
    file reads like it. A run with no table is named, not skipped."""
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

# SHARED END py-run-helpers


# SHARED BEGIN py-cva6-run-dirs

# Needs: glob, importlib.util, os, shutil, sys, DEFAULT_TESTS_DIR,
# IMAGE_CVA6_ROOT, RUNNER_NAME


def find_runner():
    """Locate run_CVA6.py next to this script, then in the cwd, or None."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, RUNNER_NAME),
                      os.path.abspath(RUNNER_NAME)):
        if os.path.isfile(candidate):
            return candidate
    print(f"[ERROR] {RUNNER_NAME} not found next to this script or in the "
          f"current directory.", file=sys.stderr)
    return None


def load_runner(path):
    """run_CVA6.py as a module, so its tables decide what --suite accepts."""
    spec = importlib.util.spec_from_file_location("run_CVA6", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def default_cva6_root(runner):
    """The CVA6 root run_CVA6.py picks without --cva6-root: /CVA6 when it
    exists, otherwise the nearest folder above the runner holding
    verif/sim."""
    if os.path.isdir(IMAGE_CVA6_ROOT):
        return IMAGE_CVA6_ROOT
    here = os.path.dirname(os.path.abspath(runner))
    path = here
    while True:
        if os.path.isdir(os.path.join(path, "verif", "sim")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return here
        path = parent


def default_tests_dir(root):
    """DEFAULT_TESTS_DIR under the CVA6 root, where the image puts the
    viewer's set, else this repository's own benchmarks/ beside scripts/."""
    folder = os.path.join(root, DEFAULT_TESTS_DIR)
    if os.path.isdir(folder):
        return folder
    here = os.path.dirname(os.path.abspath(__file__))
    own = os.path.join(os.path.dirname(here), "benchmarks")
    return own if os.path.isdir(own) else folder


def driver_results_dir(root):
    """The results/run/ folder run_CVA6.py copies its keepers into, under the
    CVA6 root rather than beside the driver."""
    return os.path.join(root, "results", "run")


def run_output_dirs(root, test_name):
    """The out_<date>/ folders holding any of this test's files. run_CVA6.py
    and cva6.py each name theirs from the date they started, so a run that
    crosses midnight can leave files in two of them."""
    found = []
    for folder in sorted(glob.glob(os.path.join(root, "verif", "sim",
                                                "out_*"))):
        if any(os.path.isfile(path)
               for path in sim_run_files(folder, test_name, "*")):
            found.append(folder)
    return found


def output_paths(results_dir, test_name):
    """The three files run_CVA6.py leaves in results/run/ for this test."""
    return {
        "vcd": os.path.join(results_dir, f"{test_name}.vcd"),
        "list": os.path.join(results_dir, f"{test_name}.list"),
        "report": os.path.join(results_dir, f"{test_name}_report.txt"),
    }


def sim_run_files(folder, test_name, target):
    """This test's files inside one out_<date>/ folder. A target of * stands
    for any, which only a glob match honours."""
    log_dir = os.path.join(folder, "veri-testharness_sim")
    bin_dir = os.path.join(folder, "directed_tests")
    paths = [
        os.path.join(log_dir, f"{test_name}.{target}.vcd"),
        os.path.join(log_dir, f"{test_name}.{target}.log"),
        # run_CVA6.py's own capture of the build and the simulation.
        os.path.join(folder, f"{test_name}_run.log"),
        os.path.join(bin_dir, f"{test_name}.o"),
        os.path.join(bin_dir, f"{test_name}.list"),
        os.path.join(bin_dir, f"{test_name}_report.txt"),
    ]
    if target == "*":
        return [match for path in paths for match in glob.glob(path)]
    return paths


def discard_run(root, results_dir, test_name, target):
    """Delete what this run left behind once it has been collected, since a
    VCD runs to hundreds of MiB per test. Only this test's files go, in
    results/run/ and in every dated folder it wrote to, so a failed test's
    output and anything else left there survive the rest of the runs.
    Returns the dated folders it cleared."""
    folders = run_output_dirs(root, test_name)
    for path in list(output_paths(results_dir, test_name).values()) + [
            path for folder in folders
            for path in sim_run_files(folder, test_name, target)]:
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    return folders


def discard_sim_trees(folders):
    """Remove the dated simulation folders the runs used, once nothing in
    them is worth keeping."""
    for folder in sorted(folders):
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)


def clear_stale_outputs(results_dir, test_name):
    """Remove the previous run's files so nothing stale gets collected."""
    for path in output_paths(results_dir, test_name).values():
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass

# SHARED END py-cva6-run-dirs


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
    runner = find_runner()
    if runner is None:
        return 2
    driver = load_runner(runner)

    parser = argparse.ArgumentParser(
        description="Run every benchmark in a folder through "
                    "run_CVA6.py.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Templates (any file with '{TEMPLATE_MARKER}' in its name) "
               f"are skipped.\nWith no folder given, "
               f"{DEFAULT_TESTS_DIR} under the CVA6 root is used, or this\n"
               f"repository's benchmarks/ when the CVA6 root has none.")
    parser.add_argument("--target", default=DEFAULT_TARGET,
                        help=f"Architecture target passed to run_CVA6.py. "
                             f"Defaults to {DEFAULT_TARGET}")
    parser.add_argument("folder", nargs="?", default=None,
                        help=f"Folder holding the tests. Defaults to "
                             f"{DEFAULT_TESTS_DIR} under the CVA6 root, else "
                             f"this repository's benchmarks/")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                        help=f"Where to gather the results. Defaults to "
                             f"{DEFAULT_OUT_DIR}/")
    parser.add_argument("--suite", choices=sorted(driver.OVERHEAD_SUITES),
                        default=None,
                        help="Forwarded to run_CVA6.py: which overhead table "
                             "to subtract. Defaults to the .overhead_suite "
                             "beside each test, and run_CVA6.py stops without "
                             "one")
    parser.add_argument("--cva6-root", default=None, metavar="DIR",
                        help="The CVA6 checkout to run and collect from, "
                             "forwarded to run_CVA6.py. Defaults to the one "
                             "run_CVA6.py picks: /CVA6 when it exists, "
                             "otherwise the checkout above the runner")
    parser.add_argument("--no-vcd", action="store_true",
                        help="Forwarded to run_CVA6.py: no VCD, "
                             "metrics only")
    parser.add_argument("--rebuild-each", action="store_true",
                        help="Rebuild the Verilated core before every test. "
                             "By default only the first test builds it and "
                             "the rest reuse it with --keep-build, which is "
                             "safe here because the target and the VCD "
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

    root = os.path.abspath(args.cva6_root or default_cva6_root(runner))

    folder = os.path.abspath(args.folder or default_tests_dir(root))
    if not os.path.isdir(folder):
        print(f"[ERROR] Folder not found: {folder}", file=sys.stderr)
        return 2

    tests, templates = discover(folder, args.recursive)

    print(SEP)
    print("BENCHMARK BATCH")
    print(SEP)
    print(f"Folder    : {folder}")
    print(f"Target    : {args.target}")
    print(f"Runner    : {runner}")
    print(f"CVA6 root : {root}")
    print(f"Out dir   : {os.path.abspath(args.out_dir)}")
    print(f"VCD       : "
          f"{'disabled (--no-vcd)' if args.no_vcd else 'enabled'}")
    build = ("rebuilt before every test" if args.rebuild_each
             else "built once, then reused")
    print(f"Build     : {build}")
    print(SEP + "\n")

    if templates:
        print(f"[INFO] Skipping {len(templates)} template(s): " +
              ", ".join(os.path.basename(p) for p in templates))

    if not tests:
        print(f"[ERROR] No tests found in {folder}. Looked for: " +
              ", ".join(sorted(SOURCE_EXTS)), file=sys.stderr)
        return 2

    print(f"[INFO] {len(tests)} test(s) to run:")
    for path in tests:
        print(f"[INFO]   {os.path.relpath(path, folder)}")
    print()

    warn_duplicates(tests, folder)

    if args.dry_run:
        print("[INFO] Dry run, nothing executed.")
        return 0

    results_dir = driver_results_dir(root)
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    results = []
    used_folders = set()
    batch_start = time.time()

    for index, path in enumerate(tests, 1):
        name = os.path.basename(path)
        test_name = os.path.splitext(name)[0]
        print("\n" + SEP)
        print(f"[{index}/{len(tests)}] {name}")
        print(SEP + "\n")

        clear_stale_outputs(results_dir, test_name)

        # This batch discards the simulation tree itself, per test and then
        # whole, so the driver must not carry it off to results/ first. The
        # root is always passed, so the driver writes where this collects.
        cmd = [sys.executable, runner, args.target, path,
               "--no-keep-sim-output", "--cva6-root", root]
        if args.suite:
            cmd.extend(["--suite", args.suite])
        if args.no_vcd:
            cmd.append("--no-vcd")
        # The Verilated model does not depend on the test, and the target and
        # the VCD setting are fixed for the batch, so only the first test
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
            where = run_output_dirs(root, test_name)
            print(f"\n[WARN] '{name}' failed with exit code {code}. "
                  f"Its output is left in place, in "
                  f"{', '.join(where) or 'verif/sim'}. Continuing with the "
                  f"rest.")
        else:
            collect(results_dir, test_name, out_dir, not args.no_vcd)
            used_folders.update(
                discard_run(root, results_dir, test_name, args.target))
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
         f"CVA6 root : {root}",
         f"Suite     : {args.suite or '(run_CVA6.py default)'}",
         f"Runs      : {len(results)}, {len(results) - failed} passed"],
        metrics_filename([args.target, args.suite]))

    print(f"[INFO] Results in {out_dir}")
    if failed:
        print(f"[INFO] The failed test(s) left their output under "
              f"{os.path.join(root, 'verif', 'sim')}")
    else:
        # Nothing in those is worth keeping now, so take the trees with it.
        discard_sim_trees(used_folders)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
