package main

import (
	"bufio"
	"encoding/csv"
	"log"
	"math"
	"math/rand"
	"net/http"
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

type responseRecorder struct {
	http.ResponseWriter
	status int
	bytes  int
}

func (rr *responseRecorder) WriteHeader(code int) {
	rr.status = code
	rr.ResponseWriter.WriteHeader(code)
}

func (rr *responseRecorder) Write(b []byte) (int, error) {
	if rr.status == 0 {
		rr.status = http.StatusOK
	}
	n, err := rr.ResponseWriter.Write(b)
	rr.bytes += n
	return n, err
}

type job struct {
	id      uint64
	traceID string
	arrival time.Time
	rw      *responseRecorder
	r       *http.Request
	done    chan struct{}
}

func burnCPU(d time.Duration) {
	deadline := time.Now().Add(d)
	x := uint64(0)
	for time.Now().Before(deadline) {
		x = x*1664525 + 1013904223
	}
	_ = x
}

func main() {
	runtime.GOMAXPROCS(1)
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)

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

	jobs := make(chan job, 1024)
	var nextID uint64

	serviceSampler := newServiceSampler()

	logQueueSize := getenvInt("CSV_LOG_QUEUE", 10000)
	flushInterval := time.Duration(getenvInt("CSV_LOG_FLUSH_MS", 1000)) * time.Millisecond
	logCh := make(chan []string, logQueueSize)
	var dropped uint64
	go func() {
		ticker := time.NewTicker(flushInterval)
		defer ticker.Stop()
		pending := 0
		for {
			select {
			case record := <-logCh:
				if err := csvWriter.Write(record); err != nil {
					log.Printf("csv write error: %v", err)
				} else {
					pending++
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
				if droppedCount := atomic.SwapUint64(&dropped, 0); droppedCount > 0 {
					log.Printf("csv logger dropped %d records (queue full)", droppedCount)
				}
			}
		}
	}()

	go func() {
		for j := range jobs {
			serviceStart := time.Now()
			// Controlled service demand (tune this later)
			burnCPU(serviceSampler())
			serviceEnd := time.Now()

			j.rw.Header().Set("X-Request-Id", j.traceID)
			j.rw.WriteHeader(http.StatusOK)
			_, _ = j.rw.Write([]byte("ok\n"))
			responseEnd := time.Now()

			queueMs := serviceStart.Sub(j.arrival).Seconds() * 1000
			serviceMs := serviceEnd.Sub(serviceStart).Seconds() * 1000
			responseMs := responseEnd.Sub(j.arrival).Seconds() * 1000
			status := j.rw.status
			if status == 0 {
				status = http.StatusOK
			}
			record := []string{
				strconv.FormatUint(j.id, 10),
				j.traceID,
				strconv.FormatInt(j.arrival.UnixNano(), 10),
				strconv.FormatInt(serviceStart.UnixNano(), 10),
				strconv.FormatInt(serviceEnd.UnixNano(), 10),
				strconv.FormatInt(responseEnd.UnixNano(), 10),
				strconv.FormatFloat(queueMs, 'f', 3, 64),
				strconv.FormatFloat(serviceMs, 'f', 3, 64),
				strconv.FormatFloat(responseMs, 'f', 3, 64),
				strconv.Itoa(status),
				strconv.Itoa(j.rw.bytes),
			}
			select {
			case logCh <- record:
			default:
				atomic.AddUint64(&dropped, 1)
			}
			close(j.done)
		}
	}()
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		d := make(chan struct{})
		id := atomic.AddUint64(&nextID, 1)
		arrival := time.Now()
		traceID := r.Header.Get("X-Request-Id")
		if traceID == "" {
			traceID = r.Header.Get("X-Vegeta-Seq")
		}
		if traceID == "" {
			traceID = strconv.FormatUint(id, 10)
		}
		rw := &responseRecorder{ResponseWriter: w}
		jobs <- job{id: id, traceID: traceID, arrival: arrival, rw: rw, r: r, done: d}
		<-d
	})
	log.Fatal(http.ListenAndServe(":8080", nil))
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
