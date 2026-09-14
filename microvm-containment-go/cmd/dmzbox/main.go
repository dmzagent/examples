// Command dmzbox boots one microVM, hands it a task over vsock, prints what
// came back, and destroys it.
//
//	sudo dmzbox -kernel vmlinux -rootfs rootfs.ext4 -- /bin/sh -c 'echo hi'
//
// It must run as root: building a network namespace and a tap device needs
// CAP_NET_ADMIN, and opening /dev/kvm needs access to it. The VMM itself runs
// as neither — the jailer drops to the unprivileged uid before exec.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	microvm "github.com/dmzagent/examples/microvm-containment-go"
)

func main() {
	var (
		kernel   = flag.String("kernel", "vmlinux", "uncompressed guest kernel (host path)")
		rootfs   = flag.String("rootfs", "rootfs.ext4", "ext4 root filesystem, attached read-only")
		fcBin    = flag.String("firecracker", "/usr/bin/firecracker", "firecracker binary")
		jailer   = flag.String("jailer", "/usr/bin/jailer", "jailer binary")
		chroot   = flag.String("chroot", "/srv/jail", "chroot base directory")
		uplink   = flag.String("uplink", "", "host interface to masquerade egress out of; empty for no egress")
		uid      = flag.Int("uid", 30000, "unprivileged uid the VMM runs as")
		gid      = flag.Int("gid", 30000, "unprivileged gid the VMM runs as")
		index    = flag.Int("index", 0, "sandbox index; picks the namespace and address block")
		budget   = flag.Duration("budget", 60*time.Second, "hard wall-clock cap")
		egressBW = flag.Int64("egress-bps", 1<<20, "egress bytes/sec, 0 for unlimited")
		egressOp = flag.Int64("egress-pps", 1000, "egress packets/sec, 0 for unlimited")
	)
	flag.Parse()

	argv := flag.Args()
	if len(argv) == 0 {
		argv = []string{"/bin/sh", "-c", "id && uname -a && cat /proc/cmdline"}
	}

	if err := preflight(*fcBin, *jailer, *kernel, *rootfs); err != nil {
		fmt.Fprintln(os.Stderr, "preflight:", err)
		os.Exit(1)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// 1. The world the guest can reach, built before the guest exists.
	net := microvm.DefaultNetwork(*index, *uplink)
	if err := net.Up(); err != nil {
		fmt.Fprintln(os.Stderr, "network:", err)
		os.Exit(1)
	}
	defer func() {
		if err := net.Down(); err != nil {
			fmt.Fprintln(os.Stderr, "warning: network teardown:", err)
		}
	}()

	// 2. The sandbox.
	sb, err := microvm.New(microvm.Config{
		ID:             fmt.Sprintf("dmz-%d-%d", *index, time.Now().UnixNano()),
		FirecrackerBin: *fcBin,
		JailerBin:      *jailer,
		ChrootBase:     *chroot,
		UID:            *uid,
		GID:            *gid,

		KernelImage: *kernel,
		RootFS:      *rootfs,
		KernelArgs:  microvm.DefaultKernelArgs + " " + net.GuestKernelArgs(),

		VCPUs:  1,
		MemMiB: 256,
		Net:    net,

		EgressBytesPerSec:  *egressBW,
		EgressOpsPerSec:    *egressOp,
		IngressBytesPerSec: 4 << 20,

		Budget: *budget,

		Metadata: map[string]any{
			"latest": map[string]any{
				"meta-data": map[string]any{"sandbox-index": fmt.Sprint(*index)},
			},
		},

		// Telemetry arrives here, on the host, already stamped. This is the
		// record: nothing inside the guest is trusted to keep one.
		OnRecord: func(r microvm.Record) {
			line, _ := json.Marshal(r)
			fmt.Fprintln(os.Stderr, string(line))
		},
		OnPurge: func(reason string) {
			fmt.Fprintln(os.Stderr, "purged:", reason)
		},
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, "config:", err)
		os.Exit(1)
	}

	// 3. Boot.
	start := time.Now()
	if err := sb.Launch(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "launch:", err)
		os.Exit(1)
	}
	fmt.Fprintf(os.Stderr, "booted in %s; jail at %s\n", time.Since(start).Round(time.Millisecond), sb.JailRoot())

	// 4. Work. Purge whatever happens, including a panic in this function.
	defer sb.Purge("driver exited")

	taskCtx, cancel := context.WithTimeout(ctx, *budget)
	defer cancel()

	var result struct {
		ExitCode int    `json:"exit_code"`
		Stdout   string `json:"stdout"`
		Stderr   string `json:"stderr"`
		Error    string `json:"error,omitempty"`
	}
	task := map[string]any{"argv": argv, "timeout": budget.String()}

	// The guest needs a moment to boot and bind its control port. A real
	// orchestrator retries rather than sleeping, but the retry belongs to the
	// caller's policy, not to the sandbox.
	var taskErr error
	for attempt := 0; attempt < 50; attempt++ {
		if taskErr = sb.SendTask(taskCtx, task, &result); taskErr == nil {
			break
		}
		select {
		case <-time.After(100 * time.Millisecond):
		case <-sb.Done():
			fmt.Fprintln(os.Stderr, "sandbox died before accepting work:", sb.Reason())
			os.Exit(1)
		case <-taskCtx.Done():
			attempt = 50
		}
	}
	if taskErr != nil {
		fmt.Fprintln(os.Stderr, "task:", taskErr)
		sb.Purge("task could not be delivered")
		os.Exit(1)
	}

	// 5. Purge, then confirm the jail is gone. An orchestrator that reports a
	// teardown it did not perform is worse than one that never claimed to.
	sb.Purge("task complete")
	<-sb.Done()
	if _, err := os.Stat(sb.JailRoot()); err == nil {
		fmt.Fprintln(os.Stderr, "warning: jail survived the purge:", sb.JailRoot())
	}

	fmt.Print(result.Stdout)
	fmt.Fprint(os.Stderr, result.Stderr)
	if result.Error != "" {
		fmt.Fprintln(os.Stderr, "guest:", result.Error)
	}
	os.Exit(result.ExitCode)
}

func preflight(paths ...string) error {
	if os.Geteuid() != 0 {
		return fmt.Errorf("must run as root (needs CAP_NET_ADMIN to build the namespace)")
	}
	if err := syscall.Access("/dev/kvm", 2 /* W_OK */); err != nil {
		return fmt.Errorf("/dev/kvm is not writable: %w (no KVM means no microVM; "+
			"nested virtualisation must be enabled if this is itself a VM)", err)
	}
	for _, p := range paths {
		if _, err := os.Stat(p); err != nil {
			return err
		}
	}
	return nil
}
