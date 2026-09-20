#!/usr/bin/env python3
"""Measure the overhead profiles run_CVA6.py subtracts to get NET.

A profile is what the harness around a measured region costs on its own:
the empty test_template of a suite, run in one language, read from the
OFFICIAL column. This runs every template on run_CVA6.py's default target,
prints the profiles beside the ones run_CVA6.py carries, and with --write
puts them into it. The runs share one Verilator build, so they run in turn,
and the first one builds it.

Launch it from the CVA6 root, where run_CVA6.py is launched from:

  python3 scripts/measure_CVA6_overhead.py                 # measure, compare
  python3 scripts/measure_CVA6_overhead.py --suite config  # one suite only
  python3 scripts/measure_CVA6_overhead.py --write         # and update
  python3 scripts/measure_CVA6_overhead.py -n              # print the runs
"""
import argparse
import importlib.util
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(HERE, "run_CVA6.py")

# The template of each language, and where each suite's templates live, the
# container's layout first and then this repository's own.
TEMPLATES = {"c": "test_template.c", "asm": "test_template.S"}
SUITE_DIRS = ("benchmarks/config", "benchmarks/viewer", "benchmarks")
SUITE_MARKER = ".overhead_suite"

# The OFFICIAL row, in the order run_CVA6.py prints it, and what each is.
KEYS = ("x18", "x19", "x20", "x21", "x22", "x23", "x24", "x25")
NAMES = ("Cycles", "Instructions", "I-cache misses", "D-cache misses",
         "I-cache accesses", "D-cache accesses", "Branches",
         "Mispredicts + unpredicted")
OFFICIAL = re.compile(r"^Clean result \(OFFICIAL\):\s*\[([^\]]*)\]", re.M)


def suite_dirs():
    """{suite: folder} for every folder whose marker names a suite."""
    found = {}
    for rel in SUITE_DIRS:
        marker = os.path.join(rel, SUITE_MARKER)
        if os.path.isfile(marker):
            with open(marker) as handle:
                found.setdefault(handle.read().strip(), rel)
    return found


def load_runner():
    spec = importlib.util.spec_from_file_location("run_CVA6", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def measure(template, lang, keep_build, dry_run):
    """Run one template and return its profile or an error message."""
    cmd = [sys.executable, RUNNER, template, "--lang", lang, "--no-vcd"]
    if keep_build:
        cmd.append("--keep-build")
    if dry_run:
        return "  $ " + " ".join(cmd)
    done = subprocess.run(cmd, capture_output=True, text=True)
    match = OFFICIAL.search(done.stdout)
    if done.returncode != 0 or not match:
        tail = (done.stdout + done.stderr).strip().splitlines()[-3:]
        return "run failed: " + " | ".join(tail)
    values = [float(v) for v in match.group(1).split(",")[:len(KEYS)]]
    return dict(zip(KEYS, (int(v) for v in values)))


def render(table):
    """OVERHEAD_SUITES as run_CVA6.py writes it."""
    out = ["OVERHEAD_SUITES = {"]
    for suite in table:
        out.append(f'    "{suite}": {{')
        for lang in table[suite]:
            out.append(f'        "{lang}": {{')
            for key, name in zip(KEYS, NAMES):
                value = f"{table[suite][lang][key]},"
                out.append(f"            '{key}': {value:<6}# {name}")
            out.append("        },")
        out.append("    },")
    out.append("}")
    return "\n".join(out)


def write_table(table):
    """Replace OVERHEAD_SUITES in run_CVA6.py, and nothing else."""
    with open(RUNNER) as handle:
        text = handle.read()
    start = text.index("OVERHEAD_SUITES = {")
    end = text.index("\n}\n", start) + 2
    with open(RUNNER, "w") as handle:
        handle.write(text[:start] + render(table) + text[end:])


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Measure run_CVA6.py's overhead profiles from the empty "
                    "templates.",
        epilog="Every run uses run_CVA6.py's default target, the one the "
               "profiles are for, with no VCD.")
    parser.add_argument("--suite", choices=["config", "viewer", "all"],
                        default="all", help="Which suite. Defaults to both")
    parser.add_argument("--keep-build", action="store_true",
                        help="Reuse work-ver for the first run too, when it "
                             "was built for the default target without a VCD")
    parser.add_argument("--write", action="store_true",
                        help="Put the measured profiles into run_CVA6.py "
                             "beside this script")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="Print the runs without starting them")
    args = parser.parse_args()

    if not os.path.isfile(RUNNER):
        print(f"[ERROR] {RUNNER} not found. This script sits beside "
              f"run_CVA6.py.")
        return 2
    dirs = suite_dirs()
    suites = ["config", "viewer"] if args.suite == "all" else [args.suite]
    results, keep = {}, args.keep_build
    for suite in suites:
        if suite not in dirs:
            print(f"[ERROR] No folder under {', '.join(SUITE_DIRS)} has a "
                  f"{SUITE_MARKER} naming '{suite}'. Run this from the CVA6 "
                  f"root.")
            return 2
        for lang, name in TEMPLATES.items():
            result = measure(os.path.join(dirs[suite], name), lang, keep,
                             args.dry_run)
            # The first run built the model, which every later one reuses.
            keep = True
            if args.dry_run:
                print(result)
                continue
            if isinstance(result, str):
                print(f"[ERROR] {suite}/{lang}: {result}")
                return 1
            results[(suite, lang)] = result
            print(f"[INFO] {suite}/{lang} measured")
    if args.dry_run:
        return 0

    current = load_runner().OVERHEAD_SUITES
    table = {s: {lang: dict(p) for lang, p in current[s].items()}
             for s in current}
    changed = 0
    for (suite, lang), profile in sorted(results.items()):
        old = current.get(suite, {}).get(lang, {})
        diffs = [f"{NAMES[KEYS.index(k)]} {old.get(k)} -> {v}"
                 for k, v in profile.items() if old.get(k) != v]
        changed += bool(diffs)
        print(f"  {suite:6} {lang:3}  "
              + (", ".join(diffs) if diffs else "as run_CVA6.py has it"))
        table.setdefault(suite, {})[lang] = profile
    print(f"[INFO] {changed} of {len(results)} profile(s) differ from "
          f"run_CVA6.py")
    if args.write and changed:
        write_table(table)
        print(f"[INFO] Wrote the profiles into {RUNNER}")
    elif changed:
        print("[INFO] --write puts them into run_CVA6.py, or paste this:\n")
        print(render(table))
    return 0


if __name__ == "__main__":
    sys.exit(main())
