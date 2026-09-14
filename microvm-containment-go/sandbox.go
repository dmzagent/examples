package microvm

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

// DefaultKernelArgs boots a read-only root with the emulated hardware
// Firecracker does not have switched off, so the guest kernel does not spend
// boot time probing for it.
//
//	pci=off            no PCI bus exists; probing it is wasted milliseconds
//	i8042.*            the i8042 exists only to deliver the reset button
//	reboot=k panic=1   any guest fault becomes an exit, which becomes a purge
//	ro                 the block device is read-only; say so to the mounter too
const DefaultKernelArgs = "console=ttyS0 reboot=k panic=1 pci=off nomodule " +
	"i8042.noaux i8042.nomux i8042.nopnp i8042.dumbkbd " +
	"random.trust_cpu=on root=/dev/vda ro"

// Config describes one sandbox. Zero values are filled in by Launch where a
// sane default exists; the paths and the identity have none.
type Config struct {
	// ID names the jail and the VM. The jailer accepts [a-zA-Z0-9-]{1,64}.
	ID string

	FirecrackerBin string // host path, e.g. /usr/bin/firecracker
	JailerBin      string // host path, e.g. /usr/bin/jailer
	ChrootBase     string // e.g. /srv/jail
	UID, GID       int    // unprivileged identity the VMM runs as

	// KernelImage and RootFS are host paths. Both are linked into the jail;
	// the rootfs is attached read-only and may be shared by every VM on the
	// box, because none of them can write to it.
	KernelImage string
	KernelArgs  string
	RootFS      string

	VCPUs  int
	MemMiB int

	Net *Network // nil runs the VM with no network interface at all

	// Throttles. Zero means unlimited, which for an untrusted guest is a
	// decision worth making explicitly rather than by omission.
	EgressBytesPerSec  int64
	EgressOpsPerSec    int64
	IngressBytesPerSec int64
	IngressOpsPerSec   int64
	DiskBytesPerSec    int64

	// TelemetryPort is the vsock port the guest pushes records to.
	// ControlPort is the vsock port the guest listens on for its task.
	TelemetryPort uint32
	ControlPort   uint32

	// TelemetryBudget caps how many bytes of telemetry this VM may emit
	// before we stop reading and purge it. A guest that floods its own log
	// channel is either broken or trying to bury something.
	TelemetryBudget int64

	// Budget is a hard wall-clock cap. When it expires the VM is destroyed
	// wherever it had got to.
	Budget time.Duration

	// Metadata is published to MMDS for the guest to read at boot.
	Metadata any

	// OnRecord receives each telemetry record, on the host, already stamped
	// with host-side truth. OnPurge fires once, with the reason.
	OnRecord func(Record)
	OnPurge  func(reason string)
}

// Record is one line of guest telemetry as the host chooses to remember it.
//
// Seq and HostTime are assigned here and are not readable or writable by the
// guest. That is the whole point of collecting out-of-band: the guest supplies
// Line and nothing else, so it cannot backdate a record, renumber a sequence
// to hide a gap, or claim to be another VM.
type Record struct {
	VM        string    `json:"vm"`
	Seq       uint64    `json:"seq"`
	HostTime  time.Time `json:"host_time"`
	Line      string    `json:"line"`
	Truncated bool      `json:"truncated,omitempty"`
}

// Sandbox is one microVM and everything the host holds on its behalf.
type Sandbox struct {
	cfg      Config
	jailRoot string
	api      *Client
	cmd      *exec.Cmd

	telemetry net.Listener
	seq       atomic.Uint64
	emitted   atomic.Int64

	purgeOnce sync.Once
	purged    chan struct{}
	reason    atomic.Value // string

	mu      sync.Mutex
	cleanup []func() error
}

// New validates a Config and fills in defaults. It touches nothing.
func New(cfg Config) (*Sandbox, error) {
	if cfg.ID == "" {
		return nil, errors.New("microvm: Config.ID is required")
	}
	for _, f := range []struct{ name, val string }{
		{"FirecrackerBin", cfg.FirecrackerBin},
		{"JailerBin", cfg.JailerBin},
		{"ChrootBase", cfg.ChrootBase},
		{"KernelImage", cfg.KernelImage},
		{"RootFS", cfg.RootFS},
	} {
		if f.val == "" {
			return nil, fmt.Errorf("microvm: Config.%s is required", f.name)
		}
	}
	if cfg.KernelArgs == "" {
		cfg.KernelArgs = DefaultKernelArgs
	}
	if cfg.VCPUs == 0 {
		cfg.VCPUs = 1
	}
	if cfg.MemMiB == 0 {
		cfg.MemMiB = 256
	}
	if cfg.TelemetryPort == 0 {
		cfg.TelemetryPort = 5000
	}
	if cfg.ControlPort == 0 {
		cfg.ControlPort = 5001
	}
	if cfg.TelemetryBudget == 0 {
		cfg.TelemetryBudget = 16 << 20
	}
	if cfg.Budget == 0 {
		cfg.Budget = 5 * time.Minute
	}
	if cfg.UID == 0 || cfg.GID == 0 {
		return nil, errors.New("microvm: Config.UID/GID must be an unprivileged identity, not root")
	}

	// The jailer derives the chroot from the exec file's base name.
	jailRoot := filepath.Join(cfg.ChrootBase, filepath.Base(cfg.FirecrackerBin), cfg.ID, "root")

	return &Sandbox{
		cfg:      cfg,
		jailRoot: jailRoot,
		purged:   make(chan struct{}),
	}, nil
}

// JailRoot is the host path of the guest's entire visible filesystem.
func (s *Sandbox) JailRoot() string { return s.jailRoot }

// Done closes when the sandbox has been purged.
func (s *Sandbox) Done() <-chan struct{} { return s.purged }

// Reason reports why the sandbox was purged, once it has been.
func (s *Sandbox) Reason() string {
	if r, ok := s.reason.Load().(string); ok {
		return r
	}
	return ""
}

func (s *Sandbox) defer_(fn func() error) {
	s.mu.Lock()
	s.cleanup = append(s.cleanup, fn)
	s.mu.Unlock()
}

// Launch brings the sandbox all the way up: jail, VMM, devices, telemetry,
// boot. On any error it purges whatever it had already built, so a failed
// Launch leaves nothing behind.
func (s *Sandbox) Launch(ctx context.Context) (err error) {
	defer func() {
		if err != nil {
			s.Purge("launch failed: " + err.Error())
		}
	}()

	if err := s.startJailer(ctx); err != nil {
		return err
	}
	if err := s.waitForAPI(ctx, 10*time.Second); err != nil {
		return err
	}
	if err := s.stage(); err != nil {
		return err
	}
	// The listener has to exist before the guest dials, and the guest exists
	// the instant InstanceStart returns. Bind it while there is still no
	// guest to race with.
	if err := s.serveTelemetry(); err != nil {
		return err
	}
	if err := s.configure(ctx); err != nil {
		return err
	}
	if err := s.api.Start(ctx); err != nil {
		return fmt.Errorf("InstanceStart: %w", err)
	}

	s.armBudget()
	return nil
}

// startJailer runs the VMM under the jailer, which chroots it, drops it to an
// unprivileged uid/gid, puts it in fresh namespaces and a cgroup, and installs
// a seccomp filter. A guest that escapes the kernel lands here: an empty
// directory owned by nobody, with no network but its own tap.
func (s *Sandbox) startJailer(ctx context.Context) error {
	args := []string{
		"--id", s.cfg.ID,
		"--exec-file", s.cfg.FirecrackerBin,
		"--uid", fmt.Sprint(s.cfg.UID),
		"--gid", fmt.Sprint(s.cfg.GID),
		"--chroot-base-dir", s.cfg.ChrootBase,
		"--cgroup-version", "2",
		// A new PID namespace makes the VMM its namespace's init, so killing
		// it reaps every descendant with it and nothing can be orphaned into
		// the host's tree.
		"--new-pid-ns",
	}
	if s.cfg.Net != nil {
		args = append(args, "--netns", s.cfg.Net.NetNSPath())
	}
	args = append(args, "--",
		"--api-sock", "/run/firecracker.socket",
		"--log-path", "/run/firecracker.log",
		"--level", "Warn",
	)

	cmd := exec.Command(s.cfg.JailerBin, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{
		// Our own process group, so one kill takes the whole tree even if
		// the PID namespace is not in play.
		Setpgid: true,
		// If this orchestrator dies, the VMM dies with it. An agent must not
		// be able to outlive the thing that was supposed to be watching it.
		Pdeathsig: syscall.SIGKILL,
	}
	cmd.Stdout = io.Discard
	cmd.Stderr = os.Stderr

	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start jailer: %w", err)
	}
	s.cmd = cmd
	s.api = NewClient(s.apiSocket())

	// The jail directory is ours to remove; the jailer will not do it.
	s.defer_(func() error {
		return os.RemoveAll(filepath.Dir(s.jailRoot))
	})
	return nil
}

func (s *Sandbox) apiSocket() string {
	return filepath.Join(s.jailRoot, "run", "firecracker.socket")
}

// hostVsockPath is where Firecracker creates the multiplexing socket. Guest
// connections to port N arrive on a separate listener at "<path>_N".
func (s *Sandbox) hostVsockPath() string {
	return filepath.Join(s.jailRoot, "v.sock")
}

func (s *Sandbox) waitForAPI(ctx context.Context, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	sock := s.apiSocket()
	for time.Now().Before(deadline) {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		// Existence is not readiness: the socket file appears before the
		// server accepts. Dial it.
		if c, err := net.Dial("unix", sock); err == nil {
			c.Close()
			return nil
		}
		if s.cmd.ProcessState != nil {
			return fmt.Errorf("VMM exited before its API came up: %s", s.cmd.ProcessState)
		}
		time.Sleep(5 * time.Millisecond)
	}
	return fmt.Errorf("API socket %s did not come up within %s", sock, timeout)
}

// stage puts the kernel and the root filesystem inside the chroot, because a
// chrooted VMM cannot open anything outside it.
//
// Hard links, not copies: the rootfs is attached read-only, so every VM on the
// host can share one inode. Copying a 200MB image per VM is the difference
// between a sandbox per task and a sandbox per hour.
func (s *Sandbox) stage() error {
	for _, f := range []struct{ src, dst string }{
		{s.cfg.KernelImage, filepath.Join(s.jailRoot, "vmlinux")},
		{s.cfg.RootFS, filepath.Join(s.jailRoot, "rootfs.ext4")},
	} {
		if err := os.MkdirAll(filepath.Dir(f.dst), 0o750); err != nil {
			return err
		}
		if err := link(f.src, f.dst); err != nil {
			return fmt.Errorf("stage %s: %w", f.src, err)
		}
		if err := os.Chown(f.dst, s.cfg.UID, s.cfg.GID); err != nil {
			return fmt.Errorf("chown %s: %w", f.dst, err)
		}
	}
	return nil
}

// link hard-links src to dst, falling back to a copy across filesystems.
func link(src, dst string) error {
	if err := os.Link(src, dst); err == nil {
		return nil
	} else if !errors.Is(err, syscall.EXDEV) {
		return err
	}
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o440)
	if err != nil {
		return err
	}
	defer out.Close()
	if _, err := io.Copy(out, in); err != nil {
		return err
	}
	return out.Sync()
}

// configure issues every PUT the machine needs, in the order Firecracker
// requires: everything before InstanceStart, boot source before anything that
// depends on the kernel.
func (s *Sandbox) configure(ctx context.Context) error {
	if err := s.api.SetMachineConfig(ctx, MachineConfig{
		VcpuCount:  s.cfg.VCPUs,
		MemSizeMib: s.cfg.MemMiB,
		// Simultaneous multithreading off: two vCPUs sharing a physical core
		// share its caches, and cache sharing between tenants is a side
		// channel, not a performance tuning question.
		SMT: false,
	}); err != nil {
		return err
	}

	if err := s.api.SetBootSource(ctx, BootSource{
		KernelImagePath: "/vmlinux",
		BootArgs:        s.cfg.KernelArgs,
	}); err != nil {
		return err
	}

	if err := s.api.AddDrive(ctx, Drive{
		DriveID:      "rootfs",
		PathOnHost:   "/rootfs.ext4",
		IsRootDevice: true,
		// The single most important flag in this file. The guest cannot
		// modify the image it booted from, so it cannot leave a backdoor for
		// the next agent to boot the same image.
		IsReadOnly:  true,
		RateLimiter: rateLimiter(s.cfg.DiskBytesPerSec, 0),
	}); err != nil {
		return err
	}

	if s.cfg.Net != nil {
		if err := s.api.AddNetworkInterface(ctx, NetworkInterface{
			IfaceID:     "eth0",
			HostDevName: s.cfg.Net.TapName,
			GuestMAC:    s.cfg.Net.GuestMAC,
			// Two directions, two limiters. Throttling egress alone leaves a
			// guest free to pull an arbitrarily large payload in.
			TxRateLimiter: rateLimiter(s.cfg.EgressBytesPerSec, s.cfg.EgressOpsPerSec),
			RxRateLimiter: rateLimiter(s.cfg.IngressBytesPerSec, s.cfg.IngressOpsPerSec),
		}); err != nil {
			return err
		}
	}

	if err := s.api.SetVsock(ctx, Vsock{
		// CID 3 is the lowest legal guest CID. Each VM has its own vsock
		// device and its own host socket, so the CID need not be unique
		// across the fleet.
		GuestCID: 3,
		UDSPath:  "/v.sock",
	}); err != nil {
		return err
	}

	if err := s.api.SetEntropy(ctx, Entropy{}); err != nil {
		return err
	}

	if s.cfg.Metadata != nil && s.cfg.Net != nil {
		if err := s.api.SetMMDSConfig(ctx, MMDSConfig{
			// V2 requires the guest to PUT for a token before it may GET,
			// which stops a confused-deputy request (an SSRF in the agent's
			// own code) from reading the metadata by accident.
			Version:           "V2",
			NetworkInterfaces: []string{"eth0"},
			IPv4Address:       "169.254.169.254",
		}); err != nil {
			return err
		}
		if err := s.api.PutMMDS(ctx, s.cfg.Metadata); err != nil {
			return err
		}
	}
	return nil
}

func rateLimiter(bytesPerSec, opsPerSec int64) *RateLimiter {
	b := Throttle(bytesPerSec, 100*time.Millisecond)
	o := Throttle(opsPerSec, 100*time.Millisecond)
	if b == nil && o == nil {
		return nil
	}
	return &RateLimiter{Bandwidth: b, Ops: o}
}

// ---------------------------------------------------------------------------
// Out-of-band telemetry
// ---------------------------------------------------------------------------

// serveTelemetry binds the host side of the guest's log channel.
//
// Firecracker's vsock has two directions with two different mechanics. For
// guest-initiated connections — this one — the host listens on a Unix socket
// named "<uds_path>_<port>" and Firecracker connects to it when the guest
// dials CID 2 on that port. If the socket is absent the guest gets a reset,
// so this must be bound before the guest runs.
func (s *Sandbox) serveTelemetry() error {
	path := fmt.Sprintf("%s_%d", s.hostVsockPath(), s.cfg.TelemetryPort)
	_ = os.Remove(path)

	ln, err := net.Listen("unix", path)
	if err != nil {
		return fmt.Errorf("bind telemetry socket: %w", err)
	}
	// Firecracker runs as the jailed uid and must be able to connect here.
	if err := os.Chown(path, s.cfg.UID, s.cfg.GID); err != nil {
		ln.Close()
		return fmt.Errorf("chown telemetry socket: %w", err)
	}
	s.telemetry = ln
	s.defer_(func() error { return ln.Close() })

	go s.acceptTelemetry(ln)
	return nil
}

func (s *Sandbox) acceptTelemetry(ln net.Listener) {
	for {
		conn, err := ln.Accept()
		if err != nil {
			return // listener closed by Purge
		}
		go s.readTelemetry(conn)
	}
}

// readTelemetry consumes one guest connection under a hard budget.
//
// The reader is itself an attack surface: the guest chooses how much to send
// and how long each line is. Both are bounded here, and exhausting the budget
// is treated as a containment event rather than as a logging inconvenience.
func (s *Sandbox) readTelemetry(conn net.Conn) {
	defer conn.Close()

	const maxLine = 64 << 10
	br := bufio.NewReaderSize(conn, 4096)

	for {
		remaining := s.cfg.TelemetryBudget - s.emitted.Load()
		if remaining <= 0 {
			s.Purge("telemetry budget exhausted")
			return
		}

		line, err := br.ReadString('\n')
		truncated := false
		if len(line) > maxLine {
			line, truncated = line[:maxLine], true
		}
		s.emitted.Add(int64(len(line)))

		if line != "" && s.cfg.OnRecord != nil {
			s.cfg.OnRecord(Record{
				VM:  s.cfg.ID,
				Seq: s.seq.Add(1),
				// Host time, not guest time. A guest's clock is a guest's
				// claim; the ordering of an incident report must not be.
				HostTime:  time.Now().UTC(),
				Line:      trimEOL(line),
				Truncated: truncated,
			})
		}
		if err != nil {
			return
		}
	}
}

func trimEOL(s string) string {
	for len(s) > 0 && (s[len(s)-1] == '\n' || s[len(s)-1] == '\r') {
		s = s[:len(s)-1]
	}
	return s
}

// DialGuest opens a host-initiated connection to a port the guest is listening
// on, for handing it work and reading the result.
//
// Host-initiated connections use the other half of the vsock protocol: connect
// to the multiplexing socket, send "CONNECT <port>\n", and expect "OK <n>\n".
func (s *Sandbox) DialGuest(ctx context.Context, port uint32) (net.Conn, error) {
	var d net.Dialer
	conn, err := d.DialContext(ctx, "unix", s.hostVsockPath())
	if err != nil {
		return nil, fmt.Errorf("dial vsock mux: %w", err)
	}
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	}
	if _, err := fmt.Fprintf(conn, "CONNECT %d\n", port); err != nil {
		conn.Close()
		return nil, err
	}

	// Read the acknowledgement one byte at a time. A buffered reader would
	// consume past the newline into the guest's first bytes of payload, and
	// they would be gone: the buffer is discarded with the reader, not
	// returned to the connection.
	var ack []byte
	buf := make([]byte, 1)
	for len(ack) < 64 {
		if _, err := io.ReadFull(conn, buf); err != nil {
			conn.Close()
			return nil, fmt.Errorf("vsock CONNECT: no acknowledgement: %w", err)
		}
		if buf[0] == '\n' {
			break
		}
		ack = append(ack, buf[0])
	}
	if len(ack) < 2 || string(ack[:2]) != "OK" {
		conn.Close()
		return nil, fmt.Errorf("vsock CONNECT %d refused: %q (no guest listener?)", port, ack)
	}
	_ = conn.SetDeadline(time.Time{})
	return conn, nil
}

// SendTask hands the guest a JSON payload on the control port and reads the
// single JSON response back, under the caller's deadline.
func (s *Sandbox) SendTask(ctx context.Context, task any, result any) error {
	conn, err := s.DialGuest(ctx, s.cfg.ControlPort)
	if err != nil {
		return err
	}
	defer conn.Close()
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	}

	if err := json.NewEncoder(conn).Encode(task); err != nil {
		return fmt.Errorf("send task: %w", err)
	}
	if uc, ok := conn.(*net.UnixConn); ok {
		_ = uc.CloseWrite() // the guest reads to EOF
	}
	// Bound the reply: the guest chooses its length.
	return json.NewDecoder(io.LimitReader(conn, 1<<20)).Decode(result)
}

// ---------------------------------------------------------------------------
// Purge
// ---------------------------------------------------------------------------

func (s *Sandbox) armBudget() {
	timer := time.AfterFunc(s.cfg.Budget, func() {
		s.Purge(fmt.Sprintf("wall-clock budget of %s expired", s.cfg.Budget))
	})
	s.defer_(func() error { timer.Stop(); return nil })
}

// Purge destroys the sandbox. It is safe to call from anywhere, any number of
// times; the first reason wins.
//
// There is no graceful path here on purpose. SendCtrlAltDel asks the guest to
// shut itself down, which is a request an untrusted guest may decline. SIGKILL
// to the VMM is not a request: the kernel tears down the KVM file descriptors,
// and the guest's memory returns to the host allocator, which zeroes a page
// before any other process can see it.
func (s *Sandbox) Purge(reason string) {
	s.purgeOnce.Do(func() {
		s.reason.Store(reason)

		if s.cmd != nil && s.cmd.Process != nil {
			pid := s.cmd.Process.Pid
			// Negative PID: the whole process group, so the jailer and the
			// VMM it forked both go.
			_ = syscall.Kill(-pid, syscall.SIGKILL)
			_ = syscall.Kill(pid, syscall.SIGKILL)

			// Belt and braces: the jailer put the VMM in a cgroup, and
			// cgroup.kill is the only teardown that cannot be raced by a
			// process forking as it dies.
			_ = os.WriteFile(
				filepath.Join("/sys/fs/cgroup", filepath.Base(s.cfg.FirecrackerBin), s.cfg.ID, "cgroup.kill"),
				[]byte("1"), 0o200,
			)
			_, _ = s.cmd.Process.Wait()
		}

		s.mu.Lock()
		cleanups := s.cleanup
		s.cleanup = nil
		s.mu.Unlock()
		for i := len(cleanups) - 1; i >= 0; i-- {
			_ = cleanups[i]()
		}

		if s.cfg.OnPurge != nil {
			s.cfg.OnPurge(reason)
		}
		close(s.purged)
	})
}
