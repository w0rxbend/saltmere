---
title: "Multipath TCP on Modern Linux: One Connection, Two Uplinks"
date: 2026-08-07
track: linux-tools
summary: "How the in-kernel MPTCP stack lets a single TCP connection ride WiFi and cellular at once — enabling the sysctl, opening IPPROTO_MPTCP sockets, wiring endpoints and limits with ip mptcp, and forcing MPTCP onto unmodified binaries with mptcpize."
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

A dual-uplink gateway — a cell modem on `usb0`, WiFi or Ethernet on `eth0` — has two ways to reach the internet, but a normal TCP connection can only use one at a time. Pull the primary and the connection dies; every session resets. Multipath TCP fixes exactly that: it lets a single TCP connection spread its bytes across several network paths (**subflows**) at once, moving traffic between them without the application ever noticing, and surviving the loss of any one path.

The good news for Linux is that none of this needs an out-of-tree patch anymore. A clean, upstream MPTCP v1 implementation has been in the mainline kernel since **5.6** (March 2020) and has matured steadily since — most notably the **userspace path manager**, which landed in **5.19** (July 2022). This is a spec, RFC 8684, not a research fork, and it interoperates with the MPTCP stacks on other OSes.

## How MPTCP actually works

An MPTCP connection starts as an ordinary TCP handshake with an extra `MP_CAPABLE` option. If both ends agree, the first subflow becomes the "primary" path, and either side can then open **additional subflows** — separate TCP flows over different addresses — that join the same logical connection. Data is striped across subflows and reassembled in order at the far end. Because it's just TCP options on the wire, MPTCP falls back gracefully to plain TCP if a middlebox strips the options.

Two knobs control this behaviour: the **path manager**, which decides *which* subflows to create and *which* addresses to announce, and the **packet scheduler**, which decides *which* subflow each chunk of data goes on. You configure the path manager; the scheduler is the kernel's job.

## Enabling MPTCP

MPTCP is opt-in and can be gated by a sysctl. Turn it on and make it persistent:

```
sudo sysctl -w net.mptcp.enabled=1
echo 'net.mptcp.enabled = 1' | sudo tee /etc/sysctl.d/90-mptcp.conf
```

Check it's live:

```
sysctl net.mptcp.enabled
# net.mptcp.enabled = 1
```

With the sysctl off, MPTCP socket calls return `ENOPROTOOPT` (protocol not available); on kernels older than 5.6 they return `EINVAL`.

## Making sockets MPTCP-aware

MPTCP is not a transparent replacement for TCP — an application has to ask for it. Instead of `IPPROTO_TCP`, you pass `IPPROTO_MPTCP` (defined as `262`) to `socket()`:

```c
/* Ordinary TCP:  socket(AF_INET, SOCK_STREAM, IPPROTO_TCP)  */
int sd = socket(AF_INET, SOCK_STREAM, IPPROTO_MPTCP);
if (sd < 0)
        perror("socket");   /* ENOPROTOOPT if disabled/unsupported */
```

Everything after that — `connect()`, `bind()`, `listen()`, `read()`, `write()` — is the same as a normal stream socket. From the application's view it's still one connection with one byte stream; the extra paths are invisible.

If you can't recompile the binary, use `mptcpize` (shipped with `mptcpd`). It `LD_PRELOAD`s a shim that rewrites `IPPROTO_TCP` socket creation into `IPPROTO_MPTCP`, so an unmodified program speaks MPTCP:

```
mptcpize run curl -s https://example.com/big.iso -o big.iso
```

For a systemd service, `mptcpize enable <unit>` drops in the same shim via the unit's environment.

## Wiring up paths with `ip mptcp`

Enabling the sysctl and opening the right socket gets you an MPTCP connection over one path. To actually *use* the second uplink you have to tell the in-kernel path manager about your addresses, using the `ip mptcp` subcommand from iproute2.

There are two moving parts: **endpoints** (which local addresses may join a connection, and how) and **limits** (how many extra subflows are allowed). An endpoint carries a flag:

- **`subflow`** — the kernel will initiate an additional subflow *from* this address after the connection is up (the client side of building extra paths).
- **`signal`** — the address is announced to the peer via an `ADD_ADDR` option, inviting it to create a subflow toward this address (typically the server side).
- **`backup`** — the subflow is used only when the primary path fails, rather than for aggregation. Ideal for a metered cellular link you want as failover, not for bulk throughput.
- **`fullmesh`** — create subflows to every address the peer announces, not just one.

A concrete dual-uplink client. `eth0` is the primary; `usb0` (cellular) is a metered backup:

```
# Allow up to 2 extra subflows, accept up to 2 peer-announced addresses
sudo ip mptcp limits set subflows 2 add_addr_accepted 2

# eth0's address builds extra subflows for aggregation
sudo ip mptcp endpoint add 192.0.2.10 dev eth0 subflow

# usb0's address is failover only
sudo ip mptcp endpoint add 100.64.1.20 dev usb0 subflow backup
```

Note the `limits` and the number of `subflow` endpoints have to agree: if `subflows` is 0 (the default limit) the kernel creates no extra paths no matter how many endpoints you declare.

Verify the configuration and the runtime state:

```
ip mptcp limits show
ip mptcp endpoint show
# 192.0.2.10 id 1 subflow dev eth0
# 100.64.1.20 id 2 subflow backup dev usb0
```

## Confirming it works

Once traffic is flowing, `ss` shows MPTCP sockets and their subflows with `-M`:

```
ss -Mti
```

The kernel also exports MPTCP counters, which are the fastest way to prove a second subflow really formed and carried data:

```
nstat -z | grep -i mptcp
# MPTcpExtMPCapableSYNTX      12
# MPTcpExtMPJoinSynRx          8   <- extra subflows joined
# MPTcpExtMPJoinAckRx          8
```

`MPJoin*` counters incrementing means additional subflows are actually being established. To watch path events live — subflows being created, addresses announced, paths closing — run `ip mptcp monitor` in another terminal while you exercise the link (and pull a cable to see failover).

## In-kernel vs userspace path manager

Everything above uses the **in-kernel** path manager, which applies one uniform policy to every MPTCP connection on the box. That's exactly right for a gateway with a fixed set of uplinks. When you need per-connection policy — different subflow rules for different apps — switch to the **userspace** path manager and let a daemon like `mptcpd` drive it:

```
sudo sysctl -w net.mptcp.path_manager=userspace
# older kernels: net.mptcp.pm_type=1
```

In userspace mode the `ip mptcp endpoint` rules are ignored; the daemon decides. For most IoT and mobile-gateway deployments the in-kernel manager plus a couple of static endpoints is all you need.

## Why this matters for gateways

The killer use case is a small router or IoT gateway with two uplinks — LTE/5G plus fixed line, or dual cellular modems. With MPTCP and a `backup` endpoint, a long-lived connection (a VPN tunnel, an MQTT session, a video upload) rides the primary uplink at full speed and shifts to the backup within a couple of round-trips when the primary drops — no reconnect, no reset, no application changes if you front it with `mptcpize`. Set both endpoints to plain `subflow` instead and you *aggregate* the two links, summing their throughput for a single transfer. One kernel feature, two very different resilience stories, both configured with three commands.

**Try next:** On a machine with two interfaces, `sysctl -w net.mptcp.enabled=1`, add a `subflow` endpoint on each with `ip mptcp endpoint add`, set `ip mptcp limits set subflows 2`, then run `mptcpize run curl` against a large file while watching `nstat MPTcp*` and `ip mptcp monitor` — pull one interface mid-transfer and confirm the download keeps going.
