# Research Difficulties Log

A chronological record of significant problems encountered during the
experimental setup, measurement, modelling, and validation phases of this
thesis. Each entry describes what went wrong, why it happened, how it was
resolved, and what it changed about the research direction.

---

## Phase 1 — Initial Setup (Oct 2025 – Jan 2026)

### D-01 · Service demand vs wall-clock time confusion

**What happened.**
The first version of the Go server measured service time as the total wall-clock
duration of the request handler, including network I/O (reading the request body,
writing the response). This produced service times that were far larger than the
actual CPU work and varied with payload size and client latency.

**Why it mattered.**
Queueing models (M/G/1, M/G/c) require *service demand* — pure CPU work — not
total handler time. Feeding inflated wall-clock times into the DES made predicted
response times unrealistically high.

**Resolution.**
Refactored the Go server to record time only around the computation block
(the calibrated busy-work loop), excluding I/O. Commit `e340610` ("refactored
and made service demand work based and not wall clock based").

**Lesson.**
Service time and response time are not the same. Every server subsequently
instrumented time strictly around the computation kernel.

---

### D-02 · Apache routing silently failing

**What happened.**
The first Apache PHP server returned 200 OK for every request regardless of
whether the PHP script executed, because the default VirtualHost was serving
the Apache default page instead of the PHP application.

**Why it mattered.**
Load generator CSV files contained valid status codes but the measured response
times were ~0.1 ms (static file) rather than ~2 ms (PHP computation). This
would have made Apache look orders of magnitude faster than it really is.

**Resolution.**
Added a correct `000-default.conf` VirtualHost pointing `DocumentRoot` to
`/var/www/html` and enabled `mod_php`. Commit `d336061` ("fix Apache routing").

**Lesson.**
Always verify the server is actually executing the intended workload, not just
responding. Added a sanity-check endpoint (`/health`) that returns the measured
service time so the load generator can detect misconfiguration immediately.

---

### D-03 · Single-core constraint not enforced in Docker

**What happened.**
Early Docker Compose definitions did not set `cpuset` or `cpus`. On a multi-core
host, the container scheduler spread goroutines/threads across all available
cores. The Go server appeared to saturate at a much higher rate than expected
for a single-server M/G/1 model.

**Why it mattered.**
The entire experimental design assumes a known number of servers *c*. Without
CPU pinning the effective parallelism was undefined and not reproducible.

**Resolution.**
Added `cpuset: "0"` and `cpus: "1.0"` to every single-core service definition,
and `cpuset: "0,1,2"` / `cpus: "3.0"` for the three-worker variants. This made
the system behave as designed and results became reproducible across machines.

**Lesson.**
Docker resource limits are not set by default. Container "isolation" does not
mean CPU isolation unless explicitly configured.

---

## Phase 2 — Instrumentation & Logging (Feb – Mar 2026)

### D-04 · PHP has no way to measure service time directly at response boundary

**What happened.**
PHP Apache (mpm_prefork) runs a script, sends the response, then the process
becomes available for the next request. There is no native hook that fires
*after* the response bytes leave the socket but *before* the process is reused.
`apache_response_header_sent()` does not exist. Using `microtime()` at the end
of the script captures time *before* the response is fully written.

**Why it mattered.**
We needed `queue_ms` (time waiting for a free Apache process) and `service_ms`
(pure computation time) separately. Apache only gives access to
`$_SERVER['REQUEST_TIME_FLOAT']` (when the request was accepted) from inside
the script — not when it started executing.

**Resolution.**
Used PHP's `register_shutdown_function()` to log metrics after script execution
completes. Recorded `$serviceStartNs` immediately before the AES+FIR computation
and `$serviceEndNs` immediately after. `queue_ms` was computed as
`response_ms - service_ms` (subtraction, not direct measurement). Used
`hrtime(true)` (nanosecond resolution) rather than `microtime()` to avoid
system-clock drift affecting short intervals.

**Lesson.**
`queue_ms` in all Apache traces is *derived*, not measured. It absorbs any
overhead between script start and computation start (include loading, session
init) and any overhead between computation end and response write. This is
a known measurement artefact documented in the results.

---

### D-05 · CSV log file corruption under concurrent writes

**What happened.**
Multiple Apache worker processes wrote to the same CSV log file simultaneously.
With no locking, rows were interleaved mid-line, producing parse errors and
corrupted arrival timestamps.

**Why it mattered.**
Any corrupted row silently invalidated part of the trace. At 75 rps with 8+
concurrent Apache processes this happened frequently enough to lose 5–10% of
records.

**Resolution.**
Added `flock($fp, LOCK_EX)` before every `fwrite()` call. This serialises all
log writes across processes.

**Consequence discovered later.**
`flock` is blocking — each process holds the CPU while waiting for the lock.
At high load, processes queue for the lock, adding a small but consistent
overhead to every request's `register_shutdown_function` execution time.
This contributed to the measured `queue_ms` values being slightly inflated
even at low load.

---

### D-06 · Go server silently dropping requests without logging

**What happened.**
The Go server used a bounded channel as its job queue (`JOB_QUEUE=1024`). When
the channel was full, new requests were rejected with HTTP 503. The load
generator counted these as valid responses (status 503, fast response time) and
included them in the CSV.

**Why it mattered.**
503 responses have near-zero response time (immediate rejection). Including them
made the tail latency look artificially low and the throughput look artificially
high.

**Resolution.**
Logged rejections separately with a `rejected=true` flag. Modified the load
generator to filter `status_code` and only include 200 responses in analysis.
Added a rejection counter to the server metrics. Commit `fba5730` ("refined DES
by logging request rejection in go server").

**Lesson.**
Any server with a finite queue must explicitly track and log rejections. Capacity
analysis that ignores rejection rates is meaningless near the saturation point.

---

### D-07 · Java server service times inflated at low request rates (JIT warmup)

**What happened.**
At 25 rps, Java DSP reported `mean_svc_ms ≈ 0.55 ms`. At 50–200 rps it dropped
to `0.24–0.31 ms`. This looked like service time *decreasing* with load, the
opposite of every other server.

**Why it mattered.**
The load-dependent model `E[S] = S₀ / (1 − β·ρ)` fitted β ≈ 0.999 for Java DSP
— implying extreme PS-like degradation. But the actual cause was the opposite:
early requests run interpreted bytecode (slow), JIT kicks in after a warmup
period (fast), and the measured average at low rates captures the slow phase.

**Resolution.**
Identified as a JIT warmup artefact, not a queueing phenomenon. Excluded Java
DSP from the load-dependent model analysis. Noted that production Java services
always run behind a warmup phase (load balancer health checks, Kubernetes
readiness probes) so steady-state measurements should exclude the first N seconds.

**Lesson.**
JVM-based services require a warmup period before measurements are valid.
The `n_obs` column in summary CSVs was used to verify sufficient samples were
collected in steady state.

---

## Phase 3 — DES Implementation (Feb – Mar 2026)

### D-08 · Trace-driven DES underestimated response time at low load

**What happened.**
CDF plots showed the DES simulated response times were consistently *smaller*
than observed response times at low utilisation (ρ < 0.1), producing a
visible gap in the left tail. KS distances of 0.20–0.27 at low rates were
puzzling given the DES was parameterised from the same trace.

**Why it mattered.**
If the DES underestimates at low load, it will also underestimate the capacity
knee. The model would recommend a higher request rate as "safe" than is actually
safe.

**Root cause identified.**
`queue_ms` in the PHP/Apache traces is computed as
`response_ms − service_ms`, which includes: (a) time between request acceptance
and script execution start (Apache process selection overhead), (b) time between
script end and response fully written to socket. None of these appear in `service_ms`.
At near-zero utilisation, `queue_ms` ≈ 0.5–2 ms from this overhead alone. The
DES models pure queueing only, so its simulated `wait_ms ≈ 0` at low ρ, but
observed `queue_ms ≈ 2 ms`.

**Resolution.**
Documented as a known measurement artefact. The DES is correct — it models
the queueing component. The overhead is a constant additive term that could be
subtracted from observed `queue_ms` if needed. Noted explicitly in results so
KS distances at low ρ are not misinterpreted as model failures.

---

### D-09 · Three DES modes produced different KS distances for the same trace

**What happened.**
Running replay, bootstrap, and parametric DES on the same trace file produced
noticeably different KS distances (sometimes differing by 0.05–0.10). It was
unclear which mode was "correct" or which to report.

**Why it mattered.**
If modes give different answers, the reported KS depends on which mode you run.
This could make results look better or worse depending on arbitrary choices.

**Root cause.**
Each mode tests a different hypothesis:
- **Replay** — uses exact arrival times from trace. Tests: *given these arrivals, does the service time distribution explain response times?* Sensitive to service time model.
- **Bootstrap** — resamples inter-arrivals from trace. Tests: *given this arrival rate distribution, does the model hold?* Averages over arrival randomness.
- **Parametric** — generates Poisson arrivals at observed λ. Tests: *if arrivals were truly Poisson, would the model hold?* Reveals non-Poisson arrival effects.

**Resolution.**
Reported all three modes in summary CSVs. Used parametric KS as the primary
model validation metric (tests the full M/G/c assumption including Poisson
arrivals). Replay KS used to isolate service time model fit. The difference
between parametric and replay KS quantifies how much non-Poisson arrivals
contribute to model error.

---

### D-10 · KS distance implementation was O(n²) causing slow analysis

**What happened.**
The original `ks_distance()` in `plot_all_cdfs.py` iterated over all unique
x-values and for each computed the CDF fraction by counting elements — O(n)
per point, O(n²) total. For traces with 10,000+ requests this took 30–40
seconds per server.

**Resolution.**
Rewrote as a two-pointer O(n log n) algorithm (sort both arrays, advance
pointers through merged sorted order). Extracted to `des_utils.py` so both
`run_des_all.py` and `plot_all_cdfs.py` share the same implementation. Runtime
dropped to under 1 second per server.

**Lesson.**
Shared utility code (KS, percentiles, CSV reading) must live in one place.
The two files had diverged implementations with different performance
characteristics — a source of subtle inconsistency.

---

## Phase 4 — Multi-Core Servers (Mar – Apr 2026)

### D-11 · Worker count did not match DES c parameter

**What happened.**
The Java DSP server was configured with `JAVA_WORKERS=4` (4 threads) but the
initial DES ran with `c=1`. The simulated queue was far longer than observed
because the DES thought only 1 worker was available.

**Why it mattered.**
KS distances for Java DSP were extremely high (> 0.7) until the correct worker
count was used.

**Resolution.**
Audited every server's concurrency model and matched the DES `c` parameter
accordingly. Key findings during this audit:

- **Apache mpm_prefork**: one OS process per connection, effective parallelism
  determined by `MaxRequestWorkers` (default 150). Constrained to `c=1` by
  `cpus=1.0` in Docker — only one process runs at a time.
- **Node.js cluster**: `WORKERS=3` environment variable spawns 3 child processes.
  OS TCP round-robin distributes connections. `c=3`.
- **Python Gunicorn**: `--workers 3` spawns 3 independent WSGI processes with
  separate GILs. `c=3`.
- **Java ThreadPoolExecutor**: `JAVA_WORKERS=4`. `c=4`.
- **Go goroutines**: single shared channel, single worker goroutine. `c=1`.

---

### D-12 · Node.js cluster did not distribute load evenly

**What happened.**
Node.js cluster mode uses OS-level TCP round-robin (SO_REUSEPORT). Under Poisson
arrivals, the distribution was not perfectly even — one worker would occasionally
receive multiple requests in quick succession while others were idle. This
produced a service time distribution with a bimodal shape (fast = no queue at
worker, slow = queued at worker) that the M/G/c model (which assumes a shared
queue) could not reproduce.

**Why it mattered.**
KS distances for Node DSP multi-core remained high (0.66–0.78) even with correct
`c=3`. The fundamental issue is that Node.js cluster is not a shared-queue M/G/c
system — it is three independent M/G/1 queues fed by an approximately equal
splitter.

**Resolution.**
Documented as an architectural mismatch. Node.js cluster is better modelled as
3 × M/G/1 in parallel with Bernoulli splitting, not a single M/G/3. Noted as a
finding: the queueing model must match the actual dispatching architecture, not
just the worker count.

---

## Phase 5 — Model Violations & Novel Contributions (Apr 2026)

### D-13 · Service time was not constant — the core M/G/c assumption violated

**What happened.**
Summary CSV analysis showed that `mean_svc_ms` increased with `rate_rps` for
most servers. For Apache DSP, service time grew from 2.5 ms at 10 rps to
5.8 ms at 75 rps — a 2.3× increase. This directly violates the M/G/c
assumption that service time is drawn from a fixed distribution independent
of system load.

**Why it mattered.**
All KS distances computed up to this point assumed constant service time. The
model was systematically wrong at high load, and the "good" KS distances at
low load were coincidental (the model happens to match when load-dependence
is negligible).

**Root cause.**
For CPU-bound servers, the OS scheduler (Linux CFS) time-slices the CPU among
all runnable processes. At high load more processes compete for the CPU,
causing more mid-computation preemptions. Wall-clock service time includes
these scheduler delays. The effect is proportional to CPU contention — i.e.,
proportional to utilisation ρ. This is processor sharing at the kernel level
even though the application queue discipline is FCFS.

**Resolution.**
Fitted a hyperbolic load-dependent service time model:

```
E[S](ρ) = S₀ / (1 − β·ρ)
```

where S₀ is the base service time at zero load and β ∈ [0, 1) is a per-server
sensitivity coefficient fitted by nonlinear least squares from the summary CSV.
Implemented in `fit_service_time.py` and validated in `des_load_dependent.py`.

Results: the load-dependent DES improved KS distance for 100% of stable
measurement points on 5 of 9 servers (Apache DSP, Apache MSG, Go lognormal,
Node DSP 1c, Python DSP 3c), with zero degradation on servers where β ≈ 0
(Python DSP 1c, Go SQLite). The largest single improvement: Apache DSP at
ρ = 0.435, KS drops from 0.527 → 0.290.

**This became the central research contribution.**

---

### D-14 · Go lognormal server also showed load-dependent service time

**What happened.**
The Go server was designed to sample service time from a fixed lognormal
distribution regardless of load. Yet the summary CSV showed service time growing
from 1.22 ms at 50 rps (ρ=0.061) to 2.18 ms at 200 rps (ρ=0.435). β fitted
as 0.976.

**Why it mattered.**
This server was expected to be the "ground truth" validator for M/G/c because
its service distribution is explicitly programmed. Finding load-dependence here
was unexpected.

**Root cause (after investigation).**
Go's goroutine scheduler is cooperative with preemption since Go 1.14. Under
high goroutine concurrency, the Go runtime scheduler itself introduces latency
between when a goroutine is runnable and when it actually runs. Additionally,
the calibration busy-loop that implements service demand involves tight CPU
spinning — at high request rates, multiple goroutines compete for the same
OS thread, causing scheduling delays inside the service phase.

**Lesson.**
Even a "controlled" synthetic workload can exhibit load-dependent behaviour if
the runtime scheduler is not accounted for. True constant service time requires
either: (a) sleeping for a fixed duration (which yields the CPU, introducing
different artefacts) or (b) measuring only goroutine-active time, not
wall-clock time.

---

### D-15 · Go SQLite server could not be modelled by any standard queue

**What happened.**
Go SQLite exhibited wildly varying KS distances (0.16–0.99 across rates) with
no consistent pattern. Service time was roughly constant (~10 ms) but response
time distribution was heavy-tailed and bimodal at some rates.

**Root cause.**
SQLite in WAL mode serialises all writes through a single write lock.
Under read-heavy load the server behaves as M/G/1. Under write-heavy load,
multiple goroutines queue for the WAL lock — a second internal queue that
the application-level M/G/1 does not model. Additionally, SQLite page cache
effects meant that the first request after a cold start was 10× slower than
subsequent requests, inflating response time variance non-stationarily.

**Resolution.**
Documented as outside the scope of the M/G/c family. Go SQLite requires a
two-stage model: application queue → SQLite lock queue. This became one of the
motivating examples for the multi-tier queueing direction proposed for future
work.

---

## Summary Table

| ID | Phase | Difficulty | Resolution | Impact on research |
|----|-------|-----------|------------|-------------------|
| D-01 | Setup | Wall-clock vs service demand | Instrument only compute block | Fundamental |
| D-02 | Setup | Apache routing misconfiguration | Correct VirtualHost config | Correctness |
| D-03 | Setup | CPU pinning not enforced | `cpuset` + `cpus` in Compose | Reproducibility |
| D-04 | Measurement | PHP no direct service time | `hrtime` + `register_shutdown_function` | Measurement bias |
| D-05 | Measurement | CSV corruption under concurrency | `flock(LOCK_EX)` | Data integrity |
| D-06 | Measurement | Go silently dropping requests | Log 503s, filter status codes | Analysis validity |
| D-07 | Measurement | Java JIT warmup artefact | Exclude warmup, note limitation | Model scope |
| D-08 | DES | DES underestimates at low load | Document overhead offset | Interpretation |
| D-09 | DES | Three DES modes disagree | All three reported, defined semantics | Reporting |
| D-10 | DES | KS distance O(n²) too slow | Two-pointer O(n log n) rewrite | Performance |
| D-11 | Multi-core | Wrong worker count in DES | Audit concurrency model per server | Correctness |
| D-12 | Multi-core | Node.js cluster uneven load | Document as 3×M/G/1, not M/G/3 | Model finding |
| D-13 | Modelling | Service time grows with load | Hyperbolic β model, new DES | **Core contribution** |
| D-14 | Modelling | Go lognormal also load-dependent | Goroutine scheduler contention | Finding |
| D-15 | Modelling | Go SQLite not modellable | Document as two-stage queue | Future work |
