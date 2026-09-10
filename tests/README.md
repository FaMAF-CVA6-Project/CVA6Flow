# tests/

Trace JSONs, and the sample the viewer loads on its own.

Nothing here is tracked. The traces are tens to hundreds of megabytes each, so this directory is generated rather than committed, and a fresh clone finds only this file. That is deliberate, and it is why the README links to `tests/` resolve.

## What lands here

| File                                     | Made by                                                                                                                            |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `<name>.vcd`, `<name>.list`              | `scripts/run_CVA6.py`, copied from the run's output folder                                                                         |
| `<name>.json`                            | `CVA6Flow_tracer.py`, or `scripts/create_all_CVA6Flow_jsons.py` over a folder of VCDs                                              |
| `daxpy.config1.json`, `daxpy.config1.js` | `scripts/make_CVA6Flow_sample.py`. This pair is the sample `CVA6Flow.html` loads when opened with no file, under exactly this name |
| `oversized*.json`                        | `scripts/make_CVA6Flow_oversized.py`, for testing the viewer's record ceiling                                                      |

## Filling it

```bash
python3 scripts/run_CVA6.py benchmarks/daxpy.S
python3 CVA6Flow_tracer.py tests/daxpy.vcd --disasm-list tests/daxpy.list -o tests/daxpy.json
python3 scripts/make_CVA6Flow_sample.py tests/daxpy.json -o tests/daxpy.config1
```

Add `--strict` to the tracer in a batch run: it exits non-zero when the dump was truncated or a mechanism did not resolve, and `metadata.degraded` in the JSON says which.
