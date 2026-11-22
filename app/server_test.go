package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func newTestServer(cfg Config) *httptest.Server {
	return httptest.NewServer(NewServer(cfg, NewMemoryStore()).Routes())
}

func post(t *testing.T, url, body string) (*http.Response, map[string]any) {
	t.Helper()
	resp, err := http.Post(url+"/orders", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp, out
}

func TestCreateListGet(t *testing.T) {
	ts := newTestServer(Config{Env: "test"})
	defer ts.Close()
	resp, out := post(t, ts.URL, `{"item":"widget","qty":3}`)
	if resp.StatusCode != 201 || out["status"] != "accepted" {
		t.Fatalf("create: %d %v", resp.StatusCode, out)
	}
	id := out["id"].(string)
	r, err := http.Get(ts.URL + "/orders/" + id)
	if err != nil || r.StatusCode != 200 {
		t.Fatalf("get: %v %d", err, r.StatusCode)
	}
	r, _ = http.Get(ts.URL + "/orders")
	var list struct {
		Orders []Order `json:"orders"`
	}
	_ = json.NewDecoder(r.Body).Decode(&list)
	if len(list.Orders) != 1 || list.Orders[0].Item != "widget" {
		t.Fatalf("list: %+v", list)
	}
	r, _ = http.Get(ts.URL + "/orders/ord_missing")
	if r.StatusCode != 404 {
		t.Fatalf("missing order: %d", r.StatusCode)
	}
}

func TestValidation(t *testing.T) {
	ts := newTestServer(Config{Env: "test"})
	defer ts.Close()
	for _, body := range []string{`{}`, `{"item":"","qty":1}`, `{"item":"x","qty":0}`, `{"item":"x","qty":5000}`, `not json`} {
		if resp, _ := post(t, ts.URL, body); resp.StatusCode != 400 {
			t.Fatalf("%s: want 400, got %d", body, resp.StatusCode)
		}
	}
}

func TestFaultKnobsOnlyHitBusinessRoutes(t *testing.T) {
	ts := newTestServer(Config{Env: "test", FailRate: 1.0, ExtraLatency: 20 * time.Millisecond})
	defer ts.Close()
	start := time.Now()
	resp, out := post(t, ts.URL, `{"item":"widget","qty":1}`)
	if resp.StatusCode != 500 || !strings.Contains(out["error"].(string), "injected") {
		t.Fatalf("fail rate 1.0 must fail business routes: %d %v", resp.StatusCode, out)
	}
	if time.Since(start) < 20*time.Millisecond {
		t.Fatal("extra latency not applied")
	}
	for _, p := range []string{"/healthz", "/readyz", "/version", "/metrics"} {
		r, err := http.Get(ts.URL + p)
		if err != nil || r.StatusCode != 200 {
			t.Fatalf("%s must stay healthy during a bad release: %v %d", p, err, r.StatusCode)
		}
	}
}

func TestMetricsExposed(t *testing.T) {
	ts := newTestServer(Config{Env: "test"})
	defer ts.Close()
	post(t, ts.URL, `{"item":"widget","qty":1}`)
	r, _ := http.Get(ts.URL + "/metrics")
	body, _ := io.ReadAll(r.Body)
	for _, want := range []string{`http_requests_total{code="201",route="/orders"}`, "http_request_duration_seconds_bucket", "orders_created_total "} {
		if !strings.Contains(string(body), want) {
			t.Fatalf("metrics missing %q", want)
		}
	}
}
