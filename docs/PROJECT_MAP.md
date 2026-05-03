# Capacity Lab Project Map

This workspace is organized by responsibility. The root directory is kept for
top-level entrypoints and config; implementation, data, results, and documents
live in dedicated folders.

```text
Capacity_lab/
+-- servers/                    Docker service implementations
|   +-- go_lognormal/           Go lognormal M/G/1 server
|   +-- apache_msg/             Apache/PHP messaging backend
|   +-- apache_dsp_aes/         Apache/PHP DSP-AES workload
|   +-- node_dsp_aes/           Node.js DSP-AES workload
|   +-- python_dsp_aes/         Python/Gunicorn DSP-AES workload
|   +-- java_dsp_aes/           Java ThreadPool DSP-AES workload
|   +-- go_sqlite/              Go SQLite workload
|
+-- analysis/
|   +-- des/                    DES engines, shared metrics, fitted models
|   +-- load/                   Poisson load generators
|   +-- orchestration/          Sweep and DES batch runners
|   +-- reporting/              CDF plots and generated experiment READMEs
|   +-- visualisation/          Browser visualiser and live backend
|
+-- data/
|   +-- experiments/            Raw traces, DES outputs, summaries, per-server READMEs
|   +-- live_logs/              Bind-mounted CSV logs written by running containers
|
+-- results/
|   +-- figures/                Cross-server figures used by README/thesis
|   +-- tables/                 Cross-server CSV outputs
|
+-- docs/
|   +-- PROJECT_MAP.md          This workspace map
|   +-- EXPERIMENTS.md          Main experiment log
|   +-- DIFFICULTIES.md         Research/debugging log
|   +-- latex/
|   |   +-- thesis/             LaTeX source and local figure assets
|   |   +-- exports/            Compiled PDFs and export zips
|   +-- references/             Papers and course material
|
+-- .github/workflows/          CI checks
+-- docker-compose.yml          Container definitions
+-- environment.yml             Python/conda dependencies
+-- prometheus.yml              Prometheus scrape config
+-- README.md                   Main newcomer guide and result summary
```

Common commands:

```bash
python analysis/orchestration/run_sweeps.py --servers node_dsp
python analysis/orchestration/run_des_all.py --servers go python_dsp
python analysis/reporting/plot_all_cdfs.py
python analysis/reporting/write_server_readmes.py
python analysis/des/fit_service_time.py
python analysis/des/des_m0_m1_m2_compare.py
python analysis/des/plot_supervisor_followup.py
python analysis/des/anova_service_time.py
```
