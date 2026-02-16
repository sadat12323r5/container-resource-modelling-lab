package main

import (
	"bufio"
	"context"
	"encoding/csv"
	"errors"
	"log"
	"math"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"
)

// ------------------------------
// Single-queue, single-worker HTTP server (FCFS)
// ------------------------------
//
// Goals for modelling:
//   - One FIFO queue, one worker => clean M/G/1 baseline.
//   - Service demand is WORK-based (fixed CPU work) not TIME-based.
//   - ResponseWriter writes happen in the handler goroutine (clean net/http usage).
//   - Logging NEVER drops samples (missing tails ruins experiments).
//   - Graceful shutdown flushes CSV on SIGTERM (docker stop/compose down).

type job struct {
	id       uint64
	traceID  string
	arrival  time.Time
	respChan chan workerResult
}

type workerResult struct {
	serviceStart time.Time
	serviceEnd   time.Time
	status       int
	body         []byte
	headers      map[string]string
}

// ------------------------------
// Work-based CPU demand
// ------------------------------
//
// IMPORTANT: a wall-clock "spin until deadline" loop is not a stable service
// demand model. If the process is throttled/preempted, wall time moves anyway,
// and your loop can exit having received *less* CPU than intended.
//
// So we calibrate a loop rate once (iters/ns), then for each request we execute
// a fixed amount of work proportional to sampled demand.

var sink uint64 // prevents compiler optimising the loop away

func busyLoop(iters uint64) {
	x := sink ^ 0x9e3779b97f4a7c15
	for i := uint64(0); i < iters; i++ {
		x = x*1664525 + 1013904223
	}
	sink = x
}

func calibrateItersPerNs() float64 {
	calIters := uint64(getenvInt("CALIBRATE_ITERS", 20_000_000))
	if calIters < 1_000_000 {
		calIters = 1_000_000
	}

	// Warm-up reduces first-run jitter.
	busyLoop(2_000_000)

	start := time.Now()
	busyLoop(calIters)
	elapsed := time.Since(start)
	if elapsed <= 0 {
		return 1
	}
	return float64(calIters) / float64(elapsed.Nanoseconds())
}

func doWork(d time.Duration, itersPerNs float64) {
	if d <= 0 {
		return
	}
	target := uint64(float64(d.Nanoseconds()) * itersPerNs)
	if target == 0 {
		target = 1
	}
	busyLoop(target)
}

// ------------------------------
// Logging helpers
// ------------------------------

func mustWriteHeader(csvWriter *csv.Writer, bufWriter *bufio.Writer) {
	if err := csvWriter.Write([]string{
		"id",
		"trace_id",
		"arrival_unix_ns",
		"service_start_unix_ns",
		"service_end_unix_ns",
		"response_end_unix_ns",
		"queue_ms",
		"service_ms",
		"response_ms",
		"status_code",
		"bytes_written",
	}); err != nil {
		log.Fatalf("write csv header: %v", err)
	}
	csvWriter.Flush()
	if err := csvWriter.Error(); err != nil {
		log.Fatalf("flush csv header: %v", err)
	}
	if err := bufWriter.Flush(); err != nil {
		log.Fatalf("flush csv buffer: %v", err)
	}
}

func main() {
	// Make the system behaviour closer to "single worker".
	runtime.GOMAXPROCS(1)
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)

	itersPerNs := calibrateItersPerNs()
	log.Printf("calibration iters_per_ns=%.6f (CALIBRATE_ITERS=%d)", itersPerNs, getenvInt("CALIBRATE_ITERS", 20_000_000))

	// ------------------------------
	// CSV setup
	// ------------------------------
	csvPath := os.Getenv("CSV_LOG_PATH")
	if csvPath == "" {
		csvPath = "requests.csv"
	}
	appendCSV := false
	switch os.Getenv("CSV_LOG_APPEND") {
	case "1", "true", "TRUE", "yes", "YES":
		appendCSV = true
	}
	openFlags := os.O_CREATE | os.O_WRONLY
	if appendCSV {
		openFlags |= os.O_APPEND
	} else {
		openFlags |= os.O_TRUNC
	}
	csvFile, err := os.OpenFile(csvPath, openFlags, 0644)
	if err != nil {
		log.Fatalf("open csv log: %v", err)
	}
	defer csvFile.Close()

	needHeader := true
	if appendCSV {
		if info, statErr := csvFile.Stat(); statErr == nil && info.Size() > 0 {
			needHeader = false
		}
	}

	bufWriter := bufio.NewWriterSize(csvFile, 1<<20)
	csvWriter := csv.NewWriter(bufWriter)
	if needHeader {
		mustWriteHeader(csvWriter, bufWriter)
	}

	// Async logger that NEVER drops (it will apply backpressure instead).
	logQueueSize := getenvInt("CSV_LOG_QUEUE", 10000)
	flushInterval := time.Duration(getenvInt("CSV_LOG_FLUSH_MS", 1000)) * time.Millisecond
	logCh := make(chan []string, logQueueSize)
	loggerDone := make(chan struct{})
	go func() {
		defer close(loggerDone)
		ticker := time.NewTicker(flushInterval)
		defer ticker.Stop()
		pending := 0
		for {
			select {
			case record, ok := <-logCh:
				if !ok {
					// final flush
					if pending > 0 {
						csvWriter.Flush()
						if err := csvWriter.Error(); err != nil {
							log.Printf("csv flush error: %v", err)
						}
						if err := bufWriter.Flush(); err != nil {
							log.Printf("csv buffer flush error: %v", err)
						}
					}
					return
				}
				if err := csvWriter.Write(record); err != nil {
					log.Printf("csv write error: %v", err)
				} else {
					pending++
				}
				// occasional size-based flush reduces loss on crash
				if pending >= 5000 {
					csvWriter.Flush()
					if err := csvWriter.Error(); err != nil {
						log.Printf("csv flush error: %v", err)
					}
					if err := bufWriter.Flush(); err != nil {
						log.Printf("csv buffer flush error: %v", err)
					}
					pending = 0
				}
			case <-ticker.C:
				if pending > 0 {
					csvWriter.Flush()
					if err := csvWriter.Error(); err != nil {
						log.Printf("csv flush error: %v", err)
					}
					if err := bufWriter.Flush(); err != nil {
						log.Printf("csv buffer flush error: %v", err)
					}
					pending = 0
				}
			}
		}
	}()

	// ------------------------------
	// Work queue + worker
	// ------------------------------
	jobs := make(chan job, getenvInt("JOB_QUEUE", 1024))
	serviceSampler := newServiceSampler()
	workerDone := make(chan struct{})
	go func() {
		defer close(workerDone)
		for j := range jobs {
			serviceStart := time.Now()
			demand := serviceSampler()
			doWork(demand, itersPerNs)
			serviceEnd := time.Now()

			j.respChan <- workerResult{
				serviceStart: serviceStart,
				serviceEnd:   serviceEnd,
				status:       http.StatusOK,
				body:         []byte("ok\n"),
				headers:      map[string]string{"X-Request-Id": j.traceID},
			}
			close(j.respChan)
		}
	}()

	// ------------------------------
	// HTTP handlers
	// ------------------------------
	var nextID uint64

	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})

	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		arrival := time.Now()
		id := atomic.AddUint64(&nextID, 1)

		traceID := r.Header.Get("X-Request-Id")
		if traceID == "" {
			traceID = r.Header.Get("X-Vegeta-Seq")
		}
		if traceID == "" {
			traceID = strconv.FormatUint(id, 10)
		}

		respChan := make(chan workerResult, 1)
		j := job{id: id, traceID: traceID, arrival: arrival, respChan: respChan}

		// Fail fast if the queue is full.
		select {
		case jobs <- j:
			// queued
		default:
			w.Header().Set("X-Request-Id", traceID)
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte("queue full\n"))
			return
		}

		res, ok := <-respChan
		if !ok {
			w.Header().Set("X-Request-Id", traceID)
			w.WriteHeader(http.StatusInternalServerError)
			_, _ = w.Write([]byte("worker error\n"))
			return
		}

		for k, v := range res.headers {
			w.Header().Set(k, v)
		}
		w.WriteHeader(res.status)
		bytesWritten, _ := w.Write(res.body)
		responseEnd := time.Now()

		queueMs := res.serviceStart.Sub(arrival).Seconds() * 1000
		serviceMs := res.serviceEnd.Sub(res.serviceStart).Seconds() * 1000
		responseMs := responseEnd.Sub(arrival).Seconds() * 1000

		record := []string{
			strconv.FormatUint(id, 10),
			traceID,
			strconv.FormatInt(arrival.UnixNano(), 10),
			strconv.FormatInt(res.serviceStart.UnixNano(), 10),
			strconv.FormatInt(res.serviceEnd.UnixNano(), 10),
			strconv.FormatInt(responseEnd.UnixNano(), 10),
			strconv.FormatFloat(queueMs, 'f', 3, 64),
			strconv.FormatFloat(serviceMs, 'f', 3, 64),
			strconv.FormatFloat(responseMs, 'f', 3, 64),
			strconv.Itoa(res.status),
			strconv.Itoa(bytesWritten),
		}

		// For modelling: do NOT drop logs. If the logger can't keep up,
		// backpressure is the correct failure mode.
		logCh <- record
	})

	server := &http.Server{
		Addr:    ":8080",
		Handler: mux,
	}

	// ------------------------------
	// Graceful shutdown
	// ------------------------------
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sigCh := make(chan os.Signal, 2)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigCh
		log.Printf("shutdown signal received; draining...")
		cancel()
		// Close server first so we stop accepting new work.
		shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer shutdownCancel()
		_ = server.Shutdown(shutdownCtx)
		// Drain worker + logger.
		close(jobs)
		<-workerDone
		close(logCh)
		<-loggerDone
		log.Printf("shutdown complete")
	}()

	log.Printf("listening on %s", server.Addr)
	err = server.ListenAndServe()
	if err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("listen: %v", err)
	}

	// If we exited normally (server closed), wait for shutdown to finish.
	<-ctx.Done()
}

func getenvInt(name string, fallback int) int {
	value := os.Getenv(name)
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}
	return parsed
}

func getenvFloat(name string, fallback float64) float64 {
	value := os.Getenv(name)
	if value == "" {
		return fallback
	}
	parsed, err := strconv.ParseFloat(value, 64)
	if err != nil {
		return fallback
	}
	return parsed
}

func newServiceSampler() func() time.Duration {
	dist := strings.ToLower(os.Getenv("SERVICE_DIST"))
	if dist == "" {
		dist = "fixed"
	}
	meanUs := getenvFloat("SERVICE_MEAN_US", 200)
	if meanUs <= 0 {
		meanUs = 1
	}
	rng := rand.New(rand.NewSource(time.Now().UnixNano()))

	switch dist {
	case "exp", "exponential":
		log.Printf("service_dist=exponential service_mean_us=%.3f", meanUs)
		return func() time.Duration {
			return time.Duration(rng.ExpFloat64() * meanUs * float64(time.Microsecond))
		}
	case "lognormal", "ln":
		sigma := getenvFloat("SERVICE_LOGN_SIGMA", 0.5)
		if sigma <= 0 {
			sigma = 0.5
		}
		mu := math.Log(meanUs) - 0.5*sigma*sigma
		log.Printf("service_dist=lognormal service_mean_us=%.3f logn_sigma=%.3f", meanUs, sigma)
		return func() time.Duration {
			return time.Duration(math.Exp(rng.NormFloat64()*sigma+mu) * float64(time.Microsecond))
		}
	case "uniform":
		minUs := getenvFloat("SERVICE_MIN_US", meanUs*0.5)
		maxUs := getenvFloat("SERVICE_MAX_US", meanUs*1.5)
		if maxUs < minUs {
			maxUs = minUs
		}
		log.Printf("service_dist=uniform service_min_us=%.3f service_max_us=%.3f", minUs, maxUs)
		return func() time.Duration {
			return time.Duration((minUs + rng.Float64()*(maxUs-minUs)) * float64(time.Microsecond))
		}
	default:
		log.Printf("service_dist=fixed service_us=%.3f", meanUs)
		return func() time.Duration {
			return time.Duration(meanUs * float64(time.Microsecond))
		}
	}
}
