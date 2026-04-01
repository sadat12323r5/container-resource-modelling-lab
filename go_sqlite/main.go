// go-sqlite — I/O-bound capacity experiment server.
//
// Architecture: FCFS channel-queue with SQLITE_WORKERS goroutines draining it.
// SQLITE_WORKERS=1 (default): true M/G/1 — single worker, one request at a time.
// SQLITE_WORKERS=N: M/G/N — N parallel workers sharing one job queue and one DB.
//
// With N>1, WAL journal mode is enabled and db.SetMaxOpenConns(N) allows N
// concurrent SQLite connections. SQLite serialises concurrent writers at the
// page lock level, so service time increases slightly under write contention;
// this is intentional and matches the real behaviour we want to model with M/G/c.
//
// Service work: INSERT a sensor reading + SELECT last SQLITE_READ_ROWS rows.
// Tests whether DES accuracy depends on service distribution shape vs architecture.
//
// Configuration (environment):
//   PORT              — listen port (default 8080)
//   JOB_QUEUE         — channel buffer / queue capacity (default 1024)
//   SQLITE_PATH       — SQLite DB file path (default /tmp/readings.db)
//   SQLITE_READ_ROWS  — rows fetched per request (default 20)
//   SQLITE_WORKERS    — number of worker goroutines (default 1)
//   CSV_LOG_PATH      — CSV trace output path
package main

import (
	"context"
	"database/sql"
	"encoding/csv"
	"encoding/json"
	"fmt"
	"log"
	"math/rand"
	"net/http"
	"os"
	"strconv"
	"sync"
	"time"

	_ "modernc.org/sqlite"
)

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

func envStr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func envInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

var (
	port           = envStr("PORT", "8080")
	jobQueueSize   = envInt("JOB_QUEUE", 1024)
	sqlitePath     = envStr("SQLITE_PATH", "/tmp/readings.db")
	readRows       = envInt("SQLITE_READ_ROWS", 20)
	sqliteWorkers  = envInt("SQLITE_WORKERS", 1)
	csvLogPath     = envStr("CSV_LOG_PATH", "")
)

// ---------------------------------------------------------------------------
// Job / response types
// ---------------------------------------------------------------------------

type job struct {
	w          http.ResponseWriter
	r          *http.Request
	arrivalNs  int64
	responseCh chan struct{}
}

type traceRow struct {
	arrivalNs  int64
	serviceMs  float64
	queueMs    float64
	responseMs float64
	statusCode int
}

// ---------------------------------------------------------------------------
// CSV logger
// ---------------------------------------------------------------------------

type csvLogger struct {
	mu     sync.Mutex
	file   *os.File
	writer *csv.Writer
}

func newCsvLogger(path string) (*csvLogger, error) {
	if path == "" {
		return &csvLogger{}, nil
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return nil, err
	}
	info, _ := f.Stat()
	w := csv.NewWriter(f)
	if info.Size() == 0 {
		_ = w.Write([]string{"arrival_unix_ns", "service_ms", "queue_ms", "response_ms", "status_code"})
		w.Flush()
	}
	return &csvLogger{file: f, writer: w}, nil
}

func (l *csvLogger) log(row traceRow) {
	if l.writer == nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	_ = l.writer.Write([]string{
		strconv.FormatInt(row.arrivalNs, 10),
		fmt.Sprintf("%.6f", row.serviceMs),
		fmt.Sprintf("%.6f", row.queueMs),
		fmt.Sprintf("%.6f", row.responseMs),
		strconv.Itoa(row.statusCode),
	})
	l.writer.Flush()
}

// ---------------------------------------------------------------------------
// SQLite helpers
// ---------------------------------------------------------------------------

func initDB(db *sql.DB) error {
	// Enable WAL for concurrent writes when using multiple workers
	if sqliteWorkers > 1 {
		if _, err := db.Exec(`PRAGMA journal_mode=WAL`); err != nil {
			return fmt.Errorf("enable WAL: %w", err)
		}
	}
	_, err := db.Exec(`CREATE TABLE IF NOT EXISTS readings (
		id        INTEGER PRIMARY KEY AUTOINCREMENT,
		sensor_id INTEGER NOT NULL,
		value     REAL    NOT NULL,
		ts        INTEGER NOT NULL
	)`)
	return err
}

type serviceResult struct {
	RowsRead int     `json:"rows_read"`
	Mean     float64 `json:"mean"`
	InsertID int64   `json:"insert_id"`
}

// doService runs one unit of I/O-bound work: INSERT then SELECT.
func doService(db *sql.DB, sensorID int, value float64) (serviceResult, error) {
	now := time.Now().UnixNano()

	res, err := db.ExecContext(context.Background(),
		`INSERT INTO readings (sensor_id, value, ts) VALUES (?, ?, ?)`,
		sensorID, value, now)
	if err != nil {
		return serviceResult{}, err
	}
	insertID, _ := res.LastInsertId()

	rows, err := db.QueryContext(context.Background(),
		`SELECT value FROM readings ORDER BY id DESC LIMIT ?`, readRows)
	if err != nil {
		return serviceResult{}, err
	}
	defer rows.Close()

	var sum float64
	var n int
	for rows.Next() {
		var v float64
		if err := rows.Scan(&v); err != nil {
			continue
		}
		sum += v
		n++
	}
	mean := 0.0
	if n > 0 {
		mean = sum / float64(n)
	}
	return serviceResult{RowsRead: n, Mean: mean, InsertID: insertID}, nil
}

// ---------------------------------------------------------------------------
// Worker
// ---------------------------------------------------------------------------

func worker(queue <-chan job, db *sql.DB, logger *csvLogger) {
	rng := rand.New(rand.NewSource(time.Now().UnixNano()))

	for j := range queue {
		queueStart := time.Now()

		sensorID := rng.Intn(100) + 1
		value := rng.Float64()*200 - 100

		if j.r.ContentLength > 0 {
			var body struct {
				SensorID *int     `json:"sensor_id"`
				Value    *float64 `json:"value"`
			}
			if err := json.NewDecoder(j.r.Body).Decode(&body); err == nil {
				if body.SensorID != nil {
					sensorID = *body.SensorID
				}
				if body.Value != nil {
					value = *body.Value
				}
			}
		}

		svcStart := time.Now()
		result, err := doService(db, sensorID, value)
		svcEnd := time.Now()

		serviceMs := float64(svcEnd.Sub(svcStart).Microseconds()) / 1000.0
		queueMs := float64(svcStart.Sub(queueStart).Microseconds()) / 1000.0
		responseMs := float64(svcEnd.Sub(queueStart).Microseconds()) / 1000.0

		statusCode := http.StatusOK
		var respBytes []byte
		if err != nil {
			statusCode = http.StatusInternalServerError
			respBytes, _ = json.Marshal(map[string]string{"error": err.Error()})
		} else {
			respBytes, _ = json.Marshal(map[string]any{
				"ok":         true,
				"rows_read":  result.RowsRead,
				"mean":       result.Mean,
				"insert_id":  result.InsertID,
				"service_ms": fmt.Sprintf("%.3f", serviceMs),
			})
		}

		j.w.Header().Set("Content-Type", "application/json")
		j.w.Header().Set("X-Service-Actual-Ms", fmt.Sprintf("%.3f", serviceMs))
		j.w.WriteHeader(statusCode)
		_, _ = j.w.Write(respBytes)

		logger.log(traceRow{
			arrivalNs:  j.arrivalNs,
			serviceMs:  serviceMs,
			queueMs:    queueMs,
			responseMs: responseMs,
			statusCode: statusCode,
		})

		close(j.responseCh)
	}
}

// ---------------------------------------------------------------------------
// HTTP handlers
// ---------------------------------------------------------------------------

func makeHandler(queue chan<- job) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		arrivalNs := time.Now().UnixNano()

		if r.URL.Path == "/health" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"ok":true}`))
			return
		}
		if r.URL.Path == "/" && r.Method == http.MethodGet {
			w.Header().Set("Content-Type", "application/json")
			info, _ := json.Marshal(map[string]any{
				"service":    "go-sqlite",
				"runtime":    fmt.Sprintf("Go %d-worker FCFS queue", sqliteWorkers),
				"pipeline":   "INSERT reading + SELECT last N rows (SQLite)",
				"read_rows":  readRows,
				"queue_size": jobQueueSize,
				"workers":    sqliteWorkers,
			})
			_, _ = w.Write(info)
			return
		}
		if r.URL.Path != "/process" {
			http.NotFound(w, r)
			return
		}
		if r.Method != http.MethodPost {
			http.Error(w, `{"error":"method_not_allowed"}`, http.StatusMethodNotAllowed)
			return
		}

		responseCh := make(chan struct{})
		j := job{w: w, r: r, arrivalNs: arrivalNs, responseCh: responseCh}

		select {
		case queue <- j:
			<-responseCh
		default:
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(`{"error":"queue full"}`))
		}
	}
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

func main() {
	db, err := sql.Open("sqlite", sqlitePath)
	if err != nil {
		log.Fatalf("open sqlite: %v", err)
	}
	// Allow multiple connections when running multiple workers
	db.SetMaxOpenConns(sqliteWorkers)
	if err := initDB(db); err != nil {
		log.Fatalf("init db: %v", err)
	}

	logger, err := newCsvLogger(csvLogPath)
	if err != nil {
		log.Fatalf("csv logger: %v", err)
	}

	queue := make(chan job, jobQueueSize)
	for i := 0; i < sqliteWorkers; i++ {
		go worker(queue, db, logger)
	}

	log.Printf("go-sqlite :%s  workers=%d  queue=%d  sqlite=%s  read_rows=%d  csv=%q",
		port, sqliteWorkers, jobQueueSize, sqlitePath, readRows, csvLogPath)

	http.HandleFunc("/", makeHandler(queue))
	if err := http.ListenAndServe(":"+port, nil); err != nil {
		log.Fatal(err)
	}
}
