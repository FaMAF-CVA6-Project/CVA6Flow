#!/usr/bin/env python3
"""Turn every Verilator VCD in a folder into a CVA6Flow viewer JSON.

Each VCD needs the objdump listing of the same test beside it, since that is
where the instruction text comes from. A VCD without one is skipped and named
rather than converted into a JSON whose instruction column would be blank.

    python3 CVA6Flow_create_all_jsons.py            # this script's folder
    python3 CVA6Flow_create_all_jsons.py ../run_results
    python3 CVA6Flow_create_all_jsons.py -j 8
    python3 CVA6Flow_create_all_jsons.py --force    # redo existing JSONs
"""
import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# The tracer, resolved from this file rather than from the working directory,
# so the script runs from anywhere.
HERE = os.path.dirname(os.path.abspath(__file__))
TRACER = os.path.join(HERE, os.pardir, "CVA6Flow_tracer.py")

DEFAULT_WORKERS = 4

TRACE_END = ".vcd"


def json_for(path):
    """daxpy.config1.vcd -> daxpy.config1.json"""
    base = path[:-len(TRACE_END)] if path.endswith(TRACE_END) else path
    return base + ".json"


def list_for(path):
    """The objdump listing that belongs to a VCD."""
    base = path[:-len(TRACE_END)] if path.endswith(TRACE_END) else path
    return base + ".list"


def run_one(trace, out_json, quiet):
    cmd = [sys.executable, TRACER, trace,
           "--disasm-list", list_for(trace), "-o", out_json]
    if quiet:
        cmd.append("--quiet")
    start = time.time()
    code = subprocess.run(cmd).returncode
    took = time.time() - start
    name = os.path.basename(out_json)
    if code != 0:
        return f"[ERROR]   {name} failed with exit code {code}"
    size = os.path.getsize(out_json) / (1024 * 1024)
    return f"[SUCCESS] {name} ({size:.1f} MB, {took:.0f}s)"


def main():
    parser = argparse.ArgumentParser(
        description="Run CVA6Flow_tracer.py over every Verilator VCD in a "
                    "folder.")
    parser.add_argument("folder", nargs="?", default=HERE,
                        help="Folder holding the VCDs. Defaults to the one "
                             "this script sits in")
    parser.add_argument("-j", "--jobs", type=int, default=DEFAULT_WORKERS,
                        metavar="N",
                        help=f"VCDs to convert at a time. Defaults to "
                             f"{DEFAULT_WORKERS}. Each holds a whole trace's "
                             f"state, so memory binds before cores do")
    parser.add_argument("--force", action="store_true",
                        help="Convert a trace even when its JSON already "
                             "exists and is newer")
    parser.add_argument("--quiet", action="store_true",
                        help="Pass --quiet to the tracer, dropping its "
                             "progress line")
    args = parser.parse_args()

    if not os.path.isfile(TRACER):
        print(f"[ERROR] Tracer not found at {TRACER}. This script expects to "
              f"sit in tests/ inside the CVA6Flow repository.")
        return 2
    if not os.path.isdir(args.folder):
        print(f"[ERROR] {args.folder} is not a folder")
        return 2

    folder = os.path.abspath(args.folder)
    traces = sorted(os.path.join(folder, f) for f in os.listdir(folder)
                    if f.endswith(TRACE_END))
    if not traces:
        print(f"[INFO] No *{TRACE_END} files in {folder}")
        return 0

    todo, skipped, no_list = [], [], []
    for trace in traces:
        out_json = json_for(trace)
        if not os.path.isfile(list_for(trace)):
            no_list.append(os.path.basename(trace))
        elif (not args.force and os.path.isfile(out_json)
                and os.path.getmtime(out_json) >= os.path.getmtime(trace)):
            skipped.append(os.path.basename(out_json))
        else:
            todo.append((trace, out_json))

    if no_list:
        print(f"[WARN] {len(no_list)} VCD(s) have no .list beside them, so "
              f"their instruction column would be blank. Skipped: "
              f"{', '.join(no_list)}")
    if skipped:
        print(f"[INFO] {len(skipped)} JSON(s) already up to date, use --force "
              f"to redo them: {', '.join(skipped)}")
    if not todo:
        return 0

    print(f"[INFO] Converting {len(todo)} VCD(s) from {folder}")
    print(f"[INFO] {args.jobs} at a time\n")

    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [pool.submit(run_one, t, j, args.quiet) for t, j in todo]
        for future in as_completed(futures):
            line = future.result()
            failed += line.startswith("[ERROR]")
            print(line)

    print(f"\n[INFO] {len(todo) - failed} of {len(todo)} converted")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
