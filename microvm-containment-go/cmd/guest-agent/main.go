// Command guest-agent runs inside the microVM. It is the only process in the
// sandbox that speaks to the host, and it speaks over vsock — never over the
// network interface, which is filtered, and never through the root filesystem,
// which is read-only.
//
// It does two things: it streams every line it produces to the host's
// telemetry port, and it serves one task at a time on the control port.
package main

import (
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const (
	// AF_VSOCK. The syscall package defines this per-GOARCH but not on every
	// platform Go builds for, so it is named here rather than imported.
	afVsock = 40

	// Reserved context IDs. The guest reaches the host at 2; a guest's own
	// CID is whatever Firecracker was configured with, and it never needs to
	// know it.
	cidHost = 2
	cidAny  = 0xFFFFFFFF
)

// sockaddrVM builds a struct sockaddr_vm:
//
//	u16 svm_family; u16 svm_reserved1; u32 svm_port; u32 svm_cid; u8 svm_zero[4]
//
// The family field is native byte order, not network byte order — it is a
// plain C short, not a wire field.
func sockaddrVM(cid, port uint32) [16]byte {
	var sa [16]byte
	binary.NativeEndian.PutUint16(sa[0:2], afVsock)
	binary.NativeEndian.PutUint32(sa[4:8], port)
	binary.NativeEndian.PutUint32(sa[8:12], cid)
	return sa
}

// wrap turns a raw socket fd into an *os.File.
//
// It is deliberately not a net.Conn. net.FileConn cannot wrap an AF_VSOCK
// socket: the stdlib's anyToSockaddr has no case for the family, so
// Getsockname yields nothing, and newFileFD's type switch falls through to
// default and returns EPROTONOSUPPORT. An *os.File is a complete
// io.ReadWriteCloser over the same fd and is all a stream needs.
func wrap(fd int, name string) *os.File {
	return os.NewFile(uintptr(fd), name)
}

func dialVsock(cid, port uint32) (*os.File, error) {
	fd, err := syscall.Socket(afVsock, syscall.SOCK_STREAM, 0)
	if err != nil {
		return nil, fmt.Errorf("vsock socket: %w", err)
	}
	sa := sockaddrVM(cid, port)
	if _, _, errno := syscall.Syscall(
		syscall.SYS_CONNECT, uintptr(fd),
		uintptr(unsafe.Pointer(&sa[0])), uintptr(len(sa)),
	); errno != 0 {
		syscall.Close(fd)
		return nil, fmt.Errorf("vsock connect cid=%d port=%d: %w", cid, port, errno)
	}
	return wrap(fd, "vsock"), nil
}

type vsockListener struct{ fd int }

func listenVsock(port uint32) (*vsockListener, error) {
	fd, err := syscall.Socket(afVsock, syscall.SOCK_STREAM, 0)
	if err != nil {
		return nil, fmt.Errorf("vsock socket: %w", err)
	}
	sa := sockaddrVM(cidAny, port)
	if _, _, errno := syscall.Syscall(
		syscall.SYS_BIND, uintptr(fd),
		uintptr(unsafe.Pointer(&sa[0])), uintptr(len(sa)),
	); errno != 0 {
		syscall.Close(fd)
		return nil, fmt.Errorf("vsock bind port=%d: %w", port, errno)
	}
	if err := syscall.Listen(fd, 4); err != nil {
		syscall.Close(fd)
		return nil, fmt.Errorf("vsock listen: %w", err)
	}
	return &vsockListener{fd: fd}, nil
}

func (l *vsockListener) Accept() (*os.File, error) {
	nfd, _, err := syscall.Accept(l.fd)
	if err != nil {
		return nil, err
	}
	return wrap(nfd, "vsock-conn"), nil
}

func (l *vsockListener) Close() error { return syscall.Close(l.fd) }

// ---------------------------------------------------------------------------

// Task is what the host sends. Result is what comes back.
type Task struct {
	Argv    []string `json:"argv"`
	Stdin   string   `json:"stdin,omitempty"`
	Timeout string   `json:"timeout,omitempty"`
}

type Result struct {
	ExitCode int    `json:"exit_code"`
	Stdout   string `json:"stdout"`
	Stderr   string `json:"stderr"`
	Error    string `json:"error,omitempty"`
}

func main() {
	telemetryPort := envPort("DMZ_TELEMETRY_PORT", 5000)
	controlPort := envPort("DMZ_CONTROL_PORT", 5001)

	// Telemetry first. If the host is not listening we have no way to report
	// anything, and a sandbox that cannot be observed should not run work.
	tel, err := dialVsock(cidHost, telemetryPort)
	if err != nil {
		fmt.Fprintf(os.Stderr, "guest-agent: telemetry unavailable: %v\n", err)
		os.Exit(1)
	}
	defer tel.Close()

	log := func(format string, a ...any) {
		// The host stamps sequence and time. Anything this line says about
		// either would be the guest's claim, so it says neither.
		fmt.Fprintf(tel, format+"\n", a...)
	}
	log("guest-agent up, control port %d", controlPort)

	ln, err := listenVsock(controlPort)
	if err != nil {
		log("fatal: %v", err)
		os.Exit(1)
	}
	defer ln.Close()

	for {
		conn, err := ln.Accept()
		if err != nil {
			log("accept: %v", err)
			return
		}
		serve(conn, log)
		conn.Close()
	}
}

func serve(conn *os.File, log func(string, ...any)) {
	var t Task
	// Bound the request: the host is trusted here, but a bounded decoder
	// costs nothing and the habit is worth more than the bytes.
	if err := json.NewDecoder(io.LimitReader(conn, 1<<20)).Decode(&t); err != nil {
		log("malformed task: %v", err)
		return
	}
	if len(t.Argv) == 0 {
		writeResult(conn, Result{ExitCode: -1, Error: "empty argv"})
		return
	}
	log("task: %v", t.Argv)

	timeout := 60 * time.Second
	if t.Timeout != "" {
		if d, err := time.ParseDuration(t.Timeout); err == nil {
			timeout = d
		}
	}

	cmd := exec.Command(t.Argv[0], t.Argv[1:]...)
	if t.Stdin != "" {
		cmd.Stdin = strings.NewReader(t.Stdin)
	}
	var stdout, stderr capped
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	res := Result{}
	done := make(chan error, 1)
	if err := cmd.Start(); err != nil {
		writeResult(conn, Result{ExitCode: -1, Error: err.Error()})
		return
	}
	go func() { done <- cmd.Wait() }()

	select {
	case err := <-done:
		if ee, ok := err.(*exec.ExitError); ok {
			res.ExitCode = ee.ExitCode()
		} else if err != nil {
			res.ExitCode, res.Error = -1, err.Error()
		}
	case <-time.After(timeout):
		_ = cmd.Process.Kill()
		<-done
		res.ExitCode, res.Error = -1, "timed out in guest"
	}

	res.Stdout, res.Stderr = stdout.String(), stderr.String()
	log("task exit %d (%d bytes out, %d err)", res.ExitCode, len(res.Stdout), len(res.Stderr))
	writeResult(conn, res)
}

func writeResult(w io.Writer, r Result) { _ = json.NewEncoder(w).Encode(r) }

// capped is a bounded buffer. A task that prints without end must not take the
// agent's memory with it — the VM has 256MB and the OOM killer would take the
// agent before the task.
type capped struct {
	buf []byte
	cut bool
}

const cap_ = 1 << 20

func (c *capped) Write(p []byte) (int, error) {
	if room := cap_ - len(c.buf); room > 0 {
		if len(p) > room {
			p, c.cut = p[:room], true
		}
		c.buf = append(c.buf, p...)
	} else {
		c.cut = true
	}
	return len(p), nil // report success: truncation is not the task's failure
}

func (c *capped) String() string {
	if c.cut {
		return string(c.buf) + "\n[truncated]"
	}
	return string(c.buf)
}

func envPort(key string, def uint32) uint32 {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.ParseUint(v, 10, 32); err == nil {
			return uint32(n)
		}
	}
	return def
}
