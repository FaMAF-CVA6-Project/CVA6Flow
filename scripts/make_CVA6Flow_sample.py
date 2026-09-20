#!/usr/bin/env python3
"""Build a sample JSON for the viewer's "Load sample" button.

Keeps a run of records from a full tracer JSON, clips every event array to
the cycles those records span, by the rule the viewer's streamed loader
follows, and writes both forms: the .js the page loads with a script tag,
served or opened from disk, and the .json for every other reader. The sample
is then listed in samples.js beside it, the manifest the page reads from
tests/. Samples are generated, not committed.

    python3 scripts/make_CVA6Flow_sample.py tests/daxpy.json
    python3 scripts/make_CVA6Flow_sample.py tests/daxpy.json -n 1500
    python3 scripts/make_CVA6Flow_sample.py daxpy.json --from 4000 -n 2000
    python3 scripts/make_CVA6Flow_sample.py daxpy.json -o tests/daxpy.loop
"""
import argparse
import json
import os
import re
import sys

TOOL = "cva6flow_tracer"
SCHEMA_VERSION = 3

# Small enough to open at once, long enough to show a miss and a mispredict.
DEFAULT_COUNT = 3000

# The page whose MAX_STREAM_INSTRUCTIONS a sample stays within, so a sample
# never needs its range.
PAGE = "CVA6Flow.html"


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


# SHARED BEGIN py-json-clip

# Needs: py-json-fields

# The pages keep the same margin around a streamed range, so a sample and
# a ranged load of the same records carry the same events.
EVENT_MARGIN_CYCLES = 64


def path_set(obj, dotted, value):
    *parents, last = dotted.split(".")
    for key in parents:
        obj = obj[key]
    obj[last] = value


def clip_json(data, first, last, source):
    """Keep records first to last (1-based), in place, without their null
    keys. When that is a slice, keep the events whose cycles overlap the
    records' span widened by EVENT_MARGIN_CYCLES, and set metadata.clipped,
    counted from the first source when data is already a slice. Returns
    (span or None, whether it is a slice)."""
    metadata = data["metadata"]
    total = len(data["instructions"])
    data["instructions"] = [without_nulls(rec) for rec
                            in data["instructions"][first - 1:last]]
    span = record_span(data["instructions"], metadata)
    sliced = first > 1 or last < total
    lo = span[0] - EVENT_MARGIN_CYCLES if span else 0
    hi = span[1] + EVENT_MARGIN_CYCLES if span else -1
    masks = {}
    for path, where in metadata["event_fields"].items():
        elements = path_get(data, path)
        mask = []
        for element in elements:
            cycles = event_cycles(element, where)
            mask.append(not sliced or not cycles
                        or (max(cycles) >= lo and min(cycles) <= hi))
        masks[path] = mask
        path_set(data, path, [
            without_nulls(element) if isinstance(element, dict) else element
            for element, keep in zip(elements, mask) if keep])
    for twin, base in metadata["event_twin_fields"].items():
        elements = path_get(data, twin)
        path_set(data, twin, [element for element, keep
                              in zip(elements, masks[base]) if keep])
    if sliced:
        earlier = metadata.get("clipped")
        offset = earlier["first_record"] - 1 if earlier else 0
        metadata["clipped"] = {
            "source": earlier["source"] if earlier else source,
            "first_record": offset + first,
            "last_record": offset + first + len(data["instructions"]) - 1,
            "n_records_source": (earlier["n_records_source"] if earlier
                                 else total),
        }
    return span, sliced

# SHARED END py-json-clip


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


# SHARED BEGIN py-sample-manifest

# Needs: json, os

JS_GLOBAL = "window.__SAMPLE_JSON__"
MANIFEST_GLOBAL = "window.__SAMPLE_MANIFEST__"
MANIFEST_FILE = "samples.js"
# A sample is meant to open at once, and past this it no longer does.
MAX_SAMPLE_BYTES = 40 * 1024 * 1024


def sample_label(out_base):
    """The manifest label, the output's base name without .sample, since the
    page names a sample with its label followed by (sample)."""
    label = os.path.basename(out_base)
    return label[:-len(".sample")] if label.endswith(".sample") else label


def write_sample(out_base, data):
    """Write <out_base>.json and <out_base>.js, returning both paths. The
    .js is what a script tag loads, served or from disk."""
    body = json.dumps(data, separators=(",", ":"))
    out_json, out_js = out_base + ".json", out_base + ".js"
    os.makedirs(os.path.dirname(os.path.abspath(out_json)), exist_ok=True)
    with open(out_json, "w") as f:
        f.write(body + "\n")
    with open(out_js, "w") as f:
        f.write(f"{JS_GLOBAL} = {body};\n")
    return out_json, out_js


def read_manifest(folder):
    """The entries of folder/samples.js, or an empty list without one."""
    path = os.path.join(folder, MANIFEST_FILE)
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        text = f.read().strip()
    prefix = MANIFEST_GLOBAL + " = "
    if not text.startswith(prefix) or not text.endswith(";"):
        raise ValueError(f"{path} is not a manifest this script wrote")
    return json.loads(text[len(prefix):-1])


def update_manifest(out_js, data, label):
    """Add or replace this sample in its folder's samples.js, dropping
    entries whose file is gone. Returns the manifest's path."""
    folder = os.path.dirname(os.path.abspath(out_js))
    name = os.path.basename(out_js)
    entries = [e for e in read_manifest(folder) if e["file"] != name
               and os.path.isfile(os.path.join(folder, e["file"]))]
    metadata = data["metadata"]
    entries.append({
        "file": name, "label": label, "tool": metadata["tool"],
        "schema_version": metadata["schema_version"],
        "n_records": len(data["instructions"]),
    })
    entries.sort(key=lambda e: e["file"])
    path = os.path.join(folder, MANIFEST_FILE)
    with open(path, "w") as f:
        f.write(f"{MANIFEST_GLOBAL} = {json.dumps(entries, indent=2)};\n")
    return path

# SHARED END py-sample-manifest


def default_out_base(source):
    """tests/<source name>.sample beside the page, which cannot overwrite
    the source JSON and is where the page looks."""
    stem = os.path.basename(source)
    if stem.endswith(".json"):
        stem = stem[:-len(".json")]
    return os.path.join(page_root(), "tests", stem + ".sample")


def main():
    parser = argparse.ArgumentParser(
        description="Trim a CVA6Flow tracer JSON into the viewer's sample "
                    "JSON, writing both the .json and the .js form and "
                    "listing it in tests/samples.js.")
    parser.add_argument(
        "source", help="A full tracer JSON, such as tests/daxpy.json")
    parser.add_argument(
        "-o", "--out", metavar="PATH",
        help="Output path without its extension. Defaults to "
             "tests/<source name>.sample beside the page")
    parser.add_argument(
        "-n", "--instructions", type=int, default=DEFAULT_COUNT, metavar="N",
        help=f"How many records to keep. Defaults to {DEFAULT_COUNT}, and 0 "
             f"keeps every record from --from on. At most the page's "
             f"MAX_STREAM_INSTRUCTIONS, what the viewer renders at once")
    parser.add_argument(
        "--from", dest="start", type=int, default=1, metavar="N",
        help="Record number of the first record to keep, counting from 1 "
             "as the viewer's range does, so a sample can start past "
             "the program's set-up")
    args = parser.parse_args()

    if args.instructions < 0:
        print(f"[ERROR] -n {args.instructions} is negative. Give a count, "
              f"or 0 for every record.", file=sys.stderr)
        return 1
    limits = read_page_limits()
    if limits is None:
        print(f"[ERROR] Could not read MAX_STREAM_INSTRUCTIONS from {PAGE} in "
              f"{page_root()}.", file=sys.stderr)
        return 1
    max_records = limits[1]
    if not os.path.isfile(args.source):
        print(f"[ERROR] {args.source} does not exist.", file=sys.stderr)
        return 1
    print(f"[INFO] Reading {args.source} "
          f"({human(os.path.getsize(args.source))})")
    try:
        data = load_tracer_json(args.source, TOOL, SCHEMA_VERSION)
    except ValueError as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        return 1

    total = len(data["instructions"])
    if not 1 <= args.start <= total:
        print(f"[ERROR] --from {args.start} is outside the {total:,} "
              f"records, which run from 1 to {total:,}.", file=sys.stderr)
        return 1
    last = total if args.instructions == 0 else min(
        total, args.start + args.instructions - 1)
    if last - args.start + 1 > max_records:
        print(f"[ERROR] That keeps {last - args.start + 1:,} records, more "
              f"than the {max_records:,} the viewer renders at once. Give a "
              f"smaller -n.", file=sys.stderr)
        return 1

    span, _sliced = clip_json(data, args.start, last,
                              os.path.basename(args.source))
    where = (f"cycles {span[0]:,} to {span[1]:,}" if span
             else "which carry no cycle")
    print(f"[INFO] Kept records {args.start:,} to {last:,} of {total:,}, "
          f"{where}.")

    out_base = args.out or default_out_base(args.source)
    out_json, out_js = write_sample(out_base, data)
    size = os.path.getsize(out_json)
    print(f"[INFO] Wrote {out_json} ({human(size)})")
    print(f"[INFO] Wrote {out_js} ({human(os.path.getsize(out_js))})")
    label = sample_label(out_base)
    manifest = update_manifest(out_js, data, label)
    print(f"[INFO] Listed {label} in {manifest}")
    tests = os.path.realpath(os.path.join(page_root(), "tests"))
    if os.path.realpath(os.path.dirname(manifest)) != tests:
        print(f"[WARN] The page reads the manifest in tests/ only, so it "
              f"does not offer a sample listed in {manifest}.")
    if size > MAX_SAMPLE_BYTES:
        print(f"[WARN] {human(size)} is past {human(MAX_SAMPLE_BYTES)}, and "
              f"a sample is meant to open at once. Try a smaller -n.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
