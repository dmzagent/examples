package microvm

import (
	"bufio"
	"context"
	"io"
	"net"
	"strings"
	"sync"
	"testing"
	"time"
)

func newTestSandbox(t *testing.T, cfg Config) *Sandbox {
	t.Helper()
	if cfg.ID == "" {
		cfg.ID = "vm-test"
	}
	if cfg.TelemetryBudget == 0 {
		cfg.TelemetryBudget = 1 << 20
	}
	return &Sandbox{cfg: cfg, jailRoot: t.TempDir(), purged: make(chan struct{})}
}

// TestDialGuestDoesNotSwallowThePayload is the reason DialGuest reads the
// acknowledgement a byte at a time.
//
// Firecracker answers "OK <port>\n" and then relays the guest's bytes down the
// same connection, often in the same read. A bufio.Reader would consume the
// acknowledgement AND the first bytes of payload into its buffer, and the
// buffer is discarded with the reader rather than handed back to the
// connection: the payload is simply gone. Switch DialGuest to a bufio.Reader
// and this test fails.
func TestDialGuestDoesNotSwallowThePayload(t *testing.T) {
	s := newTestSandbox(t, Config{ControlPort: 5001})

	ln, err := net.Listen("unix", s.hostVsockPath())
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()

	const payload = "PAYLOAD-THE-GUEST-SENT-IMMEDIATELY\n"
	gotConnect := make(chan string, 1)

	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		line, _ := bufio.NewReader(conn).ReadString('\n')
		gotConnect <- line
		// One write: acknowledgement and payload arrive together, which is
		// exactly the case a buffered read loses.
		_, _ = conn.Write([]byte("OK 1024\n" + payload))
		time.Sleep(50 * time.Millisecond)
	}()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	conn, err := s.DialGuest(ctx, 5001)
	if err != nil {
		t.Fatalf("DialGuest: %v", err)
	}
	defer conn.Close()

	if got := <-gotConnect; got != "CONNECT 5001\n" {
		t.Errorf("sent %q, want %q", got, "CONNECT 5001\n")
	}

	buf := make([]byte, len(payload))
	if _, err := io.ReadFull(conn, buf); err != nil {
		t.Fatalf("payload lost after the acknowledgement: %v", err)
	}
	if string(buf) != payload {
		t.Errorf("payload = %q, want %q", buf, payload)
	}
}

// TestDialGuestReportsNoListener: Firecracker drops the host connection when
// nothing in the guest is listening on the port. That must be an error, not a
// connection that silently reads EOF forever.
func TestDialGuestReportsNoListener(t *testing.T) {
	s := newTestSandbox(t, Config{ControlPort: 5001})
	ln, err := net.Listen("unix", s.hostVsockPath())
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		if conn, err := ln.Accept(); err == nil {
			conn.Close() // what Firecracker does when no guest listener exists
		}
	}()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := s.DialGuest(ctx, 5001); err == nil {
		t.Fatal("a refused connection was reported as success")
	}
}

// TestTelemetryIsStampedByTheHost: the guest supplies the text of a record and
// nothing else. It cannot set the sequence number, the time, or the VM name,
// even by sending something shaped exactly like a record.
func TestTelemetryIsStampedByTheHost(t *testing.T) {
	var mu sync.Mutex
	var got []Record

	s := newTestSandbox(t, Config{
		ID: "vm-alpha",
		OnRecord: func(r Record) {
			mu.Lock()
			got = append(got, r)
			mu.Unlock()
		},
	})

	host, guest := net.Pipe()
	done := make(chan struct{})
	go func() { s.readTelemetry(host); close(done) }()

	before := time.Now().UTC()
	// The third line is a guest trying to forge a record.
	_, _ = io.WriteString(guest, "started\n")
	_, _ = io.WriteString(guest, "working\n")
	_, _ = io.WriteString(guest, `{"vm":"vm-victim","seq":1,"host_time":"1999-01-01T00:00:00Z","line":"forged"}`+"\n")
	guest.Close()
	<-done

	mu.Lock()
	defer mu.Unlock()
	if len(got) != 3 {
		t.Fatalf("got %d records, want 3: %+v", len(got), got)
	}
	for i, r := range got {
		if r.Seq != uint64(i+1) {
			t.Errorf("record %d has seq %d, want %d", i, r.Seq, i+1)
		}
		if r.VM != "vm-alpha" {
			t.Errorf("record %d claims VM %q; the host names the VM", i, r.VM)
		}
		if r.HostTime.Before(before) {
			t.Errorf("record %d is backdated to %s", i, r.HostTime)
		}
	}
	if !strings.Contains(got[2].Line, "forged") {
		t.Errorf("the forged line should survive verbatim as text: %q", got[2].Line)
	}
}

// TestTelemetryBudgetPurges: a guest that floods its own log channel is either
// broken or burying something, and either way it stops being our problem.
func TestTelemetryBudgetPurges(t *testing.T) {
	s := newTestSandbox(t, Config{ID: "vm-noisy", TelemetryBudget: 64})

	host, guest := net.Pipe()
	defer guest.Close()
	go s.readTelemetry(host)

	go func() {
		for i := 0; i < 1000; i++ {
			if _, err := io.WriteString(guest, "flooding the channel\n"); err != nil {
				return
			}
		}
	}()

	select {
	case <-s.Done():
		if !strings.Contains(s.Reason(), "budget") {
			t.Errorf("purged for %q, want a budget reason", s.Reason())
		}
	case <-time.After(5 * time.Second):
		t.Fatal("a guest exceeded its telemetry budget and was not purged")
	}
}

// TestPurgeIsIdempotentAndKeepsTheFirstReason: Purge is called from a timer, a
// telemetry reader and the caller, possibly at once.
func TestPurgeIsIdempotentAndKeepsTheFirstReason(t *testing.T) {
	s := newTestSandbox(t, Config{})
	var purges int
	s.cfg.OnPurge = func(string) { purges++ }

	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(i int) { defer wg.Done(); s.Purge("reason-" + string(rune('a'+i))) }(i)
	}
	wg.Wait()

	if purges != 1 {
		t.Errorf("OnPurge fired %d times, want 1", purges)
	}
	if s.Reason() == "" {
		t.Error("no reason recorded")
	}
	select {
	case <-s.Done():
	default:
		t.Error("Done() did not close")
	}
}

// TestDefaultNetworkMACIsWellFormed. An earlier draft built the MAC from
// "02:dm:za:..." -- readable, and not hexadecimal, so Firecracker rejected the
// interface at PUT time with a message about the device rather than the field.
func TestDefaultNetworkMACIsWellFormed(t *testing.T) {
	for _, i := range []int{0, 1, 255, 256, 4095} {
		mac := DefaultNetwork(i, "eth0").GuestMAC
		hw, err := net.ParseMAC(mac)
		if err != nil {
			t.Fatalf("sandbox %d has MAC %q: %v", i, mac, err)
		}
		if hw[0]&0x01 != 0 {
			t.Errorf("sandbox %d has multicast MAC %q", i, mac)
		}
		if hw[0]&0x02 == 0 {
			t.Errorf("sandbox %d has globally-unique MAC %q; use a locally administered one", i, mac)
		}
	}
}

// TestNetworkAddressesDoNotCollide: every sandbox gets its own namespace, so
// addresses need only be disjoint -- but a collision would silently give two
// guests the same gateway, which is the sort of thing that looks like a flake.
func TestNetworkAddressesDoNotCollide(t *testing.T) {
	seen := map[string]int{}
	for i := 0; i < 512; i++ {
		n := DefaultNetwork(i, "eth0")
		in, out, _ := n.vethNet()
		for _, addr := range []string{n.GuestIP(), n.GatewayIP(), in, out} {
			if net.ParseIP(addr) == nil {
				t.Fatalf("sandbox %d produced unparseable address %q", i, addr)
			}
			if prev, dup := seen[addr]; dup {
				t.Fatalf("sandboxes %d and %d both claim %s", prev, i, addr)
			}
			seen[addr] = i
		}
		if n.NS == "" || n.NetNSPath() == "/var/run/netns/" {
			t.Fatalf("sandbox %d has no namespace name", i)
		}
	}
}

// TestGuestKernelArgsMatchTheGateway: the guest is configured by kernel
// command line, so the image needs no DHCP client. A mismatch here is a guest
// that boots fine and has no route.
func TestGuestKernelArgsMatchTheGateway(t *testing.T) {
	n := DefaultNetwork(7, "eth0")
	args := n.GuestKernelArgs()
	fields := strings.Split(strings.TrimPrefix(args, "ip="), ":")
	if len(fields) != 7 {
		t.Fatalf("ip= has %d fields, want 7: %q", len(fields), args)
	}
	if fields[0] != n.GuestIP() {
		t.Errorf("ip= client %q != GuestIP %q", fields[0], n.GuestIP())
	}
	if fields[2] != n.GatewayIP() {
		t.Errorf("ip= gateway %q != GatewayIP %q", fields[2], n.GatewayIP())
	}
	if fields[6] != "off" {
		t.Errorf("autoconf %q, want off: a sandbox must not DHCP", fields[6])
	}
}

// TestFilterDropsInternalBeforeAllowingPorts is the ordering property the
// whole policy rests on. "Allow 443" above the private-address drop would turn
// the allowance into a route to every internal service that speaks TLS, which
// is most of them.
func TestFilterDropsInternalBeforeAllowingPorts(t *testing.T) {
	n := DefaultNetwork(0, "eth0")
	rules := n.filterRuleset(n.GuestIP())

	drop := strings.Index(rules, "ip daddr {")
	allow := strings.Index(rules, "tcp dport")
	if drop < 0 {
		t.Fatal("no private-address drop in the ruleset")
	}
	if allow < 0 {
		t.Fatal("no port allowance in the ruleset")
	}
	if drop > allow {
		t.Error("the port allowance precedes the private-address drop; internal TLS services are reachable")
	}
	if !strings.Contains(rules, "policy drop") {
		t.Error("the forward chain does not default to drop")
	}
	for _, cidr := range []string{"10.0.0.0/8", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10"} {
		if !strings.Contains(rules, cidr) {
			t.Errorf("%s is reachable from the sandbox", cidr)
		}
	}
}

// TestNoEgressByDefault: a Network with no allowances gives the guest an
// interface and nowhere to use it.
func TestNoEgressByDefault(t *testing.T) {
	n := &Network{Index: 0, NS: "dmz0", TapName: "tap0"}
	guest := n.GuestIP()
	for _, line := range strings.Split(n.filterRuleset(guest), "\n") {
		if strings.Contains(line, guest) && strings.Contains(line, "accept") {
			t.Errorf("a Network with no allowances still permits: %s", strings.TrimSpace(line))
		}
	}
	if c := strings.Count(n.filterRuleset(guest), "policy drop"); c != 2 {
		t.Errorf("%d chains default to drop, want 2 (forward and input)", c)
	}
}
