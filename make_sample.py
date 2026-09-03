"""Build the sample trace the viewer's "Load sample" button loads.

Writes both forms: the .json a served page fetches, and the .js a file:// page
loads with a script tag. Trimmed so the sample is small enough to commit, with
the cycle-keyed arrays clipped to the span the kept instructions cover.

    python3 make_sample.py daxpy.json
    python3 make_sample.py daxpy.json -n 1500
    python3 make_sample.py daxpy.json --from 4000 -n 2000
    python3 make_sample.py daxpy.json -o tests/daxpy.config1
"""
import argparse
import json
import os
import sys

# What MinorFlow.html asks for, without its extension.
DEFAULT_OUT = os.path.join("tests", "daxpy.config1")

# Small enough to commit, long enough to show a miss and a mispredict.
DEFAULT_COUNT = 3000

# The global the .js form has to define, since a script tag cannot return one.
JS_GLOBAL = "window.__SAMPLE_TRACE__"

# Cycle-typed record fields, used to find the span a slice covers.
CYCLE_FIELDS = (
    "if1_lo", "if2_lo", "if1_hi", "if2_hi",
    "fe_cycle", "id_cycle", "is_cycle", "ex_cycle", "wb_cycle", "co_cycle",
    "lsu_admit_cycle", "lsu_complete_cycle", "bp_resolution_cycle",
)

# Plain ascending cycle lists at the top level.
CYCLE_LISTS = ("ic_access_cycles", "dc_access_cycles", "icache_miss_cycles")

# Top-level event lists of objects, each with the field naming its cycle.
EVENT_LISTS = {
    "writebacks": "cycle",
    "dcache_alloc_events": "cycle",
    "icache_events": "fe1",
}


def cycle_span(records):
    """First and last cycle the records touch, inclusive, or None."""
    lo = hi = None
    for rec in records:
        for key in CYCLE_FIELDS:
            value = rec.get(key)
            if not isinstance(value, (int, float)):
                continue
            lo = value if lo is None or value < lo else lo
            hi = value if hi is None or value > hi else hi
    return (lo, hi) if lo is not None else None


def clip(data, span):
    """Drop every event outside the span, in place."""
    if span is None:
        return
    lo, hi = span

    for key in CYCLE_LISTS:
        if isinstance(data.get(key), list):
            data[key] = [c for c in data[key] if lo <= c <= hi]

    for key, field in EVENT_LISTS.items():
        entries = data.get(key)
        if not isinstance(entries, list):
            continue
        data[key] = [e for e in entries
                     if not isinstance(e, dict)
                     or not isinstance(e.get(field), (int, float))
                     or lo <= e[field] <= hi]


def human(size):
    for unit in ("B", "KiB", "MiB"):
        if size < 1024 or unit == "MiB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024


def main():
    parser = argparse.ArgumentParser(
        description="Trim a CVA6Flow tracer JSON into the viewer's sample "
                    "trace, writing both the .json and the .js form.")
    parser.add_argument("source", help="A full tracer JSON, such as daxpy.json")
    parser.add_argument("-o", "--out", default=DEFAULT_OUT, metavar="PATH",
                        help=f"Output path without its extension. Defaults to "
                             f"{DEFAULT_OUT}, which is what the viewer's "
                             f"button looks for")
    parser.add_argument("-n", "--instructions", type=int,
                        default=DEFAULT_COUNT, metavar="N",
                        help=f"How many instruction records to keep. Defaults "
                             f"to {DEFAULT_COUNT}. 0 keeps them all")
    parser.add_argument("--from", dest="start", type=int, default=0,
                        metavar="N",
                        help="Index of the first record to keep, so a sample "
                             "can start past the program's set-up")
    args = parser.parse_args()

    if not os.path.isfile(args.source):
        print(f"[ERROR] {args.source} does not exist")
        sys.exit(1)

    print(f"[INFO] Reading {args.source} "
          f"({human(os.path.getsize(args.source))})")
    with open(args.source) as f:
        data = json.load(f)

    records = data.get("instructions")
    if not isinstance(records, list) or not records:
        print(f"[ERROR] {args.source} has no 'instructions' array, so it is "
              f"not a tracer JSON")
        sys.exit(1)

    start = max(0, min(args.start, len(records) - 1))
    end = len(records) if args.instructions <= 0 else start + args.instructions
    data["instructions"] = records[start:end]
    kept = len(data["instructions"])
    if not kept:
        print(f"[ERROR] --from {args.start} is past the last of "
              f"{len(records)} records")
        sys.exit(1)

    span = cycle_span(data["instructions"])
    clip(data, span)
    if span:
        print(f"[INFO] Kept {kept:,} of {len(records):,} records, "
              f"cycles {span[0]:,} to {span[1]:,}")

    out_json = args.out + ".json"
    out_js = args.out + ".js"
    directory = os.path.dirname(os.path.abspath(out_json))
    os.makedirs(directory, exist_ok=True)

    body = json.dumps(data, separators=(",", ":"))
    with open(out_json, "w") as f:
        f.write(body)
    with open(out_js, "w") as f:
        f.write(f"{JS_GLOBAL} = {body};\n")

    print(f"[INFO] Wrote {out_json} ({human(os.path.getsize(out_json))})")
    print(f"[INFO] Wrote {out_js} ({human(os.path.getsize(out_js))})")
    if os.path.getsize(out_json) > 40 * 1024 * 1024:
        print("[WARN] Over 40 MiB. GitHub warns at 50 and refuses at 100, and "
              "a sample is meant to open instantly. Try a smaller -n.")


if __name__ == "__main__":
    main()
