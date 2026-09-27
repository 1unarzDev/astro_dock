# Host time synchronization

Containers inherit their host clock. Fix the host, not each container. Bad dates
break certificate validation/package downloads and stale/future-stamped ROS TF
and sensor data. The host setup now configures time **before** package installs:

```sh
./setup.linux.sh --components clock
# On an isolated boat LAN with a reachable time server:
ASTRO_NTP_SERVER=192.168.0.185 ./setup.linux.sh --components clock
```

The reusable `.devcontainer/setup-time-sync.sh` configures the existing chrony
client on Fedora/CoreOS or systemd-timesyncd on Ubuntu/Arch. It preserves other
chrony sources, enables the service across boots, and waits up to 30 seconds
for synchronization. If no source is reachable it fails instead of proceeding
to package installation with an unverified clock. Rerun after fixing networking.

## Current boat setup

The internet-connected `innovation` workstation has a small `astro_clock`
container, restart policy `unless-stopped`, serving NTP on its boat-LAN address
`192.168.0.185`. It runs chrony with `-x`, **without control of the host clock**.
Only `192.168.0.0/24` clients are allowed. The workstation's existing native
time-sync service is unchanged. ODROID and Squirtle use this server and retain
Internet NTP fallback sources. No Internet route or forwarding is supplied by
the clock container.

To reproduce the relay on the internet-connected workstation:

```sh
docker build -f .devcontainer/Dockerfile.time-server -t astro:clock-local .devcontainer
docker run -d --name astro_clock --restart unless-stopped --network host astro:clock-local
docker exec astro_clock chronyc tracking
```

Reserve that workstation's LAN address in DHCP, or rerun the client setup with
its new address if it changes. The relay must be running and have a valid NTP
source; it deliberately does not advertise an unsynchronized local clock as
authoritative. On another boat subnet, edit the `allow` CIDR in
`time-server.conf`. Do not expose UDP 123 indiscriminately to the Internet.

If the workstation is absent, clients use Internet NTP when available. Fully
offline cold starts on machines without an RTC cannot recover true UTC from
nothing: provide an always-present GPS/NTP server or a battery-backed RTC.
An offline timestamp seed is not a substitute for an authoritative clock.

Before navigation or updates:

```sh
timedatectl status
# Fedora/CoreOS:
chronyc tracking
chronyc sources -v
# Ubuntu/Arch:
timedatectl timesync-status
```

Look for synchronization, a selected source, and a small offset. Clock steps
must be performed with navigation/propulsion stopped. Accurate time alone does
not repair missing DNS, routing, Internet access, or end-of-life repositories.
