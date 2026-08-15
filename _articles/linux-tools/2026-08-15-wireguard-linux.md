---
title: "WireGuard on Linux: Cryptokey Routing in ~4k Lines"
date: 2026-08-15
track: linux-tools
summary: "WireGuard replaces the certificate machinery of OpenVPN and IPsec with a single association: a public key bound to a list of allowed IP ranges. This article covers the model, a two-peer setup with wg-quick, the dual role of AllowedIPs as both filter and routing input, and the systemd-networkd configuration that removes wg-quick entirely."
reading_time: 6
tags: [wireguard, vpn, networking, systemd-networkd, homelab, linux-tools]
sources:
  - title: "WireGuard — fast, modern, secure VPN tunnel (project site)"
    url: "https://www.wireguard.com/"
  - title: "Donenfeld, J. — WireGuard: Next Generation Kernel Network Tunnel (whitepaper)"
    url: "https://www.wireguard.com/papers/wireguard.pdf"
  - title: "WireGuard Quick Start"
    url: "https://www.wireguard.com/quickstart/"
  - title: "systemd.netdev(5) — [WireGuard] and [WireGuardPeer] sections"
    url: "https://man.archlinux.org/man/systemd.netdev.5.en"
  - title: "Linux 5.6 — WireGuard mainlined (KernelNewbies)"
    url: "https://kernelnewbies.org/Linux_5.6"
---

**Gist.** Reaching a private network — a home lab of sensor nodes, a dashboard, a network-attached storage (NAS) box — from an untrusted network requires an authenticated tunnel, and the conventional answers (OpenVPN, IPsec) carry a certificate hierarchy and a negotiation protocol with it. WireGuard removes both: a peer's identity is its 32-byte Curve25519 public key, and each key is bound statically to the set of IP ranges it may use inside the tunnel. The cost of that simplicity is that **nothing is negotiated and nothing is discovered** — key distribution, address allocation, routing and forwarding are all out-of-band operator responsibilities, and a mismatch in the key-to-range binding presents as silent packet loss rather than an error.

WireGuard has been in the mainline Linux kernel since **5.6** (2020), so on a current distribution the data plane is already present; the `wireguard-tools` package supplies only the `wg` and `wg-quick` userspace helpers.

## The model: fixed Noise handshake plus cryptokey routing

WireGuard's cryptography is fixed rather than negotiated. Every tunnel uses the Noise protocol framework's IK handshake; the whitepaper names the exact construction, **`Noise_IKpsk2_25519_ChaChaPoly_BLAKE2s`** — Curve25519 key agreement, ChaCha20-Poly1305 authenticated encryption, BLAKE2s hashing, **one round trip** to establish a session. There are no cipher suites, no certificates, no X.509, no Transport Layer Security (TLS). Because there is no suite to agree on, there is no downgrade step and no version handshake; the corresponding limitation is that **an implementation speaking a different construction cannot interoperate at all.**

The second idea is **cryptokey routing**: each interface holds a table mapping peer public keys to the IP ranges that peer may use. The table is consulted in both directions, and this is the invariant worth memorising:

- **Outbound**, the kernel looks up the packet's *destination* address in the table. The peer whose `AllowedIPs` contains the longest matching prefix is the peer the packet is encrypted for. **If no entry matches, the packet is dropped** — there is no default peer.
- **Inbound**, after a packet decrypts and authenticates under some peer's session key, its *source* address is checked against that same peer's ranges. A mismatch drops the packet. A peer therefore cannot source packets from an address outside its own configured ranges, whatever it puts in the header.

Because identity is the key and not the outer UDP address, the outer endpoint is treated as mutable state: **a peer's endpoint is re-pinned to the source address of the most recent correctly authenticated packet from it.** Roaming between Wi-Fi and a cellular link is a consequence of that rule, not a separate feature. It also means the direction of first contact matters — a peer whose endpoint has never been learned or configured cannot be sent to.

## Implementation size and its consequences

The whitepaper's headline claim is that WireGuard can be implemented for Linux in "less than 4,000 lines of code". The comparison points are structurally different: OpenVPN is a userspace daemon carrying a full TLS stack, while IPsec splits the work between the kernel XFRM framework and a separate Internet Key Exchange (IKE) daemon such as strongSwan. The whitepaper reports higher throughput and lower ping latency for WireGuard than for both under its test conditions; the structural difference it describes is that **WireGuard's encryption and decryption happen in the kernel, with no userspace daemon in the per-packet path.** No claim beyond the whitepaper's own measurements is made here.

## Two peers with wg-quick

Key generation, on each machine:

```bash
umask 077
wg genkey | tee private.key | wg pubkey > public.key
```

Server (the home-lab host, local network `192.168.1.0/24`), `/etc/wireguard/wg0.conf`:

```ini
[Interface]
Address    = 10.8.0.1/24
ListenPort = 51820
PrivateKey = <server-private-key>

[Peer]
# laptop
PublicKey  = <laptop-public-key>
AllowedIPs = 10.8.0.2/32
```

Laptop, `/etc/wireguard/wg0.conf`:

```ini
[Interface]
Address    = 10.8.0.2/24
PrivateKey = <laptop-private-key>

[Peer]
# home-lab server
PublicKey           = <server-public-key>
Endpoint            = vpn.example.org:51820
AllowedIPs          = 10.8.0.0/24, 192.168.1.0/24
PersistentKeepalive = 25
```

Note the asymmetry, which is the cryptokey-routing invariant expressed in configuration: the server grants the laptop **exactly one address**, while the laptop routes two prefixes towards the server. `AllowedIPs` is per-direction by construction, never a shared value.

Bringing the interfaces up and verifying:

```bash
wg-quick up wg0
wg show                               # handshake age, transfer counters
ping 10.8.0.1                         # from the laptop
systemctl enable --now wg-quick@wg0   # persist across boots
```

`wg show` reports a *latest handshake* timestamp per peer. **An absent or ageing handshake timestamp with a rising `transfer: … sent` counter and a static `received` counter is the signature of a one-way path** — the outbound packets leave, nothing authenticates on the way back.

Reaching the network behind the server (the `192.168.1.0/24` entry in the laptop's `AllowedIPs`) requires two things WireGuard does not do itself: forwarding enabled on the server, `sysctl -w net.ipv4.ip_forward=1`, and a masquerade rule for `10.8.0.0/24` out of the local-network interface, via nftables. With both in place, a service such as `http://192.168.1.50:3000` is reachable while only UDP port 51820 is exposed.

## AllowedIPs is a filter and a routing input

The name suggests an access-control list only. It is also the input to route installation: `wg-quick` reads each peer's `AllowedIPs` and installs kernel routes for those prefixes via the interface. On the laptop, `AllowedIPs = 10.8.0.0/24, 192.168.1.0/24` therefore means both "send these destinations into the tunnel" and "accept packets from the server only when sourced from these ranges."

Setting it to `0.0.0.0/0` yields a full-tunnel configuration. A plain default route through the tunnel would be circular — the encrypted packets themselves must reach the endpoint — so **wg-quick avoids the loop with an fwmark on the interface's own output and a policy-routing rule that exempts marked packets from the tunnel route.**

## Dropping wg-quick: systemd-networkd native

On a machine already managed by systemd-networkd, WireGuard is another netdev, requiring no `wg-quick@` unit. Server, `/etc/systemd/network/50-wg0.netdev`:

```ini
[NetDev]
Name = wg0
Kind = wireguard

[WireGuard]
PrivateKeyFile = /etc/systemd/network/wg0.key
ListenPort     = 51820
RouteTable     = main

[WireGuardPeer]
PublicKey  = <laptop-public-key>
AllowedIPs = 10.8.0.2/32
```

And `/etc/systemd/network/50-wg0.network`:

```ini
[Match]
Name = wg0

[Network]
Address = 10.8.0.1/24
```

Two behavioural differences from wg-quick. First, **networkd does not install routes for `AllowedIPs` unless directed to**; `RouteTable = main` requests it, a per-peer `RouteTable=` narrows it, and `off` disables it. Second, key material lives in unit-style files, so the permissions systemd.netdev(5) recommends apply to both the `.netdev` and the referenced key file: `chown root:systemd-network` and `chmod 0640`. Changes are applied with `networkctl reload` and inspected with `networkctl status wg0`; `wg show` continues to work, since the underlying kernel interface is the same.

A preshared key adds a symmetric layer on top of the Curve25519 agreement: `wg genpsk`, then `PresharedKey=` under each `[Peer]`. The same value must appear on both sides of the pair.

## Pitfalls

- **A peer behind network address translation (NAT) becomes unreachable after an idle period.** WireGuard emits nothing on an idle tunnel, so the NAT mapping expires and the peer with no configured `Endpoint` has no address to send to. `PersistentKeepalive = 25` emits a keepalive every 25 seconds; it belongs on the peer *behind* the NAT, directed at the reachable peer.
- **A destination outside every peer's `AllowedIPs` is dropped without an error.** There is no default peer and no fallback; the symptom is a timeout, not an ICMP rejection from the tunnel.
- **The same prefix listed under two peers on one interface is not a load-sharing configuration.** Outbound selection is longest-prefix match over the single table, so one peer receives the traffic and the other never does.
- **Copying `AllowedIPs` symmetrically onto both peers hands each the other's address range.** A server listing `10.8.0.0/24` for a laptop peer permits that laptop to source packets as any tunnel address, defeating the inbound source check.
- **Under systemd-networkd, a tunnel that comes up but carries no traffic to remote prefixes usually lacks `RouteTable=`.** The interface and handshake succeed while no route sends anything into it.
- **Reaching a network behind a peer fails when only `AllowedIPs` was widened.** Forwarding (`net.ipv4.ip_forward`) and the masquerade rule are separate kernel configuration that WireGuard neither reads nor sets.
- **A preshared key configured on one side only breaks the handshake.** The handshake never completes and `wg show` reports no latest handshake, rather than an authentication error naming the cause.
