package main

import (
	"log"
	"net/http"
	"runtime"
	"time"
)

type job struct {
	w    http.ResponseWriter
	r    *http.Request
	done chan struct{}
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
	jobs := make(chan job, 1024)
	go func() {
		for j := range jobs {
			// Controlled service demand (tune this later)
			burnCPU(200 * time.Microsecond)

			j.w.WriteHeader(200)
			_, _ = j.w.Write([]byte("ok\n"))
			close(j.done)
		}
	}()
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		d := make(chan struct{})
		jobs <- job{w: w, r: r, done: d}
		<-d
	})
	log.Fatal(http.ListenAndServe(":8080", nil))
}
