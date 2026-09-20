#!/usr/bin/env python3
"""Build a JSON past both of the viewer's limits, to exercise its large-JSON
paths.

CVA6Flow.html has two limits, and a test JSON should cross both:

    MAX_JSON_BYTES          past which it counts the records first
    MAX_STREAM_INSTRUCTIONS past which it offers a range

Both are read from the page's limits block when this runs, so the closing
message always names the page's own values.

A seed JSON is read once and repeated. Repetition k moves every cycle by k
times the seed's cycle span and every id and id reference by k times the
seed's largest id, and repeats the event arrays with the records, so each
repetition reads like the seed and the viewer meets the size, never an
unusual shape. metadata.stats keeps the seed's counters, apart from
n_records.

    python3 scripts/make_CVA6Flow_oversized.py tests/daxpy.json
    python3 scripts/make_CVA6Flow_oversized.py tests/daxpy.json -n 600000
    python3 scripts/make_CVA6Flow_oversized.py tests/daxpy.json --mib 700
    python3 scripts/make_CVA6Flow_oversized.py tests/daxpy.json -o huge.json
"""
import argparse
import itertools
import json
import os
import re
import sys

TOOL = "cva6flow_tracer"
SCHEMA_VERSION = 3

# Without -n or --mib the JSON passes both of the page's limits by this
# factor, so neither can be the one that did not fire.
DEFAULT_LIMIT_FACTOR = 1.2

PROGRESS_EVERY_REPETITIONS = 5

# The page whose limits the JSON has to cross.
PAGE = "CVA6Flow.html"

# The tracer's key order, which the viewer's streamed loader insists on.
TOP_LEVEL_ORDER = (
    "metadata", "config_params", "instructions", "ic_events", "dc_events",
    "mem_writebacks",
)
EVENT_OBJECTS = ("ic_events", "dc_events", "mem_writebacks")


# SHARED BEGIN py-repo-root

# Needs: os


def repo_root():
    """The nearest folder above this script holding a .git, so a moved tree
    needs no parent count fixed. Without one, as in a release archive, the
    parent of the script's folder, which is the documented layout."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = here
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return os.path.dirname(here)
        path = parent

# SHARED END py-repo-root


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


# SHARED BEGIN py-json-fields

# Needs: json


def load_tracer_json(path, tool, schema):
    """The JSON at path, or ValueError saying why it is not the tracer's."""
    with open(path) as f:
        data = json.load(f)
    metadata = data.get("metadata") if isinstance(data, dict) else None
    if not isinstance(metadata, dict) or metadata.get("tool") != tool:
        raise ValueError(f"{path} is not a JSON written by {tool}")
    if metadata.get("schema_version") != schema:
        raise ValueError(f"{path} is schema {metadata.get('schema_version')}"
                         f", and this script reads schema {schema}")
    if not data.get("instructions"):
        raise ValueError(f"{path} holds no instruction records")
    return data


def path_get(obj, dotted):
    for key in dotted.split("."):
        obj = obj[key]
    return obj


def without_nulls(row):
    """A record or an event object as the tracer writes it, which leaves out
    every key whose value is null, so a missing key reads as null."""
    return {key: value for key, value in row.items() if value is not None}


def record_span(records, metadata):
    """First and last cycle the records carry, inclusive, or None."""
    cycles = []
    for rec in records:
        cycles.extend(rec[key] for key in metadata["cycle_fields"]
                      if rec.get(key) is not None)
        for key in metadata["cycle_list_fields"]:
            cycles.extend(rec.get(key) or ())
    return (min(cycles), max(cycles)) if cycles else None


def event_cycles(element, where):
    """The non-null cycles of one event element, per its descriptor."""
    if where == "cycle":
        return [element]
    if isinstance(element, dict):
        return [element[key] for key in where
                if element.get(key) is not None]
    return [element[at] for at in where if element[at] is not None]

# SHARED END py-json-fields


# SHARED BEGIN py-json-shift

# Needs: py-json-fields


def seed_cycle_bounds(data):
    """Smallest and largest cycle in records and event arrays together."""
    metadata = data["metadata"]
    span = record_span(data["instructions"], metadata)
    cycles = list(span) if span else []
    for path, where in metadata["event_fields"].items():
        for element in path_get(data, path):
            cycles.extend(event_cycles(element, where))
    return (min(cycles), max(cycles)) if cycles else (0, 0)


def shift_cycles(element, where, offset):
    """A copy of one event element with every cycle moved by offset, and an
    object without its null keys."""
    if where == "cycle":
        return element + offset
    if isinstance(element, dict):
        shifted = without_nulls(element)
        for key in where:
            if key in shifted:
                shifted[key] += offset
        return shifted
    shifted = list(element)
    for at in where:
        if shifted[at] is not None:
            shifted[at] += offset
    return shifted


def shift_record(rec, metadata, cycle_offset, id_offset):
    """A copy of one record with its cycles and id references moved, and
    without its null keys."""
    shifted = without_nulls(rec)
    shifted["id"] += id_offset
    for key in metadata["cycle_fields"]:
        if key in shifted:
            shifted[key] += cycle_offset
    for key in metadata["cycle_list_fields"]:
        if key in shifted:
            shifted[key] = [c + cycle_offset for c in shifted[key]]
    for key in metadata["id_fields"]:
        if key in shifted:
            shifted[key] += id_offset
    for key in metadata["id_list_fields"]:
        if key in shifted:
            shifted[key] = [i + id_offset for i in shifted[key]]
    return shifted

# SHARED END py-json-shift


# SHARED BEGIN py-page-limits

# Needs: os, re, PAGE, py-repo-root

# The declarations of the page's limits block, a product of integers each.
LIMIT_PATTERN = r"\bconst {name} = ([\d_ *]+);"


def page_limit(text, name):
    """The value of one constant of the page's limits block, or None."""
    match = re.search(LIMIT_PATTERN.format(name=name), text)
    if not match:
        return None
    value = 1
    for factor in match.group(1).split("*"):
        value *= int(factor.strip())
    return value


def page_root():
    """The folder holding PAGE: the repository root in a clone, or the
    viewer's folder beside scripts/ in a container image, where the root
    holds gem5 or CVA6. The repository root when neither has the page."""
    root = repo_root()
    viewer = os.path.splitext(PAGE)[0]
    for folder in (root, os.path.join(root, viewer)):
        if os.path.isfile(os.path.join(folder, PAGE)):
            return folder
    return root


def read_page_limits():
    """(MAX_JSON_BYTES, MAX_STREAM_INSTRUCTIONS) from PAGE in page_root(),
    or None when the page or either constant is missing."""
    path = os.path.join(page_root(), PAGE)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        text = f.read()
    limits = (page_limit(text, "MAX_JSON_BYTES"),
              page_limit(text, "MAX_STREAM_INSTRUCTIONS"))
    return None if None in limits else limits

# SHARED END py-page-limits


def seed_steps(data):
    """(cycle_step, id_step) for one repetition of the seed."""
    low, high = seed_cycle_bounds(data)
    return high - low + 1, max(rec["id"] for rec in data["instructions"])


def shifted_records(data, steps):
    """Every record of every repetition, in order, as JSON text, with the
    repetition it belongs to. Endless: the caller decides where to stop."""
    metadata = data["metadata"]
    cycle_step, id_step = steps
    for repetition in itertools.count():
        for rec in data["instructions"]:
            out = shift_record(rec, metadata, repetition * cycle_step,
                               repetition * id_step)
            yield repetition, json.dumps(out, separators=(",", ":"))


def repeated_events(data, steps, repetitions):
    """Each event object with its arrays repeated once per repetition that
    was written, cycles moved like the records'."""
    event_fields = data["metadata"]["event_fields"]
    cycle_step = steps[0]

    def repeat(path, elements):
        where = event_fields.get(path)
        if where is None:
            return elements
        return [shift_cycles(element, where, k * cycle_step)
                for k in range(repetitions) for element in elements]

    events = {}
    for name in EVENT_OBJECTS:
        if isinstance(data[name], list):
            events[name] = repeat(name, data[name])
        else:
            events[name] = {key: repeat(f"{name}.{key}", elements)
                            for key, elements in data[name].items()}
    return events


def closing_message(size, written, limits):
    """Which path the viewer takes for this JSON, limit by limit."""
    max_bytes, max_records = limits
    over_bytes = size > max_bytes
    over_records = written > max_records
    if over_bytes and over_records:
        return (f"[INFO] Past MAX_JSON_BYTES ({human(max_bytes)}), so "
                f"CVA6Flow.html counts the records before reading, and past "
                f"MAX_STREAM_INSTRUCTIONS ({max_records:,}), so the count "
                f"ends in the range.")
    if over_bytes:
        return (f"[INFO] Past MAX_JSON_BYTES ({human(max_bytes)}) only, so "
                f"CVA6Flow.html counts the records and streams the JSON "
                f"whole. Raise -n for the range.")
    if over_records:
        return (f"[INFO] Past MAX_STREAM_INSTRUCTIONS ({max_records:,}) "
                f"only, so CVA6Flow.html reads the JSON whole and then offers "
                f"the range. Raise --mib for the record count.")
    return ("[WARN] Under both limits, so the viewer will load it whole. "
            "Raise -n or --mib.")


def write_oversized(out_path, data, steps, want_records, want_bytes):
    """Write the repetitions until both targets are met, and return (records
    written, repetitions begun). The records go to a side file first, since
    metadata comes first and its n_records is only known once the stop test
    has fired."""
    header = {key: data[key] for key in TOP_LEVEL_ORDER[:2]}
    fixed_bytes = len(json.dumps(header))
    records_path = out_path + ".records"
    written = written_bytes = repetitions = 0
    with open(records_path, "w") as side:
        for repetition, text in shifted_records(data, steps):
            if written and ((want_records is None or written >= want_records)
                            and fixed_bytes + written_bytes >= want_bytes):
                break
            if repetition == repetitions:
                repetitions += 1
                if repetitions % PROGRESS_EVERY_REPETITIONS == 0:
                    print(f"[INFO]   {written:,} records, "
                          f"{human(fixed_bytes + written_bytes)}")
            line = ("" if not written else ",\n") + text
            side.write(line)
            written += 1
            written_bytes += len(line)

    metadata = dict(data["metadata"], clipped=None,
                    stats=dict(data["metadata"]["stats"], n_records=written))
    values = dict(data, metadata=metadata,
                  **repeated_events(data, steps, repetitions))
    partial = out_path + ".partial"
    with open(partial, "w") as out:
        out.write("{\n")
        for key in TOP_LEVEL_ORDER:
            out.write(f"  {json.dumps(key)}: ")
            if key == "instructions":
                out.write("[\n")
                with open(records_path) as side:
                    for chunk in iter(lambda: side.read(1 << 20), ""):
                        out.write(chunk)
                out.write("\n  ]")
            else:
                out.write(json.dumps(values[key], separators=(",", ":")))
            out.write(",\n" if key != TOP_LEVEL_ORDER[-1] else "\n")
        out.write("}\n")
    os.replace(partial, out_path)
    return written, repetitions


def main():
    parser = argparse.ArgumentParser(
        description="Repeat a tracer JSON until it is past both of "
                    "CVA6Flow's limits, to test its large-JSON paths.")
    parser.add_argument("seed",
                        help="A tracer JSON to repeat, such as "
                             "tests/daxpy.json")
    parser.add_argument("-o", "--out", default="oversized.json",
                        metavar="PATH",
                        help="Where to write. Defaults to oversized.json in "
                             "the working directory")
    parser.add_argument("-n", "--instructions", type=int, metavar="N",
                        help="How many records to write, at least 1. Without "
                             "-n or --mib the JSON passes both the page's "
                             "MAX_STREAM_INSTRUCTIONS and its MAX_JSON_BYTES "
                             "by a fifth")
    parser.add_argument("--mib", type=int, metavar="N",
                        help="Keep writing until the JSON is at least this "
                             "many MiB. Given with -n both must be reached")
    args = parser.parse_args()

    if args.instructions is not None and args.instructions < 1:
        print(f"[ERROR] -n {args.instructions} writes no record. Give 1 or "
              f"more.", file=sys.stderr)
        return 1
    if args.mib is not None and args.mib < 1:
        print(f"[ERROR] --mib {args.mib} is below 1.", file=sys.stderr)
        return 1
    limits = read_page_limits()
    if limits is None:
        print(f"[ERROR] Could not read MAX_JSON_BYTES and "
              f"MAX_STREAM_INSTRUCTIONS from {PAGE} in {page_root()}.",
              file=sys.stderr)
        return 1
    want_records = args.instructions
    want_bytes = args.mib * 1024 * 1024 if args.mib is not None else 0
    if want_records is None and args.mib is None:
        want_bytes = int(limits[0] * DEFAULT_LIMIT_FACTOR)
        want_records = int(limits[1] * DEFAULT_LIMIT_FACTOR)

    if not os.path.isfile(args.seed):
        print(f"[ERROR] {args.seed} does not exist.", file=sys.stderr)
        return 1
    print(f"[INFO] Reading {args.seed} ({human(os.path.getsize(args.seed))})")
    try:
        data = load_tracer_json(args.seed, TOOL, SCHEMA_VERSION)
    except ValueError as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        return 1

    steps = seed_steps(data)
    print(f"[INFO] Seed has {len(data['instructions']):,} records over "
          f"{steps[0]:,} cycles.")
    print(f"[INFO] Writing {args.out}")

    try:
        written, repetitions = write_oversized(args.out, data, steps,
                                               want_records, want_bytes)
    finally:
        # An interrupted or failed write leaves neither side file behind.
        for leftover in (args.out + ".records", args.out + ".partial"):
            if os.path.exists(leftover):
                os.remove(leftover)

    size = os.path.getsize(args.out)
    print(f"[INFO] Wrote {args.out}: {written:,} records in {repetitions:,} "
          f"repetitions, {human(size)}")
    print(closing_message(size, written, limits))
    return 0


if __name__ == "__main__":
    sys.exit(main())
