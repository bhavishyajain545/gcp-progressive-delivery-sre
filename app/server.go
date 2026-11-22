package main

import (
	"context"
	crand "crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math/rand/v2"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

var (
	httpRequests = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "http_requests_total", Help: "Requests by route and status code.",
	}, []string{"route", "code"})
	httpDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name: "http_request_duration_seconds", Help: "Request latency by route.",
		Buckets: []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2, 5},
	}, []string{"route"})
	ordersCreated  = promauto.NewCounter(prometheus.CounterOpts{Name: "orders_created_total", Help: "Orders accepted."})
	faultsInjected = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "faults_injected_total", Help: "Responses failed or delayed on purpose (bad-release drill).",
	}, []string{"kind"})
)

type Server struct {
	cfg   Config
	store Store
}

func NewServer(cfg Config, store Store) *Server { return &Server{cfg: cfg, store: store} }

func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.Handle("POST /orders", s.instrument("/orders", s.createOrder))
	mux.Handle("GET /orders", s.instrument("/orders", s.listOrders))
	mux.Handle("GET /orders/{id}", s.instrument("/orders/{id}", s.getOrder))
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { w.Write([]byte("ok")) })
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		if err := s.store.Ping(r.Context()); err != nil {
			http.Error(w, "store: "+err.Error(), http.StatusServiceUnavailable)
			return
		}
		w.Write([]byte("ready"))
	})
	mux.HandleFunc("GET /version", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, 200, map[string]any{"version": Version, "env": s.cfg.Env, "fail_rate": s.cfg.FailRate,
			"extra_latency_ms": s.cfg.ExtraLatency.Milliseconds()})
	})
	mux.Handle("GET /metrics", promhttp.Handler())
	return mux
}

type statusWriter struct {
	http.ResponseWriter
	code int
}

func (w *statusWriter) WriteHeader(c int) { w.code = c; w.ResponseWriter.WriteHeader(c) }

// instrument records metrics and applies the fault knobs to business routes only
// (never to probes or metrics, so a bad release still looks "healthy" to kubelet:
// exactly the kind of failure canary verification exists to catch).
func (s *Server) instrument(route string, h http.HandlerFunc) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w, code: 200}
		if s.cfg.ExtraLatency > 0 {
			faultsInjected.WithLabelValues("latency").Inc()
			time.Sleep(s.cfg.ExtraLatency)
		}
		if s.cfg.FailRate > 0 && rand.Float64() < s.cfg.FailRate {
			faultsInjected.WithLabelValues("error").Inc()
			writeJSON(sw, http.StatusInternalServerError, map[string]string{"error": "injected failure (FAIL_RATE)"})
		} else {
			h(sw, r)
		}
		httpRequests.WithLabelValues(route, strconv.Itoa(sw.code)).Inc()
		httpDuration.WithLabelValues(route).Observe(time.Since(start).Seconds())
	})
}

func (s *Server) createOrder(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Item string `json:"item"`
		Qty  int    `json:"qty"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&in); err != nil {
		writeJSON(w, 400, map[string]string{"error": "bad JSON: " + err.Error()})
		return
	}
	in.Item = strings.TrimSpace(in.Item)
	if in.Item == "" || in.Qty <= 0 || in.Qty > 1000 {
		writeJSON(w, 400, map[string]string{"error": "item required, qty 1-1000"})
		return
	}
	o := Order{ID: newID(), Item: in.Item, Qty: in.Qty, Status: "accepted", CreatedAt: time.Now().UTC(), Version: Version}
	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()
	if err := s.store.Create(ctx, o); err != nil {
		writeJSON(w, 503, map[string]string{"error": "store: " + err.Error()})
		return
	}
	ordersCreated.Inc()
	writeJSON(w, 201, o)
}

func (s *Server) listOrders(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()
	out, err := s.store.List(ctx, 20)
	if err != nil {
		writeJSON(w, 503, map[string]string{"error": "store: " + err.Error()})
		return
	}
	if out == nil {
		out = []Order{}
	}
	writeJSON(w, 200, map[string]any{"orders": out, "served_by_version": Version})
}

func (s *Server) getOrder(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()
	o, err := s.store.Get(ctx, r.PathValue("id"))
	if errors.Is(err, ErrNotFound) {
		writeJSON(w, 404, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		writeJSON(w, 503, map[string]string{"error": "store: " + err.Error()})
		return
	}
	writeJSON(w, 200, o)
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func newID() string {
	b := make([]byte, 8)
	_, _ = crand.Read(b)
	return "ord_" + hex.EncodeToString(b)
}
