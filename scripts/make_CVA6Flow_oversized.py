#!/usr/bin/env python3
"""Build a trace too large for the viewer to load, to exercise its refusal.

CVA6Flow.html gives up in two ways, and a test file should cross both:

    MAX_JSON_BYTES          500 MiB, above which the file is refused outright
    MAX_STREAM_INSTRUCTIONS 500,000 records, the cap on the object graph

The output is a real tracer JSON, not a fabricated one. A seed trace is read
once and its records are repeated with every cycle field shifted forward, so
what the viewer refuses is the size, never the shape.

    python3 scripts/make_CVA6Flow_oversized.py tests/daxpy.json
    python3 scripts/make_CVA6Flow_oversized.py tests/daxpy.json -n 600000
    python3 scripts/make_CVA6Flow_oversized.py tests/daxpy.json --mib 700
    python3 scripts/make_CVA6Flow_oversized.py tests/daxpy.json -o /tmp/huge.json
"""
import argparse
import json
import os
import sys

# Past both of the viewer's limits, so neither can be the one that did not
# fire. 600,000 records of a real trace run past 700 MB.
DEFAULT_INSTRUCTIONS = 600_000

# What CVA6Flow.html itself checks, quoted here so the message can say which
# limit a given size crosses.
MAX_JSON_BYTES = 500 * 1024 * 1024
MAX_STREAM_INSTRUCTIONS = 500_000

# Cycle-typed record fields, shifted per repetition so the copies do not all
# sit on top of each other. The same list make_CVA6Flow_sample.py clips by.
CYCLE_FIELDS = (
    "if1_lo", "if2_lo", "if1_hi", "if2_hi",
    "fe_cycle", "id_cycle", "is_cycle", "ex_cycle", "wb_cycle", "co_cycle",
    "lsu_admit_cycle", "lsu_complete_cycle", "bp_resolution_cycle",
)
CYCLE_LIST_FIELDS = ()


def human(size):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024


def span_of(records):
    """The highest cycle any record mentions, so a repetition can start after
    it rather than overlapping the one before."""
    top = 0
    for record in records:
        for key in CYCLE_FIELDS:
            value = record.get(key)
            if isinstance(value, int) and value > top:
                top = value
        for key in CYCLE_LIST_FIELDS:
            for value in record.get(key) or ():
                if isinstance(value, int) and value > top:
                    top = value
    return top


def shifted(record, seq, cycles):
    """One record moved forward in time and renumbered."""
    out = dict(record)
    out["id"] = seq
    for key in CYCLE_FIELDS:
        value = out.get(key)
        if isinstance(value, int):
            out[key] = value + cycles
    for key in CYCLE_LIST_FIELDS:
        value = out.get(key)
        if isinstance(value, list):
            out[key] = [v + cycles if isinstance(v, int) else v
                        for v in value]
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Repeat a tracer JSON until it is past what CVA6Flow can "
                    "load, to test the refusal path.")
    parser.add_argument("seed",
                        help="A real tracer JSON to repeat, such as "
                             "tests/daxpy.json")
    parser.add_argument("-o", "--out", default="oversized.json",
                        metavar="PATH",
                        help="Where to write. Defaults to oversized.json in "
                             "the working directory")
    parser.add_argument("-n", "--instructions", type=int,
                        default=DEFAULT_INSTRUCTIONS, metavar="N",
                        help=f"How many records to write. Defaults to "
                             f"{DEFAULT_INSTRUCTIONS:,}, past the viewer's "
                             f"{MAX_STREAM_INSTRUCTIONS:,}")
    parser.add_argument("--mib", type=int, default=None, metavar="N",
                        help="Keep going until the file is at least this many "
                             "MiB, whatever -n says")
    args = parser.parse_args()

    if not os.path.isfile(args.seed):
        print(f"[ERROR] {args.seed} does not exist")
        return 1
    print(f"[INFO] Reading {args.seed} ({human(os.path.getsize(args.seed))})")
    with open(args.seed) as handle:
        data = json.load(handle)

    records = data.get("instructions")
    if not isinstance(records, list) or not records:
        print(f"[ERROR] {args.seed} has no 'instructions' array, so it is not "
              f"a CVA6Flow tracer JSON")
        return 1

    stride = span_of(records) + 1
    target_bytes = (args.mib * 1024 * 1024) if args.mib else 0
    print(f"[INFO] Seed has {len(records):,} records over {stride:,} cycles")
    print(f"[INFO] Writing {args.out}")

    # Streamed a record at a time. Holding 600,000 of them would cost more
    # memory than the file itself.
    written = 0
    with open(args.out, "w") as out:
        out.write("{")
        for key, value in data.items():
            if key == "instructions":
                continue
            out.write(json.dumps(key) + ":" + json.dumps(value) + ",")
        out.write('"instructions":[')
        repetition = 0
        while True:
            for record in records:
                if written and (written >= args.instructions
                                and (not target_bytes
                                     or out.tell() >= target_bytes)):
                    break
                if written:
                    out.write(",")
                out.write(json.dumps(shifted(record, written,
                                             repetition * stride),
                                     separators=(",", ":")))
                written += 1
            else:
                repetition += 1
                if repetition % 5 == 0:
                    print(f"[INFO]   {written:,} records, "
                          f"{human(out.tell())}")
                continue
            break
        out.write("]}")

    size = os.path.getsize(args.out)
    print(f"[INFO] Wrote {args.out}: {written:,} records, {human(size)}")
    crossed = []
    if size > MAX_JSON_BYTES:
        crossed.append(f"MAX_JSON_BYTES ({human(MAX_JSON_BYTES)})")
    if written > MAX_STREAM_INSTRUCTIONS:
        crossed.append(f"MAX_STREAM_INSTRUCTIONS "
                       f"({MAX_STREAM_INSTRUCTIONS:,})")
    if crossed:
        print(f"[INFO] Past {' and '.join(crossed)}, so CVA6Flow.html should "
              f"refuse it and offer the range prompt")
    else:
        print("[WARN] Under both limits, so the viewer will load it. Raise "
              "-n or --mib.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
