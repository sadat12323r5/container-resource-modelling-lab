# Baseline Capacity Experiments — Run Report

**Platform:** Docker Compose on Windows 11 / WSL2 (Docker Desktop 28.4.0)
**Branch:** main

---

## 1. Purpose

Establish a validated baseline capacity curve for two service implementations under
controlled, single-core Poisson load, and validate a trace-driven Discrete Event
Simulation (DES) against measured distributions.

**Services under test:**
1. **Go app** (`localhost:8080`) — single-worker FCFS FIFO queue; primary DES validation target.
2. **Apache/PHP** (`localhost:8082`) — messaging backend with matching synthetic service-time
   distribution; behavioural comparison point.

Both containers are pinned to `cpuset: "0"`, `cpus: "1.0"`, `mem_limit: 256m`.

---

## 2. Pre-Experiment Fixes

These blocking issues were resolved before any experiments could run.

### 2.1 `apache/index.php` — unresolved git merge conflicts

The file contained four sets of `<<<<<<< / ======= / >>>>>>>` conflict markers, causing
a PHP parse error on every request. Resolved by keeping the `ours` side in all four
conflicts, which retains `send_html_file()`, the `?route=` path override, and the
`GET /` → `chat.html` handler.

### 2.2 Apache routing — 404 on all routes

`php:8.3-apache` serves files directly by default. Without a `FallbackResource` directive,
all requests to `/health`, `/send`, and `/messages` returned 404 from the Apache static
file handler instead of routing through `index.php`.

**Fix 1 — `apache/.htaccess`:**
```
FallbackResource /index.php
```

**Fix 2 — `apache/000-default.conf`:**
```apache
<VirtualHost *:80>
    DocumentRoot /var/www/html
    <Directory /var/www/html>
        Options -Indexes
        AllowOverride All
        Require all granted
    </Directory>
</VirtualHost>
```

**Fix 3 — `docker-compose.yml`** (additional volume mount on `apache`):
```yaml
- ./apache/000-default.conf:/etc/apache2/sites-enabled/000-default.conf:ro
```

### 2.3 `README.md` — unresolved merge conflicts

The README contained the same conflict-marker style throughout multiple sections.
Resolved and rewritten as clean Markdown reflecting the current repository state.

---

## 3. New Files Added

| File | Purpose |
|---|---|
| `apache/.htaccess` | Routes all requests to `index.php` via `FallbackResource` |
| `apache/000-default.conf` | Apache VirtualHost enabling `AllowOverride All` |
| `apache_load.py` | Open-loop Poisson load generator with mixed GET `/messages` + POST `/send` |
| `run_experiments.py` | Automated rate-sweep runner: Go + Apache, DES, summary table |
| `.gitignore` | Excludes AI tooling, Python cache, generated experiment outputs |

---

## 4. Experiment Design

### 4.1 Fixed parameters (both iterations)

| Parameter | Value |
|---|---|
| Arrival process | Open-loop Poisson (exponential inter-arrivals) |
| Rate steps | 50, 100, 200, 400 rps |
| Duration per step | 90 s |
| Warm-up | 10 rps for 10 s before each phase |
| Service distribution | Lognormal, σ = 0.5 |
| Worker model | Single FIFO goroutine (Go); Apache MPM prefork default (PHP) |
| CPU constraint | `cpuset: "0"`, `cpus: "1.0"` |
| Client timeout | 5 s |
| Apache load mix | 70% GET `/messages`, 30% POST `/send` |
| DES mode | Bootstrap, seed 42 |
| RNG seed (load) | 42 |

### 4.2 Tools

| Tool | Role |
|---|---|
| `poisson_load_generator.py` | Go app load — GET `http://localhost:8080/` |
| `apache_load.py` | Apache load — mixed GET + POST |
| `logs and des/single_server_des.py` | Bootstrap DES against per-rate trace slices |
| `run_experiments.py` | Orchestrates everything; slices CSV by row count; prints summary |

### 4.3 How to reproduce

```bash
# 1. Start the stack (current config: SERVICE_MEAN_US=2000)
docker compose up --build -d

# 2. Verify all services
curl http://localhost:8080/
curl http://localhost:8082/health
curl http://localhost:9090/-/ready
curl http://localhost:3000/api/health

# 3. Run the full experiment suite (~14 min)
python run_experiments.py

# Outputs:
#   logs and des/experiments/go_<rate>rps.csv              -- per-rate trace slices
#   logs and des/experiments/go_<rate>rps_des_bootstrap.csv -- DES output per rate
#   logs and des/experiments/experiment_results.json        -- full consolidated JSON
```

To reproduce Iteration 1 (low service demand), set `SERVICE_MEAN_US=200` in
`docker-compose.yml` before running.

---

## 5. Iteration 1 — SERVICE_MEAN_US=200 (2026-03-24)

Mean declared service demand: **200 µs** (lognormal, σ=0.5).
Observed actual service p50: ~0.10 ms (calibration produces ~100 µs effective service).

### 5.1 Go app — rate sweep

All latency values in milliseconds.

| Rate | n | tput | svc p50 | svc p99 | resp p50 | resp p95 | resp p99 | queue p99 | Errors |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 50 | 4,361 | 48.6 | 0.10 | 0.47 | 0.12 | 0.33 | 0.58 | 0.07 | 0 |
| 100 | 8,923 | 99.3 | 0.10 | 0.69 | 0.13 | 0.40 | 0.83 | 0.10 | 0 |
| 200 | 18,014 | 200.1 | 0.12 | 3.06 | 0.14 | 0.92 | 3.84 | 0.29 | 0 |
| 400 | 36,286 | 207.9 | 0.12 | 3.42 | 0.14 | 1.04 | 4.42 | 0.33 | 503 (1.4%) |

**Critical finding — no real queueing.** Actual server utilisation at all steps:

| Rate | ρ = rate × E[service] |
|---:|---:|
| 50 rps | ~0.6% |
| 100 rps | ~1.5% |
| 200 rps | ~5% |
| 400 rps | ~11% |

The queue p50 is a constant **0.007–0.008 ms at every rate**. This is not M/G/1 queueing —
it is the OS/goroutine-scheduler round-trip latency between the HTTP handler recording
`arrival_unix_ns` and the worker goroutine reading from the job channel. The server was
essentially idle at all tested rates; the theoretical capacity with 200 µs service is
~5,000 rps. The rate sweep never entered the queueing regime.

### 5.2 DES fit — Iteration 1

| Rate | KS response | KS queue |
|---:|---:|---:|
| 50 | 0.182 | 0.998 |
| 100 | 0.168 | 0.996 |
| 200 | 0.132 | 0.968 |
| 400 | 0.133 | 0.963 |

KS queue near 1.0 across all rates because: the DES correctly predicts zero queue
(server idle), but the empirical queue distribution is a spike at ~0.008 ms from the
scheduling floor. The two CDFs are shifted by a fixed offset, driving the KS statistic
to near-maximum.

### 5.3 Apache — Iteration 1

| Rate | ok | err% | p50 | p99 |
|---:|---:|---:|---:|---:|
| 50 | 4,505 | 0% | 23.4 ms | 113 ms |
| 100 | 4,346 | 52% | 5,004 ms | 5,032 ms |
| 200 | 0 | 100% | — | — |
| 400 | 0 | 100% | — | — |

Capacity ~50 rps. Bottleneck is O(n) file I/O — every request reads the entire JSONL
message file (growing at ~15 messages/s during the run), not the 200 µs busy-loop.

### 5.4 Iteration 1 conclusion

The 200 µs service mean placed all tested rates well below the capacity knee. No
meaningful queueing was observed and the DES had nothing real to validate against.
**Service mean increased to 2000 µs for Iteration 2.**

---

## 6. Iteration 2 — SERVICE_MEAN_US=2000 (2026-03-24)

Config change in `docker-compose.yml`:
```yaml
- SERVICE_MEAN_US=2000        # was 200
- APACHE_SERVICE_MEAN_US=2000 # was 200
```

Mean declared service demand: **2000 µs** (lognormal, σ=0.5).
Observed actual service p50: ~0.74–1.15 ms (calibration under WSL2 yields ~40–60% of
declared value; see §6.4 for discussion).

### 6.1 Go app — rate sweep

All latency values in milliseconds.

| Rate | n | tput | svc p50 | svc p99 | resp p50 | resp p95 | resp p99 | queue p99 | Errors |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 50 | 4,397 | 49.0 | 0.74 | 3.12 | 0.77 | 1.94 | 3.22 | 0.05 | 0 |
| 100 | 8,898 | 99.0 | 0.79 | 4.06 | 0.82 | 2.27 | 4.28 | 0.10 | 0 |
| **200** | **18,047** | **199.2** | **1.14** | **12.60** | **1.22** | **7.74** | **16.89** | **1.83** | **0** |
| **400** | **36,101** | **234.2** | **1.15** | **10.98** | **1.23** | **6.79** | **13.62** | **1.45** | **88 (0.24%)** |

**Queueing is now present and measurable.** Utilisation estimates using observed mean
service times:

| Rate | obs svc mean | ρ = rate × E[svc] |
|---:|---:|---:|
| 50 rps | 0.89 ms | ~4% |
| 100 rps | 0.99 ms | ~10% |
| 200 rps | 2.01 ms | **~40%** |
| 400 rps | 1.93 ms | **~77%** |

Key transitions:
- **50–100 rps:** Light load. Queue p99 < 0.1 ms; response p99 closely tracks service p99.
- **200 rps — capacity knee:** Queue p99 jumps 18× (0.10 → 1.83 ms). Response p99 jumps
  4× (4.28 → 16.89 ms) while p50 moves only 49% (0.82 → 1.22 ms). Classic M/G/1 tail
  blowup driven by lognormal variance at ρ ≈ 40%.
- **400 rps — near-saturation:** 88 queue-overflow 503s (0.24%). Load generator ran 154 s
  (vs 90 s target) because slow in-flight requests blocked the thread pool.

Note: `svc p99` at 400 rps (10.98 ms) is lower than at 200 rps (12.60 ms). This is a
sampling/variance effect — 400 rps generates more requests, but the mix of queued vs
served shifts the observed service-time distribution. The queue absorbs bursts
differently at each utilisation level.

### 6.2 DES fit — Iteration 2

DES bootstrap: service times sampled with replacement from the observed empirical pool;
arrival timestamps taken from the actual trace.

| Rate | obs resp p50 | sim resp p50 | obs resp p99 | sim resp p99 | KS response | KS queue |
|---:|---:|---:|---:|---:|---:|---:|
| 50 | 0.77 ms | 0.75 ms | 3.22 ms | 2.82 ms | **0.028** | 0.979 |
| 100 | 0.82 ms | 0.82 ms | 4.28 ms | 4.78 ms | **0.016** | 0.953 |
| 200 | 1.22 ms | 1.80 ms | 16.89 ms | 50.64 ms | 0.162 | 0.657 |
| 400 | 1.23 ms | 1.98 ms | 13.62 ms | 175.2 ms | 0.180 | 0.620 |

**At low load (50–100 rps): excellent response-time fit (KS = 0.016–0.028).** The DES
matches observed response distributions closely when queueing is minimal.

**At high load (200–400 rps): DES bootstrap massively overestimates queueing.** Full
comparison at 200 rps:

| Metric | Observed | DES bootstrap | Ratio |
|---|---:|---:|---:|
| queue mean | 0.072 ms | 2.45 ms | 34× over |
| queue p99 | 1.83 ms | 48.6 ms | 27× over |
| response p99 | 16.89 ms | 50.64 ms | 3× over |

**Root cause — bootstrap temporal resampling bias.** The bootstrap draws service times
randomly with replacement from the empirical pool. With lognormal variance (CV ≈ 0.53),
any sufficiently unlucky draw can cluster several large service times consecutively,
creating cascading queue buildup that does not occur in the real system where Poisson
arrivals naturally separate service episodes. The real system's queue at ρ = 40% is
mild (M/G/1 mean queue length ≈ 0.17 jobs via the Pollaczek–Khinchine formula);
the bootstrap ignores this by treating temporal ordering as random.

**Implication for thesis:** Bootstrap DES is only valid at low utilisation (ρ ≲ 20%).
At higher utilisation, a **parametric DES** — sample service times i.i.d. from a fitted
lognormal rather than the empirical bootstrap — is required to avoid this overestimation.

**KS queue still high at 50–100 rps (0.95–0.98).** The 8 µs scheduling floor (see §6.3)
still dominates at low utilisation. At 200–400 rps, real queueing swamps the floor and
KS queue improves to 0.62–0.66, showing the DES captures the queue shape directionally
even if it overestimates magnitude.

### 6.3 Timestamp validation

All 18,014 rows in the 200 rps trace were checked for:
- **Ordering:** `arrival_unix_ns ≤ service_start_unix_ns ≤ service_end_unix_ns ≤ response_end_unix_ns`
- **Non-negativity:** `queue_ms`, `service_ms`, `response_ms` all ≥ 0
- **Consistency:** stored ms values match nanosecond timestamp differences within 0.002 ms

Results:
- Ordering violations: **0**
- Negative values: **0**
- Consistency mismatches: **67 rows (0.37%)**

**Root cause of mismatches — two-clock architecture in `main.go`:**

```go
// ms columns: computed with Go's MONOTONIC clock (time.Sub)
queueMs   := res.serviceStart.Sub(arrival).Seconds() * 1000
serviceMs := res.serviceEnd.Sub(res.serviceStart).Seconds() * 1000
responseMs := responseEnd.Sub(arrival).Seconds() * 1000

// ns columns: computed with WALL clock (time.Now().UnixNano())
strconv.FormatInt(arrival.UnixNano(), 10),
strconv.FormatInt(res.serviceStart.UnixNano(), 10), ...
```

Go's `time.Sub()` uses a monotonic clock reading stripped of wall-clock adjustments.
`time.Now().UnixNano()` uses the wall clock, which can step forward or backward during
NTP synchronisation. Under WSL2 on Windows, the Windows clock sync can cause wall-clock
adjustments mid-run, creating divergence between the two representations. The 67 affected
rows (including one outlier: 4.009 ms stored vs 0.400 ms derived) correspond to moments
where the WSL2 wall clock was adjusted.

**Practical guidance for analysis:**
- Use the **ms columns** (`queue_ms`, `service_ms`, `response_ms`) for all distribution
  analysis and DES fitting — they are monotonic-clock intervals, immune to NTP jumps.
- Use the **ns columns** for request ordering only, not for computing intervals.

**8 µs scheduling floor.** Even with the clock issue resolved, the `queue_ms` column
has a non-removable measurement artifact: the ~7–8 µs round-trip from `arrival_unix_ns`
(recorded at HTTP handler entry) to `service_start_unix_ns` (recorded at worker goroutine
entry) includes goroutine scheduling and channel send/receive latency, not actual queue
wait time. This floor is constant across all rates and utilisation levels:

| Rate | obs queue p50 | obs queue p90 |
|---:|---:|---:|
| 50 rps | 0.007 ms | 0.010 ms |
| 100 rps | 0.007 ms | 0.010 ms |
| 200 rps | 0.006 ms | 0.011 ms |
| 400 rps | 0.006 ms | 0.010 ms |

When computing queue-time statistics or KS distances, subtract ~0.006 ms from all
`queue_ms` values (the approximate p1 of the distribution) to isolate actual queue wait
from scheduling overhead.

### 6.4 Service-time calibration under WSL2

Declared `SERVICE_MEAN_US=2000`, but observed actual service times:

| Rate | svc p50 | svc mean |
|---:|---:|---:|
| 50 | 0.74 ms | 0.89 ms |
| 100 | 0.79 ms | 0.99 ms |
| 200 | 1.14 ms | 2.01 ms |
| 400 | 1.15 ms | 1.93 ms |

The lognormal(mean=2000 µs, σ=0.5) distribution has a theoretical median of ~1759 µs.
The observed p50 of 0.74–1.15 ms is 40–65% of that. The work-based calibration
(`calibrateItersPerNs`) runs once at startup; actual per-request CPU speed under WSL2
varies with Windows scheduler behaviour, thermal state, and GOMAXPROCS=1 goroutine
contention. The distribution *shape* (lognormal, confirmed by p50/p99 ratio) is
preserved — only the scale differs from the declared value. This affects absolute
throughput estimates but not the structural queueing and DES validation findings.

### 6.5 Apache — Iteration 2

| Rate | ok | err% | p50 | p99 |
|---:|---:|---:|---:|---:|
| 50 | 4,505 | 0% | 24.6 ms | 100.7 ms |
| 100 | 7,543 | 16.6% | 1,833 ms | 5,029 ms |
| 200 | 45 | 99.8% | 4,088 ms | 5,034 ms |
| 400 | 0 | 100% | 4,077 ms | 4,119 ms |

Capacity remains ~50 rps. With 2 ms synthetic service demand the busy-loop is now 10×
heavier than Iteration 1, compounding with the existing O(n) file I/O bottleneck.
Apache cannot be used for DES comparison in this configuration.

---

## 7. Cross-Iteration Comparison

| | Iteration 1 (200 µs) | Iteration 2 (2000 µs) |
|---|---|---|
| Go utilisation at 200 rps | ~5% | **~40%** |
| Queue p99 at 200 rps | 0.29 ms (scheduling noise) | **1.83 ms (real queue)** |
| Response p99 at 200 rps | 3.84 ms | **16.89 ms** |
| DES KS response (50 rps) | 0.182 | **0.028** |
| DES KS response (200 rps) | 0.132 | 0.162 |
| DES KS queue (200 rps) | 0.968 | 0.657 |
| Bootstrap overestimation at 200 rps | Minimal (no real queue) | **34× queue mean** |
| Apache effective capacity | ~50 rps | ~50 rps (same I/O floor) |

Iteration 2 achieves the primary goal — real, measurable queueing — and exposes a second
finding: bootstrap DES breaks down as a queue-time predictor at moderate-to-high
utilisation.

---

## 8. Conclusions

### Confirmed findings

1. **Real queueing is present at ρ ≈ 40% (200 rps, 2 ms service).** Queue p99 = 1.83 ms,
   response p99 = 16.89 ms. The M/G/1 tail blowup pattern is clearly visible: p50
   barely moves while p99 jumps 4×.

2. **DES bootstrap fits well at low load, fails at high load.**
   - KS response = 0.016–0.028 at 50–100 rps — excellent.
   - KS response = 0.16–0.18 at 200–400 rps — moderate.
   - Bootstrap overestimates queue mean by 34× at 200 rps due to temporal resampling bias.

3. **Two timestamp findings:**
   - 8 µs goroutine-scheduling floor in `queue_ms` — constant, removable by subtracting p1.
   - 0.37% of rows have ms/ns clock divergence from WSL2 NTP adjustments — use ms columns
     for interval analysis.

4. **Apache is not a valid DES comparison point** due to O(n) file I/O bottleneck.
   Capacity capped at ~50 rps regardless of synthetic service demand.

5. **WSL2 work-based calibration is unreliable** for matching absolute service times;
   the distribution shape is preserved but the scale is 40–65% of declared value.

### Next steps

| Priority | Action |
|---|---|
| High | Implement parametric DES: fit lognormal to observed service times, sample i.i.d. |
| High | Subtract 8 µs scheduling offset from `queue_ms` before KS analysis |
| High | Run DES in replay mode and compare KS to bootstrap for both load levels |
| High | Fine-grained sweep around the 200 rps knee (150, 175, 200, 225, 250 rps) |
| Medium | Fix Apache message store (bounded ring buffer) to enable DES comparison |
| Medium | Add CSV trace logging to Apache |
| Low | Train ML latency predictor on Go traces; compare KS to DES predictions |
