#!/usr/bin/env python3
"""Turn every Verilator VCD in a folder into a CVA6Flow viewer JSON.

Each VCD needs the objdump listing of the same test beside it, since that is
where the instruction text comes from. A VCD without one is skipped and named
rather than converted into a JSON the viewer would refuse, and the batch then
exits 1. A degraded VCD still gets its JSON, and makes the batch exit 3. A
sweep's <test>.config<N>.vcd is named after configuration N of the swept
package. This script belongs to the CVA6Flow repository, and runs from its
scripts/ or from a container's scripts/ beside CVA6Flow/. The fork's
scripts/create_all_CVA6_repo_jsons.py walks the whole checkout and calls this
one for the submodule.

    python3 scripts/create_all_CVA6Flow_jsons.py        # the repository root
    python3 scripts/create_all_CVA6Flow_jsons.py results/run
    python3 scripts/create_all_CVA6Flow_jsons.py -j 8
    python3 scripts/create_all_CVA6Flow_jsons.py --dry-run  # the plan only
    python3 scripts/create_all_CVA6Flow_jsons.py --force    # redo the JSONs
    python3 scripts/create_all_CVA6Flow_jsons.py --no-strict  # allow degraded
    python3 scripts/create_all_CVA6Flow_jsons.py results/sweep_CVA6Flow
"""
import argparse
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))


def find_tracer():
    """(tracer, the folder it sits in). In this repository the tracer is one
    level above scripts/, and in a container scripts/ is the root with the
    viewer a folder below it."""
    above = os.path.dirname(HERE)
    for base in (above, os.path.join(above, "CVA6Flow"),
                 os.path.join(os.curdir, "CVA6Flow")):
        candidate = os.path.join(base, "CVA6Flow_tracer.py")
        if os.path.isfile(candidate):
            return candidate, base
    return os.path.join(above, "CVA6Flow_tracer.py"), above


TRACER, REPO_ROOT = find_tracer()

# Four whatever the core count: each tracer holds a whole VCD's state, so
# memory binds before cores do. Each worker only waits on a subprocess, which
# is why these are threads rather than processes.
DEFAULT_WORKERS = 4

VCD_EXT = ".vcd"

# A sweep names its VCDs <test>.config<N>.vcd, N a CFG_ id of the package.
RE_SWEEP_STEM = re.compile(r"\.config(\d+)$")
RE_CFG_ID = re.compile(r"^\s*localparam\s+int\s+(CFG_\w+)\s*=\s*(\d+)\s*;",
                       re.M)
SWEPT_PKG = "cv64a6_imafdc_sv39_hpdcache_wb_config_CVA6Flow_pkg.sv"


# SHARED BEGIN py-human-size

# Needs: none


def human(size):
    """A size in bytes as whole B, or as KiB, MiB or GiB with one decimal."""
    if size < 1024:
        return f"{size:.0f} B"
    for unit in ("KiB", "MiB", "GiB"):
        size /= 1024
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"

# SHARED END py-human-size


# SHARED BEGIN py-json-staleness

# Needs: os, re

# A JSON's schema_version sits in the first lines of its metadata.
JSON_HEAD_BYTES = 4096
RE_TRACER_SCHEMA = re.compile(r"^SCHEMA_VERSION = (\d+)$", re.M)
RE_JSON_SCHEMA = re.compile(r'"schema_version":\s*(\d+)')


def tracer_schema(tracer):
    """The SCHEMA_VERSION a tracer writes, read from its source, or None."""
    try:
        with open(tracer, encoding="utf-8") as f:
            match = RE_TRACER_SCHEMA.search(f.read())
    except OSError:
        return None
    return int(match.group(1)) if match else None


def json_schema(path):
    """The schema_version at the head of a JSON, or None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            match = RE_JSON_SCHEMA.search(f.read(JSON_HEAD_BYTES))
    except OSError:
        return None
    return int(match.group(1)) if match else None


def stale_reason(source, out_json, tracer, schema):
    """Why out_json needs converting from source, or None when it is up to
    date: missing, older than its input or than the tracer, or written at
    another schema than the tracer's."""
    if not os.path.isfile(out_json):
        return "missing"
    made = os.path.getmtime(out_json)
    if made < os.path.getmtime(source):
        return "older than its input"
    if made < os.path.getmtime(tracer):
        return "older than the tracer"
    if json_schema(out_json) != schema:
        return "at another schema"
    return None

# SHARED END py-json-staleness


def stem(path):
    """daxpy.config1.vcd -> daxpy.config1, with its folder."""
    return path[:-len(VCD_EXT)] if path.endswith(VCD_EXT) else path


def json_for(path):
    return stem(path) + ".json"


def list_for(path):
    """The objdump listing that belongs to a VCD."""
    return stem(path) + ".list"


def find_swept_pkg(explicit):
    """The swept package a sweep VCD's configuration is named from: the one
    given, or the first SWEPT_PKG in the repository's configs/, beside this
    script, in the working directory or in a container's CVA6_configs/."""
    if explicit:
        return explicit
    above = os.path.dirname(HERE)
    for candidate in (os.path.join(REPO_ROOT, "configs", SWEPT_PKG),
                      os.path.join(HERE, SWEPT_PKG),
                      os.path.abspath(SWEPT_PKG),
                      os.path.join(above, "CVA6_configs", SWEPT_PKG),
                      os.path.join(os.curdir, "CVA6_configs", SWEPT_PKG)):
        if os.path.isfile(candidate):
            return candidate
    return None


def read_cfg_names(pkg):
    """{CFG id: CFG_ name} from a swept package, empty when unreadable."""
    try:
        with open(pkg, encoding="utf-8") as f:
            text = f.read()
    except (OSError, TypeError):
        return {}
    return {int(number): name for name, number in RE_CFG_ID.findall(text)}


def config_name_for(vcd, given, cfg_names):
    """The --config-name to pass for a VCD: the one given, else a sweep
    VCD's CFG_ name, else None, which leaves the tracer's default."""
    if given:
        return given
    match = RE_SWEEP_STEM.search(os.path.basename(stem(vcd)))
    return cfg_names.get(int(match.group(1))) if match else None


def run_one(vcd, out_json, quiet, strict, config_name):
    """Convert one VCD, returning its outcome, ok, degraded or failed, and
    the line that reports it."""
    name = os.path.basename(out_json)
    cmd = [sys.executable, TRACER, vcd, "--disasm-list", list_for(vcd),
           "-o", out_json]
    if config_name:
        cmd += ["--config-name", config_name]
    if quiet:
        cmd.append("--quiet")
    if strict:
        cmd.append("--strict")
    print(f"[INFO] Converting {os.path.basename(vcd)} to {name}", flush=True)
    start = time.time()
    # Output is not captured, so with -j 1 the tracer's progress line shows a
    # VCD that takes minutes is not hung, and its warnings reach the log.
    code = subprocess.run(cmd).returncode
    took = time.time() - start
    if code == 3:
        # The tracer's strict exit. The JSON was still written, so say what
        # happened rather than implying the conversion produced nothing.
        return "degraded", (f"[WARN] {name} written, but the VCD is "
                            f"degraded (exit 3, see metadata.degraded).")
    if code != 0:
        return "failed", f"[ERROR] {name} failed with exit code {code}."
    return "ok", (f"[INFO] {name} written "
                  f"({human(os.path.getsize(out_json))}, {took:.0f}s)")


def main():
    parser = argparse.ArgumentParser(
        description="Run CVA6Flow_tracer.py over every Verilator VCD in a "
                    "folder.")
    parser.add_argument("folder", nargs="?", default=REPO_ROOT,
                        help="Folder holding the VCDs, not searched "
                             "recursively. Defaults to the folder holding "
                             "CVA6Flow_tracer.py")
    parser.add_argument("-j", "--jobs", type=int, default=DEFAULT_WORKERS,
                        metavar="N",
                        help=f"VCDs to convert at a time. Defaults to "
                             f"{DEFAULT_WORKERS}. Each holds a whole VCD's "
                             f"state, so memory binds before cores do. With "
                             f"more than 1 the tracers run with --quiet, so "
                             f"their progress lines do not interleave")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="Print which VCDs would be converted to which "
                             "JSONs, and convert nothing")
    parser.add_argument("--force", action="store_true",
                        help="Convert a VCD even when its JSON is up to "
                             "date: at least as new as the VCD and the "
                             "tracer, and at the tracer's schema")
    parser.add_argument("--quiet", action="store_true",
                        help="Pass --quiet to the tracer, dropping its "
                             "progress line")
    parser.add_argument("--no-strict", action="store_true",
                        help="Do not pass --strict to the tracer. By default "
                             "a VCD missing a mechanism's signals, or a "
                             "truncated or empty VCD, counts as degraded, and "
                             "the batch ends with exit 3. The JSONs are "
                             "written either way")
    parser.add_argument("--config-name", metavar="NAME",
                        help="Pass --config-name NAME to the tracer for "
                             "every VCD. By default a sweep's "
                             "<test>.config<N>.vcd gets the CFG_ name of "
                             "configuration N in --config-pkg, and any other "
                             "VCD the tracer's default")
    parser.add_argument("--config-pkg", metavar="FILE",
                        help=f"The swept package a sweep VCD's configuration "
                             f"is named from. Defaults to {SWEPT_PKG} in the "
                             f"repository's configs/, beside this script, in "
                             f"the working directory or in CVA6_configs/")
    args = parser.parse_args()

    if not os.path.isfile(TRACER):
        print(f"[ERROR] No {TRACER}. This script runs inside the CVA6Flow "
              f"repository, beside its tracer.", file=sys.stderr)
        return 2
    if not os.path.isdir(args.folder):
        print(f"[ERROR] Folder not found: {args.folder}", file=sys.stderr)
        return 2

    folder = os.path.abspath(args.folder)
    vcds = sorted(os.path.join(folder, f) for f in os.listdir(folder)
                  if f.endswith(VCD_EXT))
    if not vcds:
        print(f"[INFO] No *{VCD_EXT} files in {folder}")
        return 0

    schema = tracer_schema(TRACER)
    pkg = find_swept_pkg(args.config_pkg)
    cfg_names = read_cfg_names(pkg)
    todo, skipped, no_list, unnamed = [], [], [], []
    redone = {}
    for vcd in vcds:
        out_json = json_for(vcd)
        reason = stale_reason(vcd, out_json, TRACER, schema)
        config_name = config_name_for(vcd, args.config_name, cfg_names)
        if not os.path.isfile(list_for(vcd)):
            no_list.append(os.path.basename(vcd))
        elif not args.force and reason is None:
            skipped.append(os.path.basename(out_json))
        else:
            todo.append((vcd, out_json, config_name))
            if reason not in (None, "missing"):
                redone[reason] = redone.get(reason, 0) + 1
            if (config_name is None
                    and RE_SWEEP_STEM.search(os.path.basename(stem(vcd)))):
                unnamed.append(os.path.basename(vcd))

    if no_list:
        print(f"[WARN] {len(no_list)} VCD(s) have no .list beside them, so "
              f"the viewer would refuse their JSONs. Skipped, and the batch "
              f"exits 1: {', '.join(no_list)}")
    if skipped:
        print(f"[INFO] {len(skipped)} JSON(s) already up to date, use --force "
              f"to redo them: {', '.join(skipped)}")
    if redone:
        print(f"[INFO] {sum(redone.values())} JSON(s) out of date: "
              + ", ".join(f"{count} {reason}"
                          for reason, count in sorted(redone.items())))
    if unnamed:
        print(f"[WARN] {len(unnamed)} sweep VCD(s) name a configuration "
              f"{pkg or SWEPT_PKG} does not hold, so their JSONs carry the "
              f"tracer's default name. --config-pkg names the package: "
              f"{', '.join(unnamed)}")
    if not todo:
        return 1 if no_list else 0
    if args.dry_run:
        print(f"[INFO] Would convert {len(todo)} VCD(s) from {folder}, "
              f"{args.jobs} at a time:")
        for vcd, out_json, config_name in todo:
            named = f", {config_name}" if config_name else ""
            print(f"[INFO]   {os.path.basename(vcd)} "
                  f"({human(os.path.getsize(vcd))}) -> "
                  f"{os.path.basename(out_json)}{named}")
        return 1 if no_list else 0

    jobs = max(1, args.jobs)
    print(f"[INFO] Converting {len(todo)} VCD(s) from {folder}, {jobs} at a "
          f"time\n")
    failed = degraded = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(run_one, vcd, out_json,
                               args.quiet or jobs > 1, not args.no_strict,
                               config_name)
                   for vcd, out_json, config_name in todo]
        for future in as_completed(futures):
            outcome, line = future.result()
            failed += outcome == "failed"
            degraded += outcome == "degraded"
            print(line, file=sys.stderr if outcome == "failed"
                  else sys.stdout, flush=True)

    print(f"\n[INFO] {len(todo) - failed - degraded} of {len(todo)} "
          f"converted cleanly")
    if degraded:
        print(f"[WARN] {degraded} JSON(s) written but degraded. "
              f"metadata.degraded in each says what is missing.")
    # 3 is the tracer's own code for degraded, kept apart from 1 so a caller
    # can tell a run where nothing failed from one where something did. A
    # VCD skipped for want of its listing counts as failed.
    if failed or no_list:
        return 1
    return 3 if degraded else 0


if __name__ == "__main__":
    sys.exit(main())
