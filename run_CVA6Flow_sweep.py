#!/usr/bin/env python3
"""
Run the CVA6Flow configuration sweep.

Reads the configuration table out of the swept config package
(cv64a6_imafdc_sv39_hpdcache_wb_config_pkg.sv) and, for each configuration,
installs that package with CVA6_CONFIG_SEL pointing at the variant, then runs
only the workloads that configuration was cut for:

    localparam int CFG_BHT_64 = 4;   // BHTEntries 128 -> 64 : bht_alias_test

A configuration whose workload is 'all' runs every workload named anywhere in
the table, which is the set the sweep has been exercised with.

Outputs are collected as <test>.config<N>.<ext>, so one configuration's
results never overwrite another's. Every metrics table is also gathered into
a single metrics.txt.

The live config package is restored when the sweep ends, fails or is
interrupted.
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
CVA6_ROOT = "/cva6"

# The package the build actually reads.
LIVE_CONFIG_PKG = os.path.join(
    CVA6_ROOT, "core/include/cv64a6_imafdc_sv39_hpdcache_wb_config_pkg.sv")

# The swept package, carrying the table and the CVA6_CONFIG_SEL selector.
SOURCE_CONFIG_PKG = "cv64a6_imafdc_sv39_hpdcache_wb_config_pkg.sv"

DEFAULT_TARGET = "cv64a6_imafdc_sv39_hpdcache_wb"
DEFAULT_TESTS_DIR = os.path.join(CVA6_ROOT, "CVA6Flow_benchmarks")
DEFAULT_OUT_DIR = "CVA6Flow_sweep_results"

RUNNER_NAME = "run_CVA6.py"

# Extensions tried when turning a workload name into a file, in this order.
EXT_PRIORITY = [".c", ".S", ".s", ".asm", ".sx"]

# 'localparam int CFG_BHT_64 = 4;   // BHTEntries 128 -> 64 : bht_alias_test'
ROW_RE = re.compile(
    r'^\s*localparam\s+int\s+(CFG_\w+)\s*=\s*(\d+)\s*;\s*//\s*(.*)$', re.M)

# 'localparam int CVA6_CONFIG_SEL = CFG_BASELINE;'
SELECTOR_RE = re.compile(
    r'^(\s*localparam\s+int\s+CVA6_CONFIG_SEL\s*=\s*)(\w+)(\s*;.*)$', re.M)

SEP = "=" * 70

# What the runner writes above its metrics table, and where the sweep gathers
# every one of those tables once the runs are done.
METRICS_MARKER = "RESULTS TABLE"
METRICS_FILE = "metrics.txt"


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


def find_source_pkg(explicit):
    """Locate the swept config package."""
    if explicit:
        if os.path.isfile(explicit):
            return os.path.abspath(explicit)
        print(f"[ERROR] Config package not found: {explicit}")
        sys.exit(2)

    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, SOURCE_CONFIG_PKG),
                      os.path.abspath(SOURCE_CONFIG_PKG),
                      # In the repository the swept package lives with the
                      # viewer it belongs to, two levels up from here.
                      os.path.join(here, "..", "..", "viewers", "CVA6Flow",
                                   SOURCE_CONFIG_PKG),
                      LIVE_CONFIG_PKG):
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    print(f"[ERROR] {SOURCE_CONFIG_PKG} not found next to this script, in the "
          f"current directory, or at {LIVE_CONFIG_PKG}. Pass --config-pkg.")
    sys.exit(2)


def workloads_from_comment(comment):
    """Pull the workload list out of a table comment's trailing ': ...' part."""
    if ":" not in comment:
        return []
    tail = comment.rsplit(":", 1)[1]
    # Drop parentheticals such as '(TLB bypassed in M-mode, expect null)',
    # which would otherwise look like extra comma-separated workloads.
    tail = re.sub(r"\(.*?\)", "", tail)
    return [w.strip() for w in tail.split(",") if w.strip()]


def parse_table(text):
    """Parse the configuration table into {id: (cfg_name, description,
    workloads)}."""
    table = {}
    for cfg_name, number, comment in ROW_RE.findall(text):
        comment = comment.strip()
        description = comment.rsplit(":", 1)[0].strip() if ":" in comment \
            else comment
        table[int(number)] = (cfg_name, description,
                              workloads_from_comment(comment))
    return table


def resolve_all(table):
    """The 'all' workload set: every workload the table names explicitly."""
    named = []
    for _, _, workloads in table.values():
        for workload in workloads:
            if workload.lower() != "all" and workload not in named:
                named.append(workload)
    return sorted(named)


def suggest(name, tests_dir):
    """Files that look close to a workload name that did not resolve."""
    try:
        entries = sorted(os.listdir(tests_dir))
    except OSError:
        return []
    # Compare on the stem, so a workload typed with an extension or as a
    # path still finds its neighbours.
    stem = os.path.splitext(os.path.basename(name))[0].lower()
    return [e for e in entries
            if os.path.splitext(e)[1] in EXT_PRIORITY
            and stem in os.path.splitext(e)[0].lower()]


def resolve_test_file(name, tests_dir):
    """Turn a workload into a path.

    The table writes a workload as a bare name, but a name carrying its
    extension and a path to a file are what a person naturally types on
    --tests, so all three resolve rather than only the first."""
    # A path, absolute or relative to the working directory, taken as given.
    if os.path.isfile(name):
        return name

    stem, ext = os.path.splitext(name)
    if ext in EXT_PRIORITY:
        # A name that already carries its extension, inside the tests folder.
        candidate = os.path.join(tests_dir, name)
        if os.path.isfile(candidate):
            return candidate
        # Fall through on the stem: the same workload under a different
        # extension is a likelier intent than no match at all.
        name = stem

    matches = [os.path.join(tests_dir, name + e) for e in EXT_PRIORITY
               if os.path.isfile(os.path.join(tests_dir, name + e))]
    if not matches:
        return None
    if len(matches) > 1:
        print(f"[WARN] '{name}' matches more than one file: " +
              ", ".join(os.path.basename(m) for m in matches) +
              f". Using {os.path.basename(matches[0])}.")
    return matches[0]


def parse_config_selection(spec, table):
    """Parse '1,4-6' into a sorted list of configuration ids."""
    if not spec:
        return sorted(table)

    selected = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            low, _, high = chunk.partition("-")
            try:
                selected.update(range(int(low), int(high) + 1))
            except ValueError:
                print(f"[ERROR] Bad configuration range: '{chunk}'")
                sys.exit(2)
        else:
            try:
                selected.add(int(chunk))
            except ValueError:
                print(f"[ERROR] Bad configuration id: '{chunk}'")
                sys.exit(2)

    unknown = sorted(selected - set(table))
    if unknown:
        print(f"[ERROR] No such configuration(s): "
              f"{', '.join(str(u) for u in unknown)}. "
              f"The table has {min(table)}-{max(table)}.")
        sys.exit(2)
    return sorted(selected)


def build_plan(table, config_ids, tests_dir, override_tests):
    """Build [(config_id, cfg_name, description, [test paths])], warning about
    workloads with no matching file."""
    all_tests = override_tests if override_tests else resolve_all(table)
    plan = []
    missing = []

    for config_id in config_ids:
        cfg_name, description, workloads = table[config_id]

        if override_tests:
            names = list(override_tests)
        elif not workloads or any(w.lower() == "all" for w in workloads):
            names = list(all_tests)
        else:
            names = list(workloads)

        paths = []
        for name in names:
            path = resolve_test_file(name, tests_dir)
            if path:
                paths.append(path)
            elif name not in missing:
                missing.append(name)
        plan.append((config_id, cfg_name, description, paths))

    for name in missing:
        hints = suggest(name, tests_dir)
        print(f"[WARN] No file in {tests_dir} for the workload '{name}' "
              f"(looked for {name} plus {', '.join(EXT_PRIORITY)})." +
              (f" Did you mean {' or '.join(hints)}?" if hints else ""))

    # A configuration left with nothing to run would otherwise be skipped in
    # silence, which reads as 'swept' when it was not.
    empty = [config_id for config_id, _, _, paths in plan if not paths]
    if empty:
        print(f"[WARN] {len(empty)} configuration(s) have no runnable "
              f"workload and will be skipped: " +
              ", ".join(f"config{c}" for c in empty))
    if missing or empty:
        print()
    return plan


def install_config(source_text, cfg_name, live_path):
    """Write the swept package to the live path with the selector set."""
    text, count = SELECTOR_RE.subn(
        lambda m: m.group(1) + cfg_name + m.group(3), source_text, count=1)
    if count != 1:
        print(f"[ERROR] CVA6_CONFIG_SEL not found in the config package, so "
              f"the configuration cannot be selected.")
        sys.exit(2)
    with open(live_path, "w") as f:
        f.write(text)


def driver_results_dir(runner):
    """The run_results/ folder run_CVA6.py copies its keepers into."""
    return os.path.join(os.path.dirname(os.path.abspath(runner)), "run_results")


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


def collect(results_dir, test_name, config_id, out_dir, want_vcd):
    """Move this run's three files out under their .config<N> names."""
    produced = output_paths(results_dir, test_name)
    wanted = {
        "vcd": f"{test_name}.config{config_id}.vcd",
        "list": f"{test_name}.config{config_id}.list",
        "report": f"{test_name}_report.config{config_id}.txt",
    }

    collected = 0
    for key, source in produced.items():
        if key == "vcd" and not want_vcd:
            continue
        if not os.path.isfile(source):
            print(f"[WARN] Expected output missing: {source}")
            continue
        try:
            shutil.move(source, os.path.join(out_dir, wanted[key]))
            collected += 1
        except OSError as e:
            print(f"[WARN] Could not collect {source}: {e}")

    if collected:
        print(f"[INFO] Collected {collected} file(s) into {out_dir} as "
              f"{test_name}.config{config_id}.*")
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
    """Delete what this run left behind, once it has been collected.

    A VCD runs to hundreds of megabytes and one is produced per run, so
    keeping them would cost far more disk than the sweep is worth. Only this
    test's files are removed, so a failed run's output survives the rest of
    the sweep."""
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
    """The metrics section of a _report.txt, or None if it holds none.

    A _report.txt is the measured region of the disassembly followed by the
    metrics table, so everything from the rule above the table's title to the
    end of the file is the section wanted here."""
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


def write_metrics_file(out_dir, entries, info):
    """Gather every run's metrics table into one metrics.txt.

    entries is [(label, report file)] in plan order, so the file reads in the
    same order as the summary above it. A run whose table is missing is named
    rather than skipped silently."""
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
        print(f"[WARN] No metrics tables found, so no {METRICS_FILE} written")
        return None

    path = os.path.join(out_dir, METRICS_FILE)
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


def print_plan(plan, table, all_tests):
    print(f"[INFO] 'all' resolves to {len(all_tests)} workload(s): " +
          ", ".join(all_tests) + "\n")
    total = 0
    for config_id, cfg_name, description, paths in plan:
        names = [os.path.basename(p) for p in paths]
        total += len(paths)
        print(f"  config{config_id:<3} {cfg_name:<18} {description}")
        print(f"      {len(names)} test(s): " +
              (", ".join(names) if names else "none"))
    print(f"\n[INFO] {len(plan)} configuration(s), {total} run(s) total.")
    return total


def print_summary(results, total_elapsed):
    print("\n" + SEP)
    print("SWEEP SUMMARY")
    print(SEP)
    print(f"{'CONFIG':>8} | {'TEST':<28} | {'STATUS':>10} | {'TIME':>9}")
    print(SEP)

    for config_id, name, code, elapsed in results:
        status = "OK" if code == 0 else f"FAILED ({code})"
        print(f"{('config' + str(config_id)):>8} | {name[:28]:<28} | "
              f"{status:>10} | {format_duration(elapsed):>9}")

    passed = sum(1 for _, _, code, _ in results if code == 0)
    failed = len(results) - passed

    print(SEP)
    print(f"{len(results)} run, {passed} passed, {failed} failed, "
          f"total {format_duration(total_elapsed)}")
    print(SEP + "\n")

    if failed:
        print("[WARN] Failed: " + ", ".join(
            f"config{c}/{n}" for c, n, code, _ in results if code != 0))
    return failed


def main():
    parser = argparse.ArgumentParser(
        description="Run the CVA6Flow configuration sweep: each configuration "
                    "in the config package, with the workloads it was cut "
                    "for.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="A configuration whose workload is 'all' runs every workload "
               "named\nin the table. Outputs are collected as "
               "<test>.config<N>.<ext>.")
    parser.add_argument("--target", default=DEFAULT_TARGET,
                        help=f"Architecture target. Defaults to "
                             f"{DEFAULT_TARGET}")
    parser.add_argument("--configs", default="",
                        help="Which configurations to run, e.g. '1,4-6'. "
                             "Defaults to all of them")
    parser.add_argument("--tests-dir", default=DEFAULT_TESTS_DIR,
                        help=f"Folder holding the workloads. Defaults to "
                             f"{DEFAULT_TESTS_DIR}")
    parser.add_argument("--tests", default="",
                        help="Comma-separated workloads to run for every "
                             "configuration, instead of the ones the table "
                             "names. A bare name, a file name with its "
                             "extension, or a path all work")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                        help=f"Where to collect the results. Defaults to "
                             f"{DEFAULT_OUT_DIR}/")
    parser.add_argument("--config-pkg", default="",
                        help=f"The swept config package. Defaults to "
                             f"{SOURCE_CONFIG_PKG} next to this script")
    parser.add_argument("--live-config-pkg", default=LIVE_CONFIG_PKG,
                        help=f"The package the build reads, overwritten per "
                             f"configuration and restored at the end. "
                             f"Defaults to {LIVE_CONFIG_PKG}")
    parser.add_argument("--no-vcd", action="store_true",
                        help="Forwarded to run_CVA6.py: no .vcd trace, "
                             "metrics only")
    parser.add_argument("--list", action="store_true",
                        help="Print the plan and exit, touching nothing")
    args = parser.parse_args()

    # Keep our output interleaved correctly with each run_CVA6.py run.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    source_pkg = find_source_pkg(args.config_pkg)
    with open(source_pkg) as f:
        source_text = f.read()

    table = parse_table(source_text)
    if not table:
        print(f"[ERROR] No configuration table found in {source_pkg}. "
              f"Expected lines like "
              f"'localparam int CFG_X = 1; // description : workload'.")
        sys.exit(2)

    config_ids = parse_config_selection(args.configs, table)
    override_tests = [t.strip() for t in args.tests.split(",") if t.strip()]
    all_tests = override_tests if override_tests else resolve_all(table)

    print(SEP)
    print("CVA6FLOW SWEEP")
    print(SEP)
    print(f"Config pkg : {source_pkg}")
    print(f"Live pkg   : {args.live_config_pkg}")
    print(f"Target     : {args.target}")
    print(f"Tests dir  : {args.tests_dir}")
    print(f"Out dir    : {os.path.abspath(args.out_dir)}")
    print(
        f"Tracing    : {'disabled (--no-vcd)' if args.no_vcd else 'enabled'}")
    print(SEP + "\n")

    if not os.path.isdir(args.tests_dir):
        print(f"[ERROR] The tests folder {args.tests_dir} does not exist")
        sys.exit(2)

    plan = build_plan(table, config_ids, args.tests_dir, override_tests)
    total_runs = print_plan(plan, table, all_tests)

    if args.list:
        print("[INFO] Listing only, nothing run.")
        return 0
    if not total_runs:
        print("[ERROR] Nothing to run.")
        sys.exit(2)

    runner = find_runner()
    results_dir = driver_results_dir(runner)
    live_pkg = args.live_config_pkg
    if not os.path.isfile(live_pkg):
        print(f"[ERROR] The live config package {live_pkg} does not exist")
        sys.exit(2)
    os.makedirs(args.out_dir, exist_ok=True)

    with open(live_pkg) as f:
        original_live = f.read()
    print(f"\n[INFO] Backed up {live_pkg}, restored when the sweep ends.")

    results = []
    sweep_start = time.time()

    try:
        for config_id, cfg_name, description, paths in plan:
            if not paths:
                continue

            print("\n" + SEP)
            print(f"config{config_id}: {cfg_name} ({description})")
            print(SEP)
            install_config(source_text, cfg_name, live_pkg)
            print(f"[INFO] CVA6_CONFIG_SEL = {cfg_name}, the core will be "
                  f"rebuilt for this configuration.\n")

            for index, path in enumerate(paths, 1):
                test_name = os.path.splitext(os.path.basename(path))[0]
                print("\n" + "-" * 70)
                print(f"[config{config_id}] [{index}/{len(paths)}] "
                      f"{os.path.basename(path)}")
                print("-" * 70 + "\n")

                clear_stale_outputs(results_dir, test_name)

                cmd = [sys.executable, runner, args.target, path]
                if args.no_vcd:
                    cmd.append("--no-vcd")
                # The RTL changed with the configuration, so the first test
                # rebuilds. The rest of this configuration reuse that build.
                if index > 1:
                    cmd.append("--keep-build")

                start = time.time()
                code = subprocess.run(cmd).returncode
                elapsed = time.time() - start

                if code != 0:
                    # Leave the outputs in place: they are what there is to
                    # debug with.
                    print(f"\n[WARN] '{test_name}' failed with exit code "
                          f"{code}. Its output is left in place, including "
                          f"{os.path.join(sim_output_dir(), test_name + '_run.log')}"
                          f". Continuing with the rest.")
                else:
                    collect(results_dir, test_name, config_id, args.out_dir,
                            not args.no_vcd)
                    discard_run(results_dir, test_name,
                                args.target)
                results.append((config_id, test_name, code, elapsed))

    except KeyboardInterrupt:
        print("\n[WARN] Interrupted. Stopping the sweep.")
    finally:
        with open(live_pkg, "w") as f:
            f.write(original_live)
        print(f"\n[INFO] Restored {live_pkg}")

    failed = print_summary(results, time.time() - sweep_start)

    out_dir = os.path.abspath(args.out_dir)
    # Only a run that passed left a table behind to gather.
    write_metrics_file(
        out_dir,
        [(f"config{cid} / {name}",
          os.path.join(out_dir, f"{name}_report.config{cid}.txt"))
         for cid, name, code, _ in results if code == 0],
        [f"Target   : {args.target}",
         f"Tests dir: {os.path.abspath(args.tests_dir)}",
         f"Runs     : {len(results)}, {len(results) - failed} passed"])

    print(f"[INFO] Results in {out_dir}")
    if failed:
        print(f"[INFO] The failed run(s) left their output under "
              f"{sim_output_dir()}")
    else:
        # Nothing in there is worth keeping now, so take the tree with it.
        discard_sim_tree()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
