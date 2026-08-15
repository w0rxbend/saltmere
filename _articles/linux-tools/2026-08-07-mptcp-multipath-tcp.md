---
title: "Multipath TCP on Modern Linux: One Connection, Two Uplinks"
date: 2026-08-07
track: linux-tools
summary: "The in-kernel MPTCP stack carries a single TCP connection across WiFi and cellular at once: enabling the sysctl, opening IPPROTO_MPTCP sockets, wiring endpoints and limits with ip mptcp, and forcing MPTCP onto unmodified binaries with mptcpize."
reading_time: 6
tags: [linux, mptcp, networking, tcp, resilience]
sources:
  - title: "Multipath TCP (MPTCP) — The Linux Kernel documentation"
    url: "https://docs.kernel.org/networking/mptcp.html"
  - title: "MPTCP — Multipath TCP for Linux (mptcp.dev)"
    url: "https://www.mptcp.dev/"
  - title: "Path Manager — Multipath TCP for Linux"
    url: "https://www.mptcp.dev/pm.html"
  - title: "ip-mptcp(8) — Linux manual page"
    url: "https://man7.org/linux/man-pages/man8/ip-mptcp.8.html"
  - title: "Chapter 38. Getting started with Multipath TCP — Red Hat Enterprise Linux 9"
    url: "https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/configuring_and_managing_networking/getting-started-with-multipath-tcp_configuring-and-managing-networking"
---

**Gist.** A host with two uplinks — a cellular modem on `usb0`, Ethernet or WiFi on `eth0` — can reach the internet two ways, but an ordinary Transmission Control Protocol (TCP) connection is bound to the single address pair fixed at handshake time, so losing that path resets the session. Multipath TCP (MPTCP), specified in RFC 8684, carries one logical connection over several TCP flows called **subflows**, striping data across them and reassembling it in order, so a path failure costs retransmissions rather than a reset. The cost is configuration and visibility: MPTCP is opt-in per socket, requires explicit address and limit state in the kernel, and depends on middleboxes preserving unknown TCP options.

An upstream MPTCP v1 implementation has been in the mainline Linux kernel since **5.6** (March 2020); the **userspace path manager** landed in **5.19** (July 2022). The stack interoperates with MPTCP implementations on other operating systems.

## Protocol mechanism

An MPTCP connection begins as an ordinary TCP handshake carrying an `MP_CAPABLE` option. If both ends acknowledge it, the resulting flow becomes the connection's first subflow, and either side may open **additional subflows** — separate TCP flows over different address pairs — that join the same logical connection. Because the mechanism is expressed entirely as TCP options, a middlebox that strips unknown options causes the connection to **fall back to plain TCP** rather than fail: the endpoints do not agree on `MP_CAPABLE`, and the application observes a normal single-path stream.

Two components govern behaviour. The **path manager** decides which subflows to create and which local addresses to announce to the peer. The **packet scheduler** decides which subflow each chunk of data is sent on. The path manager is configured by the administrator; the scheduler runs in the kernel and is not configured through `ip mptcp`.

## Enabling the stack

MPTCP is gated by a sysctl:

```
sudo sysctl -w net.mptcp.enabled=1
echo 'net.mptcp.enabled = 1' | sudo tee /etc/sysctl.d/90-mptcp.conf
```

```
sysctl net.mptcp.enabled
# net.mptcp.enabled = 1
```

With the sysctl off, MPTCP socket calls return **`ENOPROTOOPT`** (protocol not available); on kernels older than 5.6 they return **`EINVAL`**. The two errno values are the practical way to distinguish "disabled" from "unsupported" at runtime.

## Requesting MPTCP on a socket

MPTCP is not a transparent replacement for TCP: the application must request it explicitly by passing **`IPPROTO_MPTCP`, defined as `262`**, in place of `IPPROTO_TCP`:

```c
/* Ordinary TCP:  socket(AF_INET, SOCK_STREAM, IPPROTO_TCP)  */
int sd = socket(AF_INET, SOCK_STREAM, IPPROTO_MPTCP);
if (sd < 0)
        perror("socket");   /* ENOPROTOOPT if disabled/unsupported */
```

Every subsequent call — `connect()`, `bind()`, `listen()`, `read()`, `write()` — behaves as on a stream socket. The application still sees one connection and one ordered byte stream; the additional paths are not exposed through the socket API.

Where the binary cannot be recompiled, `mptcpize` (shipped with `mptcpd`) supplies the change externally. It uses `LD_PRELOAD` to interpose a shim that rewrites `IPPROTO_TCP` socket creation into `IPPROTO_MPTCP`, so an unmodified program speaks MPTCP:

```
mptcpize run curl -s https://example.com/big.iso -o big.iso
```

For a systemd service, `mptcpize enable <unit>` installs the same shim through the unit's environment.

## Path configuration with `ip mptcp`

Enabling the sysctl and opening an `IPPROTO_MPTCP` socket yields an MPTCP connection over a single path. Using the second uplink requires telling the in-kernel path manager about the available addresses, via the `ip mptcp` subcommand of iproute2.

Two kinds of state exist: **endpoints**, which declare local addresses and how they may participate, and **limits**, which cap how many additional subflows and peer-announced addresses are permitted. Endpoint flags:

- **`subflow`** — the kernel initiates an additional subflow *from* this address once the connection is established.
- **`signal`** — the address is announced to the peer in an `ADD_ADDR` option, inviting the peer to open a subflow toward it; the usual server-side choice.
- **`backup`** — the subflow is used when the primary path fails rather than for aggregation, which suits a metered cellular link.
- **`fullmesh`** — create subflows toward every address the peer announces rather than one.

A dual-uplink client with `eth0` primary and metered `usb0` as backup:

```
# Allow up to 2 extra subflows, accept up to 2 peer-announced addresses
sudo ip mptcp limits set subflows 2 add_addr_accepted 2

# eth0's address builds extra subflows for aggregation
sudo ip mptcp endpoint add 192.0.2.10 dev eth0 subflow

# usb0's address is failover only
sudo ip mptcp endpoint add 100.64.1.20 dev usb0 subflow backup
```

Limits and endpoints must agree. **The default `subflows` limit is 0**, and with it the kernel creates no additional paths regardless of how many `subflow` endpoints are declared — a silent misconfiguration whose only symptom is a connection that never gains a second subflow.

```
ip mptcp limits show
ip mptcp endpoint show
# 192.0.2.10 id 1 subflow dev eth0
# 100.64.1.20 id 2 subflow backup dev usb0
```

## Observing subflows

`ss` lists MPTCP sockets and their subflows under `-M`:

```
ss -Mti
```

The kernel exports MPTCP counters, which give direct evidence that a second subflow formed and carried data:

```
nstat -z | grep -i mptcp
# MPTcpExtMPCapableSYNTX      12
# MPTcpExtMPJoinSynRx          8   <- extra subflows joined
# MPTcpExtMPJoinAckRx          8
```

Increments on the `MPJoin*` counters indicate additional subflows being established, as distinct from `MPCapable*`, which only records that connections attempted the initial negotiation. Path events — subflow creation, address announcements, path closure — can be watched live with `ip mptcp monitor` while the link is exercised.

## In-kernel versus userspace path manager

The configuration above uses the **in-kernel** path manager, which applies one uniform policy to every MPTCP connection on the host. Per-connection policy — different subflow rules for different applications — requires the **userspace** path manager, driven by a daemon such as `mptcpd`:

```
sudo sysctl -w net.mptcp.path_manager=userspace
# older kernels: net.mptcp.pm_type=1
```

In userspace mode the `ip mptcp endpoint` rules are **ignored** and the daemon decides. A host switched to userspace mode without a running daemon therefore establishes no additional subflows at all.

## Gateway deployments

The configuration distinction is small and the resulting behaviour is not. With one uplink flagged `backup`, a long-lived connection — a VPN tunnel, an MQTT session, a large upload — uses the primary uplink and shifts to the backup when the primary fails, without a reconnect at the application layer, and without application changes when fronted by `mptcpize`. With both endpoints flagged plain `subflow`, the same two links are used concurrently and their throughput aggregates for a single transfer.

A minimal verification: on a host with two interfaces, set `net.mptcp.enabled=1`, add a `subflow` endpoint on each interface, set `ip mptcp limits set subflows 2`, then run `mptcpize run curl` against a large file while watching the `MPTcp*` counters in `nstat` and the event stream from `ip mptcp monitor`; removing one interface mid-transfer shows whether the transfer continues.

## Pitfalls

- **No second subflow ever appears, and no error is reported.** The `subflows` limit defaults to 0, so declared `subflow` endpoints are never acted on until `ip mptcp limits set subflows N` raises it.
- **`socket()` fails with `ENOPROTOOPT`.** `net.mptcp.enabled` is 0; `EINVAL` instead means the kernel predates 5.6 and has no MPTCP support to enable.
- **Endpoint rules have no effect after switching path managers.** In userspace mode the kernel ignores `ip mptcp endpoint` entries entirely and defers to the daemon, so a missing or stopped `mptcpd` leaves the connection single-path.
- **The connection behaves exactly like plain TCP on some networks.** A middlebox stripping unknown TCP options prevents `MP_CAPABLE` agreement, and MPTCP falls back silently rather than reporting a failure.
- **A metered cellular link is billed for bulk traffic.** An endpoint declared `subflow` without `backup` participates in aggregation, so the scheduler places data on it during normal operation rather than only at failover.
- **Recompilation is assumed where none happened.** Applications using `IPPROTO_TCP` remain single-path regardless of endpoint and limit configuration unless they are rebuilt against `IPPROTO_MPTCP` or launched under `mptcpize`.
