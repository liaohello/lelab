#!/usr/bin/env bash
# Second layer of the runtime, executed inside the bubblewrap sandbox whose
# root is the Humble rootfs. A nested unprivileged user namespace is created
# here so that apt/dpkg run as uid 0 while /dev keeps working.
#
#   LROS_INNER  command string executed by bash -c as root inside the runtime
set -euo pipefail

inner="${LROS_INNER:?}"
command -v unshare >/dev/null || { echo '[enter] unshare missing in the rootfs' >&2; exit 1; }

# ROS 2 and CycloneDDS expect a private /dev/shm; bwrap does not create one.
exec unshare --map-root-user --mount --fork /bin/bash -c '
  set -e
  mount -t tmpfs shm -o size=64m /dev/shm 2>/dev/null || { mkdir -p /dev/shm; mount -t tmpfs shm /dev/shm; }
  mkdir -p /out /repo /vendor

  # The sandbox shares the host network namespace, so the host resolver works.
  if ! grep -qs "^nameserver" /etc/resolv.conf; then
    printf "nameserver 127.0.0.53\n" > /etc/resolv.conf
  fi
  # Package postinst scripts must not try to start host-visible services.
  printf "#!/bin/sh\nexit 101\n" > /usr/sbin/policy-rc.d
  chmod 755 /usr/sbin/policy-rc.d

  mkdir -p /out/ws/src /out/ws/build /out/ws/install /out/ws/log \
           /out/run /out/logs /out/data /out/state /out/evidence /out/stage

  exec /bin/bash -c "$LROS_INNER"
'
