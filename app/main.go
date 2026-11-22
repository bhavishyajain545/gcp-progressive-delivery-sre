// orders-api: a small Go service that exists to be delivered safely. It has
// just enough surface for canary verification to mean something: a datastore
// round-trip, Prometheus metrics by route and status, and two fault knobs
// (FAIL_RATE, EXTRA_LATENCY_MS) that a "bad release" sets through Cloud Deploy
// deploy parameters so the pipeline can be shown catching it.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"
)

var Version = "dev"

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		ReplaceAttr: func(_ []string, a slog.Attr) slog.Attr {
			switch a.Key {
			case slog.LevelKey:
				a.Key = "severity"
			case slog.MessageKey:
				a.Key = "message"
			}
			return a
		},
	})))
	cfg := Config{
		Port:         env("PORT", "8080"),
		Project:      env("GOOGLE_CLOUD_PROJECT", ""),
		Store:        env("STORE", "memory"),
		Env:          env("APP_ENV", "dev"),
		FailRate:     envFloat("FAIL_RATE", 0),
		ExtraLatency: time.Duration(envInt("EXTRA_LATENCY_MS", 0)) * time.Millisecond,
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	var store Store
	var err error
	if cfg.Store == "firestore" {
		store, err = NewFirestoreStore(ctx, cfg.Project, "orders-"+cfg.Env)
		if err != nil {
			slog.Error("firestore", "err", err)
			os.Exit(1)
		}
	} else {
		store = NewMemoryStore()
	}
	srv := NewServer(cfg, store)
	hs := &http.Server{Addr: ":" + cfg.Port, Handler: srv.Routes(), ReadHeaderTimeout: 5 * time.Second}
	go func() {
		slog.Info("listening", "port", cfg.Port, "version", Version, "env", cfg.Env, "store", cfg.Store,
			"fail_rate", cfg.FailRate, "extra_latency_ms", cfg.ExtraLatency.Milliseconds())
		if err := hs.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			slog.Error("serve", "err", err)
			os.Exit(1)
		}
	}()
	<-ctx.Done()
	sctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	_ = hs.Shutdown(sctx)
	store.Close()
}

type Config struct {
	Port, Project, Store, Env string
	FailRate                  float64
	ExtraLatency              time.Duration
}

func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func envInt(k string, d int) int {
	if v, err := strconv.Atoi(os.Getenv(k)); err == nil {
		return v
	}
	return d
}

func envFloat(k string, d float64) float64 {
	if v, err := strconv.ParseFloat(os.Getenv(k), 64); err == nil {
		return v
	}
	return d
}
