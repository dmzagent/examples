#!/bin/sh
# PID 1 inside the microVM.
#
# The root filesystem is a read-only block device shared with every other
# sandbox on the host. Everything this script mounts over it is tmpfs, which
# means it is RAM, which means the host reclaims it when the VM is killed and
# there is nothing left to inspect, recover, or re-enter.
set -eu

mount -t proc     proc     /proc
mount -t sysfs    sysfs    /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /dev/pts && mount -t devpts devpts /dev/pts

# The writable surface, in full. Nothing outside these paths can be modified,
# so an agent cannot leave anything behind for the next agent to find.
mount -t tmpfs -o size=64m,mode=1777 tmpfs /tmp
mount -t tmpfs -o size=16m,mode=0755 tmpfs /run
mount -t tmpfs -o size=16m,mode=0755 tmpfs /var/log
mount -t tmpfs -o size=128m,mode=0755,uid=1000,gid=1000 tmpfs /home/agent

# If the agent needs to write anywhere in the tree rather than in a known set
# of directories, swap the four mounts above for an overlay whose upper layer
# is tmpfs:
#
#   mount -t tmpfs -o size=256m tmpfs /mnt/rw
#   mkdir -p /mnt/rw/upper /mnt/rw/work /mnt/new
#   mount -t overlay overlay \
#         -o lowerdir=/,upperdir=/mnt/rw/upper,workdir=/mnt/rw/work /mnt/new
#   exec switch_root /mnt/new /sbin/init
#
# Same guarantee, wider surface: the lower layer is still the read-only image.

ip link set lo up
# eth0 is configured by the kernel from the ip= argument the host passed on the
# command line, so there is no DHCP client in this image and nothing on the
# network can answer for one.

exec setpriv --reuid=1000 --regid=1000 --clear-groups /usr/bin/guest-agent
