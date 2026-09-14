package microvm

import (
	"fmt"
	"os/exec"
	"path/filepath"
	"strings"
)

// Network is one microVM's entire reachable world: a network namespace holding
// a single tap device, a veth pair out to the host, and the policy that says
// where the guest may go.
//
// This type, not the rate limiter, is what contains lateral movement. A token
// bucket decides how FAST a guest may reach the internal network; the filter
// below decides WHETHER it may. Throttling a subnet scan to 1,000 packets a
// second still scans the subnet — it just takes until tomorrow, and an agent
// is patient. Rate limiting is a blast-radius control layered on top of an
// access decision, never a substitute for one.
type Network struct {
	// Index gives each sandbox a disjoint pair of /30s. One namespace per VM
	// means a guest cannot see, ARP for, or route to another guest's tap: the
	// isolation is the namespace, and the addresses need only not collide.
	Index int

	NS       string // namespace name
	TapName  string // tap inside the namespace
	GuestMAC string

	// Uplink is the host interface egress is masqueraded out of.
	Uplink string

	// AllowTCPPorts and AllowUDPPorts are the only destinations the guest may
	// open. Empty means the guest has a network interface and nowhere to use
	// it, which is the right default for a task that does not need egress.
	AllowTCPPorts []int
	AllowUDPPorts []int
}

// DefaultNetwork returns a namespace for sandbox n allowing outbound HTTPS and
// DNS, and nothing else.
func DefaultNetwork(n int, uplink string) *Network {
	return &Network{
		Index:   n,
		NS:      fmt.Sprintf("dmz%d", n),
		TapName: "tap0",
		// Locally administered (low bit of the first octet clear = unicast,
		// second bit set = locally administered). Derived from the index so a
		// capture on the host names the sandbox it came from.
		GuestMAC:      fmt.Sprintf("02:FC:00:00:%02x:%02x", (n>>8)&0xff, n&0xff),
		Uplink:        uplink,
		AllowTCPPorts: []int{443},
		AllowUDPPorts: []int{53},
	}
}

// NetNSPath is the path the jailer is handed with --netns.
func (n *Network) NetNSPath() string { return filepath.Join("/var/run/netns", n.NS) }

// guestAddr, gatewayAddr and the veth addresses are all derived from Index so
// that N sandboxes never collide. Each pair is a /30: four addresses, two
// usable, which is exactly a point-to-point link and no room for a third party.
func (n *Network) guestNet() (guest, gw string) {
	b := n.Index * 4
	return fmt.Sprintf("172.30.%d.%d", b/256, b%256+2), fmt.Sprintf("172.30.%d.%d", b/256, b%256+1)
}

func (n *Network) vethNet() (inner, outer, network string) {
	b := n.Index*4 + 2048
	return fmt.Sprintf("172.31.%d.%d", b/256, b%256+2),
		fmt.Sprintf("172.31.%d.%d", b/256, b%256+1),
		fmt.Sprintf("172.31.%d.%d/30", b/256, b%256)
}

// applyHostNAT masquerades one sandbox's link network out of the uplink. The
// table is named per sandbox so tearing one down cannot remove another's rule.
func (n *Network) applyHostNAT() error {
	_, _, network := n.vethNet()
	ruleset := fmt.Sprintf(`
table inet dmznat%d {
  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
    ip saddr %s oifname "%s" masquerade
  }
}
`, n.Index, network, n.Uplink)

	cmd := exec.Command("nft", "-f", "-")
	cmd.Stdin = strings.NewReader(ruleset)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("apply host NAT: %w: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

// GuestIP is the address the guest configures on eth0.
func (n *Network) GuestIP() string { g, _ := n.guestNet(); return g }

// GatewayIP is the guest's default route.
func (n *Network) GatewayIP() string { _, gw := n.guestNet(); return gw }

func run(name string, args ...string) error {
	out, err := exec.Command(name, args...).CombinedOutput()
	if err != nil {
		return fmt.Errorf("%s %s: %w: %s", name, strings.Join(args, " "), err, strings.TrimSpace(string(out)))
	}
	return nil
}

func (n *Network) nsExec(args ...string) error {
	return run("ip", append([]string{"netns", "exec", n.NS}, args...)...)
}

// Up builds the namespace. It is the host's work, done as root, before the
// jailer drops privileges — the VMM itself never has the capability to create
// a network device.
func (n *Network) Up() error {
	guestIP, gwIP := n.guestNet()
	vethIn, vethOut, _ := n.vethNet()
	vethH := fmt.Sprintf("vh%d", n.Index)
	vethN := fmt.Sprintf("vn%d", n.Index)

	steps := [][]string{
		{"ip", "netns", "add", n.NS},
		{"ip", "-n", n.NS, "link", "set", "lo", "up"},

		// The guest's tap, inside the namespace.
		{"ip", "-n", n.NS, "tuntap", "add", n.TapName, "mode", "tap"},
		{"ip", "-n", n.NS, "addr", "add", gwIP + "/30", "dev", n.TapName},
		{"ip", "-n", n.NS, "link", "set", n.TapName, "up"},

		// A veth out to the host.
		{"ip", "link", "add", vethH, "type", "veth", "peer", "name", vethN},
		{"ip", "link", "set", vethN, "netns", n.NS},
		{"ip", "addr", "add", vethOut + "/30", "dev", vethH},
		{"ip", "link", "set", vethH, "up"},
		{"ip", "-n", n.NS, "addr", "add", vethIn + "/30", "dev", vethN},
		{"ip", "-n", n.NS, "link", "set", vethN, "up"},
		{"ip", "-n", n.NS, "route", "add", "default", "via", vethOut},
	}
	for _, s := range steps {
		if err := run(s[0], s[1:]...); err != nil {
			_ = n.Down()
			return err
		}
	}

	if err := n.nsExec("sysctl", "-qw", "net.ipv4.ip_forward=1"); err != nil {
		_ = n.Down()
		return err
	}

	if err := n.applyFilter(guestIP); err != nil {
		_ = n.Down()
		return err
	}

	// Masquerade the namespace behind the host's uplink. The host never sees
	// the guest's address, so a leak of the host's connection table says
	// nothing about which sandbox opened what.
	if n.Uplink != "" {
		if err := n.applyHostNAT(); err != nil {
			_ = n.Down()
			return err
		}
	}

	return nil
}

// applyFilter installs the namespace's forward policy: deny by default, deny
// every internal destination explicitly, then allow the short list of ports
// the task actually needs.
func (n *Network) applyFilter(guestIP string) error {
	cmd := exec.Command("ip", "netns", "exec", n.NS, "nft", "-f", "-")
	cmd.Stdin = strings.NewReader(n.filterRuleset(guestIP))
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("apply nft policy: %w: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

// filterRuleset is separated from applying it so the ordering property below
// can be asserted without a namespace, a root uid, or nft on the box.
func (n *Network) filterRuleset(guestIP string) string {
	tcp := portSet(n.AllowTCPPorts)
	udp := portSet(n.AllowUDPPorts)

	allow := ""
	if tcp != "" {
		allow += fmt.Sprintf("    ip saddr %s tcp dport %s accept\n", guestIP, tcp)
	}
	if udp != "" {
		allow += fmt.Sprintf("    ip saddr %s udp dport %s accept\n", guestIP, udp)
	}

	// Traffic the guest addresses to the gateway itself lands in input, not
	// forward, so a resolver on the gateway needs its own rule. It is derived
	// from the same AllowUDPPorts the forward chain uses: a policy stated in
	// two places is enforced by whichever copy was not updated.
	input := ""
	for _, p := range n.AllowUDPPorts {
		input += fmt.Sprintf("    ip saddr %s udp dport %d accept\n", guestIP, p)
	}

	// Ordering is the whole rule set. The private-address drop sits ABOVE the
	// port allowances, so "allow 443" cannot be turned into a route to an
	// internal service that happens to speak TLS — which is most of them.
	//
	// 169.254.0.0/16 is listed for completeness only. Firecracker answers MMDS
	// inside the virtio-net device, so those packets are consumed before they
	// reach the tap and this rule never sees them; it is here to catch
	// anything else in link-local space.
	return fmt.Sprintf(`
table inet dmz {
  chain forward {
    type filter hook forward priority filter; policy drop;

    ct state established,related accept
    ct state invalid drop

    ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, 127.0.0.0/8, 100.64.0.0/10 } drop
    ip6 daddr { fc00::/7, fe80::/10, ::1/128 } drop

%s  }

  chain input {
    type filter hook input priority filter; policy drop;
    ct state established,related accept
    iifname "lo" accept
%s  }
}
`, allow, input)
}

func portSet(ports []int) string {
	if len(ports) == 0 {
		return ""
	}
	parts := make([]string, len(ports))
	for i, p := range ports {
		parts[i] = fmt.Sprint(p)
	}
	return "{ " + strings.Join(parts, ", ") + " }"
}

// Down removes the namespace and everything in it. Deleting the namespace
// destroys the tap and the inner veth with it; the outer veth goes when its
// peer does. Leaked taps accumulate silently and are a real operational
// failure, so this runs even on a partial Up.
func (n *Network) Down() error {
	_ = run("ip", "link", "del", fmt.Sprintf("vh%d", n.Index))
	_ = run("nft", "delete", "table", "inet", fmt.Sprintf("dmznat%d", n.Index))
	return run("ip", "netns", "del", n.NS)
}

// GuestKernelArgs returns the ip= argument that configures the guest's eth0 at
// boot, so the image needs no DHCP client and no per-VM configuration.
func (n *Network) GuestKernelArgs() string {
	guest, gw := n.guestNet()
	return fmt.Sprintf("ip=%s::%s:255.255.255.252::eth0:off", guest, gw)
}
