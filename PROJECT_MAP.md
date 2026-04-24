# Capacity Lab Project Map

This workspace is organized by responsibility:

```text
Capacity_lab/
├── servers/                    Docker service implementations
│   ├── go_lognormal/           Go lognormal M/G/1 server
│   ├── apache_msg/             Apache/PHP messaging backend
│   ├── apache_dsp_aes/         Apache/PHP DSP-AES workload
│   ├── node_dsp_aes/           Node.js DSP-AES workload
│   ├── python_dsp_aes/         Python/Gunicorn DSP-AES workload
│   ├── java_dsp_aes/           Java ThreadPool DSP-AES workload
│   └── go_sqlite/              Go SQLite workload
│
├── analysis/
│   ├── des/                    DES engines, shared metrics, fitted models
│   ├── load/                   Poisson load generators
│   ├── orchestration/          Sweep and DES batch runners
│   ├── reporting/              CDF plots and generated experiment READMEs
│   └── visualisation/          Browser visualiser and live backend
│
├── data/
│   ├── experiments/            Raw traces, DES outputs, summaries, per-server READMEs
│   └── live_logs/              Bind-mounted CSV logs written by running containers
│
├── results/
│   ├── figures/                Cross-server figures
│   └── tables/                 Cross-server CSV outputs
│
├── docs/
│   ├── EXPERIMENTS.md          Main experiment log
│   ├── DIFFICULTIES.md         Research/debugging log
│   ├── latex/                  Thesis LaTeX source and exports
│   └── references/             Papers and course material
│
├── docker-compose.yml
├── environment.yml
└── README.md
```

Common commands:

```bash
python analysis/orchestration/run_sweeps.py --servers node_dsp
python analysis/orchestration/run_des_all.py --servers go python_dsp
python analysis/reporting/plot_all_cdfs.py
python analysis/reporting/write_server_readmes.py
python analysis/des/fit_service_time.py
python analysis/des/des_load_dependent.py
```
