// Package microvm drives the Firecracker VMM over its REST-on-a-Unix-socket
// API: it stages a jail, boots one microVM against a read-only root
// filesystem, collects that VM's telemetry out-of-band over vsock, and
// destroys the whole thing on a budget or on demand.
//
// Nothing here trusts the guest. The guest cannot name its own log records,
// cannot reach the host filesystem, cannot outlive its budget, and cannot
// write to the image it booted from.
//
// Field names in this file are taken from the Firecracker OpenAPI spec
// (src/firecracker/swagger/firecracker.yaml), not from memory. Two of them
// are commonly written wrong; see the comments on TokenBucket and
// NetworkInterface.
package microvm

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"time"
)

// Client speaks the Firecracker API. Firecracker serves plain HTTP/1.1 over an
// AF_UNIX socket, so the authority in the URL is meaningless and we dial the
// socket directly.
type Client struct {
	http *http.Client
	sock string
}

// NewClient returns a client bound to one VMM's API socket.
func NewClient(sock string) *Client {
	return &Client{
		sock: sock,
		http: &http.Client{
			Transport: &http.Transport{
				DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
					var d net.Dialer
					return d.DialContext(ctx, "unix", sock)
				},
				// Firecracker's API server handles one request at a time.
				// Holding a single connection keeps our ordering equal to
				// its ordering, which matters because configuration is
				// stateful: boot-source before InstanceStart, always.
				MaxConnsPerHost:     1,
				MaxIdleConnsPerHost: 1,
				IdleConnTimeout:     30 * time.Second,
			},
			Timeout: 10 * time.Second,
		},
	}
}

// fault is Firecracker's error body. It returns 400 with this shape for every
// rejected configuration, and the message is the only place that says which
// field was wrong.
type fault struct {
	FaultMessage string `json:"fault_message"`
}

func (c *Client) do(ctx context.Context, method, path string, body any) error {
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("encode %s %s: %w", method, path, err)
		}
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, "http://firecracker"+path, rdr)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	defer resp.Body.Close()

	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
	if resp.StatusCode >= 300 {
		var f fault
		_ = json.Unmarshal(raw, &f)
		if f.FaultMessage == "" {
			f.FaultMessage = string(bytes.TrimSpace(raw))
		}
		return fmt.Errorf("%s %s: %s: %s", method, path, resp.Status, f.FaultMessage)
	}
	return nil
}

// ---------------------------------------------------------------------------
// Throttling
// ---------------------------------------------------------------------------

// TokenBucket is Firecracker's throttle primitive.
//
// The optional burst field is spelled one_time_burst. It is NOT "bucket_size";
// the API rejects unknown fields, so a config written with bucket_size fails at
// PUT time with a fault message about the device, not about the field.
type TokenBucket struct {
	// Size units become available every RefillTime milliseconds.
	Size int64 `json:"size"`
	// OneTimeBurst is spent once, at first use, and never refilled. For
	// containment leave it at zero: a burst is exactly the budget a scanner
	// wants.
	OneTimeBurst int64 `json:"one_time_burst,omitempty"`
	RefillTime   int64 `json:"refill_time"`
}

// Rate reports the sustained units per second this bucket actually admits.
//
// This exists because the pair is easy to misread. {size: 1048576,
// refill_time: 100} is one mebibyte every 100ms, which is 10 MB/s, not the
// 1 MB/s it looks like at a glance. Build buckets with Throttle and assert
// with Rate rather than writing the pair by hand.
func (t *TokenBucket) Rate() int64 {
	if t == nil || t.RefillTime <= 0 {
		return 0
	}
	return t.Size * 1000 / t.RefillTime
}

// Throttle builds a bucket admitting rate units per second, refilled every
// window. A rate of zero or less means "no limit" and yields nil, which
// serialises away via omitempty.
//
// Smaller windows shape more smoothly and tolerate bursts less; 100ms is a
// reasonable default for both bandwidth and operations.
//
// Size is an integer, so not every (rate, window) pair is exactly
// representable. Rounding is always DOWN: for a containment control the safe
// direction is to admit slightly less than asked, never slightly more. Where
// rounding down would cost more than 1% the window widens to one second,
// where every whole rate is exact, at the price of a burstier shape.
func Throttle(rate int64, window time.Duration) *TokenBucket {
	if rate <= 0 {
		return nil
	}
	ms := window.Milliseconds()
	if ms <= 0 {
		ms = 100
	}
	size := rate * ms / 1000
	if size < 1 || (size*1000/ms)*100 < rate*99 {
		ms, size = 1000, rate
	}
	return &TokenBucket{Size: size, RefillTime: ms}
}

// RateLimiter pairs a bandwidth budget (bytes) with an operations budget
// (packets, or block requests).
type RateLimiter struct {
	Bandwidth *TokenBucket `json:"bandwidth,omitempty"`
	Ops       *TokenBucket `json:"ops,omitempty"`
}

// ---------------------------------------------------------------------------
// Configuration models
// ---------------------------------------------------------------------------

// MachineConfig is PUT /machine-config. The SMT field is spelled smt; it was
// ht_enabled before Firecracker v1.0 and that spelling is now rejected.
type MachineConfig struct {
	VcpuCount       int    `json:"vcpu_count"`
	MemSizeMib      int    `json:"mem_size_mib"`
	SMT             bool   `json:"smt"`
	TrackDirtyPages bool   `json:"track_dirty_pages,omitempty"`
	CPUTemplate     string `json:"cpu_template,omitempty"`
	HugePages       string `json:"huge_pages,omitempty"`
}

// BootSource is PUT /boot-source. Paths are resolved inside the jail's chroot,
// not on the host.
type BootSource struct {
	KernelImagePath string `json:"kernel_image_path"`
	BootArgs        string `json:"boot_args,omitempty"`
	InitrdPath      string `json:"initrd_path,omitempty"`
}

// Drive is PUT /drives/{drive_id}.
type Drive struct {
	DriveID      string       `json:"drive_id"`
	PathOnHost   string       `json:"path_on_host"`
	IsRootDevice bool         `json:"is_root_device"`
	IsReadOnly   bool         `json:"is_read_only"`
	CacheType    string       `json:"cache_type,omitempty"`
	IOEngine     string       `json:"io_engine,omitempty"`
	RateLimiter  *RateLimiter `json:"rate_limiter,omitempty"`
}

// NetworkInterface is PUT /network-interfaces/{iface_id}.
//
// A network interface has no field called rate_limiter. It has two, one per
// direction. Drives have the singular field; interfaces do not, and a config
// that sets rate_limiter here is rejected.
type NetworkInterface struct {
	IfaceID       string       `json:"iface_id"`
	HostDevName   string       `json:"host_dev_name"`
	GuestMAC      string       `json:"guest_mac,omitempty"`
	MTU           int          `json:"mtu,omitempty"`
	RxRateLimiter *RateLimiter `json:"rx_rate_limiter,omitempty"`
	TxRateLimiter *RateLimiter `json:"tx_rate_limiter,omitempty"`
}

// Vsock is PUT /vsock. UDSPath is inside the chroot. Guest CIDs start at 3;
// 0, 1 and 2 are reserved, and 2 is the host the guest dials to reach us.
type Vsock struct {
	GuestCID int    `json:"guest_cid"`
	UDSPath  string `json:"uds_path"`
}

// Entropy is PUT /entropy: a virtio-rng device. Without one, a guest that
// wants entropy early either blocks or falls back to something worse.
type Entropy struct {
	RateLimiter *RateLimiter `json:"rate_limiter,omitempty"`
}

// Logger is PUT /logger. This is the VMM's own log, on the host side of the
// boundary; it is not the guest's.
type Logger struct {
	LogPath       string `json:"log_path"`
	Level         string `json:"level,omitempty"`
	ShowLevel     bool   `json:"show_level,omitempty"`
	ShowLogOrigin bool   `json:"show_log_origin,omitempty"`
}

// SerialDevice is PUT /serial: the guest's kernel console.
//
// Note the rate limiter's semantics, which differ from every other device
// here: guest writes over the rate are DROPPED, not queued. That makes the
// serial port unfit as a telemetry channel — a guest that floods it silently
// loses records, which is precisely the guest whose records you want. Use it
// for kernel messages and carry telemetry over vsock.
type SerialDevice struct {
	SerialOutPath string       `json:"serial_out_path,omitempty"`
	RateLimiter   *TokenBucket `json:"rate_limiter,omitempty"`
}

// MMDSConfig is PUT /mmds/config. The metadata service is answered by
// Firecracker inside the virtio-net device: those packets never reach the tap,
// so host firewall rules neither secure nor break it.
type MMDSConfig struct {
	Version           string   `json:"version"`
	NetworkInterfaces []string `json:"network_interfaces"`
	IPv4Address       string   `json:"ipv4_address,omitempty"`
}

type instanceAction struct {
	ActionType string `json:"action_type"`
}

type vmState struct {
	State string `json:"state"`
}

// ---------------------------------------------------------------------------
// Operations
// ---------------------------------------------------------------------------

func (c *Client) SetMachineConfig(ctx context.Context, m MachineConfig) error {
	return c.do(ctx, http.MethodPut, "/machine-config", m)
}

func (c *Client) SetBootSource(ctx context.Context, b BootSource) error {
	return c.do(ctx, http.MethodPut, "/boot-source", b)
}

func (c *Client) AddDrive(ctx context.Context, d Drive) error {
	return c.do(ctx, http.MethodPut, "/drives/"+d.DriveID, d)
}

func (c *Client) AddNetworkInterface(ctx context.Context, n NetworkInterface) error {
	return c.do(ctx, http.MethodPut, "/network-interfaces/"+n.IfaceID, n)
}

func (c *Client) SetVsock(ctx context.Context, v Vsock) error {
	return c.do(ctx, http.MethodPut, "/vsock", v)
}

func (c *Client) SetEntropy(ctx context.Context, e Entropy) error {
	return c.do(ctx, http.MethodPut, "/entropy", e)
}

func (c *Client) SetLogger(ctx context.Context, l Logger) error {
	return c.do(ctx, http.MethodPut, "/logger", l)
}

func (c *Client) SetSerial(ctx context.Context, s SerialDevice) error {
	return c.do(ctx, http.MethodPut, "/serial", s)
}

func (c *Client) SetMMDSConfig(ctx context.Context, m MMDSConfig) error {
	return c.do(ctx, http.MethodPut, "/mmds/config", m)
}

// PutMMDS publishes the metadata document the guest reads at 169.254.169.254.
// This is the right place for a task payload and a short-lived credential: it
// never touches the read-only image, so it cannot persist past the VM.
func (c *Client) PutMMDS(ctx context.Context, doc any) error {
	return c.do(ctx, http.MethodPut, "/mmds", doc)
}

// Start boots the configured machine. Every PUT above must precede it.
func (c *Client) Start(ctx context.Context) error {
	return c.do(ctx, http.MethodPut, "/actions", instanceAction{ActionType: "InstanceStart"})
}

// Pause freezes every vCPU. For an agent that has tripped a rule but whose
// state you want to examine before destroying it, pause first, then snapshot,
// then purge.
func (c *Client) Pause(ctx context.Context) error {
	return c.do(ctx, http.MethodPatch, "/vm", vmState{State: "Paused"})
}

func (c *Client) Resume(ctx context.Context) error {
	return c.do(ctx, http.MethodPatch, "/vm", vmState{State: "Resumed"})
}
