# Configurations

The two CVA6 configuration packages the viewer was developed on. Both describe `cv64a6_imafdc_sv39_hpdcache_wb`, the build the tracer's constants are written for.

| File                                                    | What it is                                                                                                                                                                                                  |
| ------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cv64a6_imafdc_sv39_hpdcache_wb_config_pkg.sv`          | The upstream package, unmodified, kept for comparison                                                                                                                                                       |
| `cv64a6_imafdc_sv39_hpdcache_wb_config_CVA6Flow_pkg.sv` | The swept package: the same core with a table of seventeen configuration cuts and one selector, `CVA6_CONFIG_SEL`. `CFG_BASELINE` elaborates the upstream core, and its notice at the top says what changed |

## How the sweep uses them

`scripts/run_CVA6Flow_sweep.py` reads the table from the swept package, writes the package over `core/include/cv64a6_imafdc_sv39_hpdcache_wb_config_pkg.sv` under the CVA6 root with the selector set to each configuration in turn, runs that configuration's workloads, and puts the live package back when the sweep ends. Each row of the table names the parameter it cuts and the workload chosen to exercise the cut, which is what the sweep runs for it.

A run of one configuration by hand is the same edit: copy the swept package over the live one, set `CVA6_CONFIG_SEL`, and pass the configuration's name to the tracer with `--config-name`, since a VCD does not name the build it came from.

The tracer reads the scoreboard depth, the commit and writeback ports and the D-cache set count from the VCD itself, so a JSON from a smaller configuration reports its own sizes in `config_params`. A configuration larger than the tracer's constants is refused with the constant to raise.
