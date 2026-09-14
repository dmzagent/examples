# microvm-containment-go

Boots one Firecracker microVM per task, hands it work over vsock, collects its
telemetry on the host, and destroys it. Go, standard library only.

```
sudo dmzbox -kernel vmlinux -rootfs rootfs.ext4 -uplink eth0 -- /bin/sh -c 'id; uname -a'
```

## What it shows

The lifecycle an orchestrator actually performs, in order:

1. **A world before a guest.** `Network.Up` builds a network namespace holding
   one tap device and the nftables policy that decides where the guest may go.
2. **A jail before a VMM.** The jailer chroots Firecracker, drops it to an
   unprivileged uid/gid, puts it in fresh PID/net/mount namespaces and a
   cgroup, and installs a seccomp filter. A guest that escapes the guest kernel
   lands in an empty directory owned by nobody.
3. **Configuration over a Unix socket.** Firecracker serves plain HTTP/1.1 on
   `/run/firecracker.socket` inside the chroot. `api.go` is the whole client.
4. **A telemetry listener before a guest exists.** Bound before
   `InstanceStart`, because a guest that dials a port with no host listener
   gets a reset and the records are simply gone.
5. **`InstanceStart`.** Everything above must precede it; Firecracker rejects
   configuration afterwards.
6. **Purge.** `SIGKILL` to the process group, `cgroup.kill` behind it, then the
   jail directory and the namespace.

## What it contains, and by which mechanism

| Property | Mechanism | Asserted by |
| --- | --- | --- |
| The guest cannot modify the image it booted from | `is_read_only: true` on the root drive | `root drive is read only` |
| The guest cannot persist anything at all | every writable path is tmpfs (`guest/init.sh`) | — (guest-side) |
| The guest cannot reach internal networks | nftables `policy drop` + private-address drop **above** the port allowances | `TestFilterDropsInternalBeforeAllowingPorts` |
| The guest cannot scan at machine speed | `rx_rate_limiter` / `tx_rate_limiter` token buckets | `network interface throttles both directions` |
| The guest cannot forge, backdate or renumber its own logs | host assigns `Seq` and `HostTime`; the guest supplies only text | `TestTelemetryIsStampedByTheHost` |
| The guest cannot blind the host by flooding | telemetry byte budget, exceeding it purges | `TestTelemetryBudgetPurges` |
| The guest cannot outlive its budget | `time.AfterFunc` → `Purge` | — |
| The guest cannot outlive the orchestrator | `Pdeathsig: SIGKILL` on the jailer | — |
| Two guests cannot share a physical core's caches | `smt: false` | `machine config uses smt, not ht_enabled` |

## Three things the common write-up gets wrong

**There is no second serial port.** Firecracker has exactly one serial device
and no virtio-serial, so `/dev/ttyS1` does not exist and cannot be a telemetry
channel. Worse, the serial rate limiter *drops* guest writes over the rate
rather than queueing them ("silently dropped and counted in the
`uart.rate_limiter_dropped_bytes` metric"), so the channel is lossy exactly
when a guest is misbehaving. Telemetry goes over **vsock**; the serial console
carries kernel messages and nothing else.

**A network interface has no `rate_limiter` field.** It has `rx_rate_limiter`
and `tx_rate_limiter`. The singular spelling is the *drive* field. A config
using it on an interface is rejected at `PUT` time.

**A token bucket has no `bucket_size` field.** The optional burst is
`one_time_burst`. And the pair is easy to misread by an order of magnitude:
`{size: 1048576, refill_time: 100}` is one mebibyte every 100 ms, which is
10 MB/s, not 1 MB/s. Build buckets with `Throttle(rate, window)` and assert
with `.Rate()`.

## The rate limiter is not an access control

A token bucket decides how *fast* the guest may reach the internal network.
`network.go` decides *whether* it may. Throttling a subnet scan to 1,000
packets per second still scans the subnet — it just finishes tomorrow, and an
agent is patient. The private-address drop sits above the port allowances on
purpose: "allow 443" placed first would turn the allowance into a route to
every internal service that speaks TLS, which is most of them.

## Boot time

Firecracker's specification commits to `<= 125 ms` from the `InstanceStart` API
call to the start of `/sbin/init`, with `<= 5 MiB` of VMM memory overhead. A
figure in the single milliseconds is either the VMM process starting (before
any guest exists) or a snapshot restore — not a cold boot. If a sandbox per
tool call needs to cost less than 125 ms, the answer is `PUT /snapshot/load`
against a pre-booted snapshot, not a faster cold boot.

## Layout

```
api.go               the Firecracker REST client and its wire types
sandbox.go           jail, launch, telemetry, purge
network.go           namespace, tap, and the filter policy
cmd/dmzbox/          the driver: boot, send one task, purge
cmd/guest-agent/     runs inside the VM; AF_VSOCK without a dependency
guest/init.sh        PID 1: tmpfs over a read-only root, then the agent
```

`cmd/guest-agent` talks to `AF_VSOCK` through raw syscalls rather than `net`.
This is not preference: `net.FileConn` cannot wrap a vsock socket, because the
standard library's `anyToSockaddr` has no case for the family, so
`Getsockname` yields nothing and `newFileFD` returns `EPROTONOSUPPORT`. An
`*os.File` over the same fd is a complete `io.ReadWriteCloser`.

## Running the tests

```
go test ./...
```

The suite is written to fail when the claims above stop holding, which is a
thing worth checking rather than assuming. Break one deliberately and watch:

| Break | Expected failure |
| --- | --- |
| `IsReadOnly: true` → `false` | `root drive is read only` |
| `tx_rate_limiter` → `rate_limiter` | `network interface throttles both directions` |
| `one_time_burst` → `bucket_size` | `TestTokenBucketFieldNames` |
| the ack read in `DialGuest` → `bufio.NewReader` | `TestDialGuestDoesNotSwallowThePayload` |
| move the port allowance above the address drop | `TestFilterDropsInternalBeforeAllowingPorts` |
| `Seq: s.seq.Add(1)` → `Seq: 1` | `TestTelemetryIsStampedByTheHost` |
| `SMT: false` → `true` | `machine config uses smt, not ht_enabled` |

An earlier revision of that table had one row that did not fail when broken:
the SMT assertion checked that the field was *present* and never what it was
set to.

## Requirements

- `/dev/kvm`, writable. Nested virtualisation if the host is itself a VM.
- `firecracker` and `jailer` binaries (v1.x).
- Root, for the namespace and the tap. The VMM itself runs as neither.
- An uncompressed kernel image (`vmlinux`, not `bzImage`) and an ext4 rootfs
  containing `guest/init.sh` as `/sbin/init` and `guest-agent` at
  `/usr/bin/guest-agent`.

## What this is not

Not a fleet manager. One sandbox, one task, no pooling, no snapshots, no
scheduling, no admission control. Those are the orchestrator's, and they are
the reason the lifecycle here is a library rather than a `main`.
