package microvm

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// A fake VMM, so the configuration sequence is asserted on the wire rather
// than on the structs that produced it. The structs agreeing with themselves
// is what a misspelt field name looks like from the inside.
// ---------------------------------------------------------------------------

type call struct {
	Method string
	Path   string
	Body   map[string]any
}

type fakeVMM struct {
	mu    sync.Mutex
	calls []call
}

func (f *fakeVMM) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	var body map[string]any
	_ = json.NewDecoder(r.Body).Decode(&body)
	f.mu.Lock()
	f.calls = append(f.calls, call{r.Method, r.URL.Path, body})
	f.mu.Unlock()
	w.WriteHeader(http.StatusNoContent)
}

func (f *fakeVMM) find(t *testing.T, path string) map[string]any {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, c := range f.calls {
		if c.Path == path {
			return c.Body
		}
	}
	t.Fatalf("no request was made to %s; got %v", path, f.paths())
	return nil
}

func (f *fakeVMM) paths() []string {
	var out []string
	for _, c := range f.calls {
		out = append(out, c.Method+" "+c.Path)
	}
	return out
}

func newFakeVMM(t *testing.T) (*Client, *fakeVMM) {
	t.Helper()
	sock := filepath.Join(t.TempDir(), "api.sock")
	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	f := &fakeVMM{}
	srv := &http.Server{Handler: f}
	go srv.Serve(ln)
	t.Cleanup(func() { _ = srv.Close() })
	return NewClient(sock), f
}

// ---------------------------------------------------------------------------

// assertRate holds a bucket to the contract Throttle documents: never above
// the requested rate, never more than 1% below it.
func assertRate(t *testing.T, got, want int64) {
	t.Helper()
	if got > want {
		t.Errorf("rate %d exceeds the requested %d", got, want)
	}
	if got*100 < want*99 {
		t.Errorf("rate %d is more than 1%% below the requested %d", got, want)
	}
}

// TestThrottleRateRoundTrips pins the arithmetic that is easiest to get wrong
// by an order of magnitude.
func TestThrottleRateRoundTrips(t *testing.T) {
	// Every rate a caller might plausibly ask for, including the ones that
	// do not divide evenly into a 100ms window.
	for _, want := range []int64{1, 7, 53, 1000, 1 << 20, 10 << 20, 12345678} {
		got := Throttle(want, 100*time.Millisecond).Rate()
		// Never admit more than was asked: over-admitting is the direction
		// that costs containment, under-admitting only costs throughput.
		if got > want {
			t.Errorf("Throttle(%d).Rate() = %d, which exceeds the requested rate", want, got)
		}
		if got*100 < want*99 {
			t.Errorf("Throttle(%d).Rate() = %d, more than 1%% below the requested rate", want, got)
		}
	}
	if b := Throttle(0, time.Second); b != nil {
		t.Errorf("Throttle(0) = %+v, want nil (no limit)", b)
	}
}

// TestHandWrittenBucketIsTenTimesFasterThanItLooks is the counter-example the
// helper exists to prevent: the pair that reads like 1 MB/s is 10 MB/s.
func TestHandWrittenBucketIsTenTimesFasterThanItLooks(t *testing.T) {
	handWritten := &TokenBucket{Size: 1048576, RefillTime: 100}
	if got := handWritten.Rate(); got != 10<<20 {
		t.Fatalf("Rate() = %d, want %d", got, 10<<20)
	}
	helper := Throttle(1<<20, 100*time.Millisecond)
	assertRate(t, helper.Rate(), 1<<20)
	if ratio := handWritten.Rate() / helper.Rate(); ratio != 10 {
		t.Fatalf("hand-written bucket is %dx the helper's, want 10x", ratio)
	}
}

// TestTokenBucketFieldNames fails if anyone reintroduces the bucket_size
// spelling, which Firecracker rejects.
func TestTokenBucketFieldNames(t *testing.T) {
	raw, err := json.Marshal(&TokenBucket{Size: 100, OneTimeBurst: 50, RefillTime: 100})
	if err != nil {
		t.Fatal(err)
	}
	var keys map[string]json.RawMessage
	if err := json.Unmarshal(raw, &keys); err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"size", "one_time_burst", "refill_time"} {
		if _, ok := keys[want]; !ok {
			t.Errorf("TokenBucket is missing %q: %s", want, raw)
		}
	}
	if _, ok := keys["bucket_size"]; ok {
		t.Errorf("TokenBucket emitted bucket_size, which the API rejects: %s", raw)
	}
}

// TestConfigureEmitsTheDocumentedWireShape drives the whole configuration
// sequence against a fake VMM and inspects what actually went over the socket.
func TestConfigureEmitsTheDocumentedWireShape(t *testing.T) {
	client, vmm := newFakeVMM(t)

	s := &Sandbox{
		api:    client,
		purged: make(chan struct{}),
		cfg: Config{
			ID: "vm-under-test", VCPUs: 2, MemMiB: 512,
			KernelArgs:         DefaultKernelArgs,
			Net:                DefaultNetwork(0, "eth0"),
			EgressBytesPerSec:  1 << 20,
			EgressOpsPerSec:    1000,
			IngressBytesPerSec: 4 << 20,
			Metadata:           map[string]any{"task": "hello"},
		},
	}
	if err := s.configure(context.Background()); err != nil {
		t.Fatalf("configure: %v", err)
	}

	t.Run("machine config uses smt, not ht_enabled", func(t *testing.T) {
		b := vmm.find(t, "/machine-config")
		if _, ok := b["smt"]; !ok {
			t.Errorf("no smt field: %v", b)
		}
		if _, ok := b["ht_enabled"]; ok {
			t.Errorf("ht_enabled was removed in Firecracker v1.0: %v", b)
		}
		// Asserting the field exists says nothing about its value. Two vCPUs
		// on one physical core share its caches, and a cache shared between
		// tenants is a side channel.
		if b["smt"] != false {
			t.Errorf("smt = %v, want false", b["smt"])
		}
		if b["vcpu_count"] != float64(2) || b["mem_size_mib"] != float64(512) {
			t.Errorf("machine config not carried through: %v", b)
		}
	})

	t.Run("root drive is read only", func(t *testing.T) {
		b := vmm.find(t, "/drives/rootfs")
		if b["is_read_only"] != true {
			t.Errorf("root drive is writable; the guest can persist across sessions: %v", b)
		}
		if b["is_root_device"] != true {
			t.Errorf("root drive not marked as root: %v", b)
		}
	})

	t.Run("network interface throttles both directions", func(t *testing.T) {
		b := vmm.find(t, "/network-interfaces/eth0")
		if _, ok := b["rate_limiter"]; ok {
			t.Errorf("a network interface has no rate_limiter field; that is the drive spelling: %v", b)
		}
		tx, ok := b["tx_rate_limiter"].(map[string]any)
		if !ok {
			t.Fatalf("no tx_rate_limiter: %v", b)
		}
		bw, ok := tx["bandwidth"].(map[string]any)
		if !ok {
			t.Fatalf("no tx bandwidth bucket: %v", tx)
		}
		// Whatever window the helper chose, the bucket must describe 1 MB/s
		// and not the 10 MB/s the hand-written pair would have meant.
		size, refill := int64(bw["size"].(float64)), int64(bw["refill_time"].(float64))
		assertRate(t, (&TokenBucket{Size: size, RefillTime: refill}).Rate(), 1<<20)
		if _, ok := b["rx_rate_limiter"]; !ok {
			t.Errorf("ingress unthrottled: a guest can still pull an arbitrarily large payload in: %v", b)
		}
	})

	t.Run("vsock and entropy are attached", func(t *testing.T) {
		v := vmm.find(t, "/vsock")
		if v["guest_cid"] != float64(3) {
			t.Errorf("guest_cid = %v, want 3 (0-2 are reserved)", v["guest_cid"])
		}
		vmm.find(t, "/entropy")
	})

	t.Run("mmds is v2", func(t *testing.T) {
		c := vmm.find(t, "/mmds/config")
		if c["version"] != "V2" {
			t.Errorf("MMDS version = %v, want V2 (V1 answers any GET, including an SSRF)", c["version"])
		}
		vmm.find(t, "/mmds")
	})

	t.Run("nothing is configured after InstanceStart", func(t *testing.T) {
		if err := s.api.Start(context.Background()); err != nil {
			t.Fatal(err)
		}
		vmm.mu.Lock()
		defer vmm.mu.Unlock()
		for i, c := range vmm.calls {
			if c.Path == "/actions" && i != len(vmm.calls)-1 {
				t.Errorf("InstanceStart at %d of %d: later configuration is rejected", i, len(vmm.calls))
			}
		}
	})
}

// TestAPIFaultsAreReported checks that a rejected field produces Firecracker's
// own message rather than a bare status code, since the message is the only
// thing that names the field.
func TestAPIFaultsAreReported(t *testing.T) {
	sock := filepath.Join(t.TempDir(), "api.sock")
	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"fault_message":"Invalid request method and/or path: unknown field bucket_size"}`))
	})}
	go srv.Serve(ln)
	defer srv.Close()

	err = NewClient(sock).SetMachineConfig(context.Background(), MachineConfig{VcpuCount: 1, MemSizeMib: 128})
	if err == nil {
		t.Fatal("a 400 was reported as success")
	}
	if !strings.Contains(err.Error(), "bucket_size") {
		t.Errorf("fault message was dropped: %v", err)
	}
}
