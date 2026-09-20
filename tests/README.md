# tests/

Tracer JSONs, and the sample JSONs the viewer's Load sample button offers.

Only this file is committed, and it is kept so the folder exists in a fresh clone. VCDs run to gigabytes and their JSONs to hundreds of megabytes, so everything else here is generated, samples included.

The FaMAF CVA6 Project, which this viewer was written for, fills this folder while it builds its CVA6 image: a full sample of every program in `benchmarks/` whose run fits the 500,000 records the page renders at once, written with `-n 0` so each one is a whole run. In a clone the folder is empty until the samples are made, and the Load sample button appears only once `samples.js` lists one.

## What lands here

| File                                                   | Made by                                                                                                                                                                                       |
| ------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `<name>.vcd`, `<name>.list`                            | `scripts/run_CVA6.py`, which leaves them in `results/run/` under the CVA6 root, copied here by hand                                                                                           |
| `<name>.json`                                          | `CVA6Flow_tracer.py`, or `scripts/create_all_CVA6Flow_jsons.py` over a folder of VCDs                                                                                                         |
| `<name>.sample.js`, `<name>.sample.json`, `samples.js` | `scripts/make_CVA6Flow_sample.py`. The page reads the manifest `samples.js`, served or opened from disk, and offers every sample it lists that its own tracer wrote at its own schema version |
| `oversized*.json`                                      | `scripts/make_CVA6Flow_oversized.py -o tests/oversized.json`, for testing the viewer's byte and record limits. Without `-o` it writes to the working directory                                |

## Filling it

```bash
python3 scripts/run_CVA6.py benchmarks/daxpy.S
cp /CVA6/results/run/daxpy.vcd /CVA6/results/run/daxpy.list tests/   # /CVA6 is the CVA6 root in the image
python3 CVA6Flow_tracer.py tests/daxpy.vcd -o tests/daxpy.json
python3 scripts/make_CVA6Flow_sample.py tests/daxpy.json
```

Add `--strict` to the tracer in a batch run: it exits with 3 when a mechanism's signals are missing from the VCD, or the VCD was cut, holds no value changes, no rising edge, no instruction or no commit, or ends at a timestamp that disagrees with its cycle count, and `metadata.degraded` in the JSON says which.
