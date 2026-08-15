---
title: "WireGuard on Linux from Scratch: Cryptokey Routing in ~4k Lines"
date: 2026-08-15
track: linux-tools
summary: "WireGuard replaces the certificate bureaucracy of OpenVPN and IPsec with one idea: a public key mapped to a list of allowed IPs. Here's the model, a complete two-peer setup with wg-quick, why AllowedIPs is both firewall and routing table, and the systemd-networkd native config that drops wg-quick entirely."
reading_time: 5
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

If you run a homelab — sensor nodes, a dashboard, a NAS — you eventually want to reach it from a coffee shop without exposing Grafana to the internet. **WireGuard** is the shortest path there. It has been in the mainline kernel since Linux 5.6 (2020), so on any current distro the data plane is already loaded; you only need the `wireguard-tools` package for the `wg` and `wg-quick` userspace helpers.

## The model: Noise handshake plus cryptokey routing

WireGuard's crypto is fixed, not negotiated. Every tunnel uses the Noise protocol framework's IK handshake — the whitepaper names the exact construction, `Noise_IKpsk2_25519_ChaChaPoly_BLAKE2s` — meaning Curve25519 keys, ChaCha20-Poly1305 encryption, BLAKE2s hashing, one round trip to establish a session. There are no cipher suites, no certificates, no X.509, no TLS. A peer's identity *is* its 32-byte public key, exchanged out of band exactly like an SSH key.

The second idea is **cryptokey routing**: each interface holds a table mapping peer public keys to the IP ranges that peer is allowed to use inside the tunnel. Outbound, the kernel looks up the destination IP in that table to decide which peer to encrypt for. Inbound, a decrypted packet is dropped unless its source IP matches the sending peer's entry. Because identity is the key rather than the outer address, peers roam freely — your laptop can hop from Wi-Fi to LTE and the tunnel just follows.

## Why ~4k lines in-kernel matters

The whitepaper's headline claim is that WireGuard "can be simply implemented for Linux in less than 4,000 lines of code." Compare the alternatives: OpenVPN is a userspace daemon dragging in a full TLS stack, and IPsec splits the job between the kernel XFRM framework and an IKE daemon like strongSwan — together hundreds of thousands of lines. Less code means a surface you can actually audit, and living entirely in-kernel means no userspace copies per packet, which is most of why WireGuard benchmarks faster than both.

## Two peers with wg-quick

Generate a keypair on each machine:

```bash
umask 077
wg genkey | tee private.key | wg pubkey > public.key
```

Server (the homelab box, LAN `192.168.1.0/24`), `/etc/wireguard/wg0.conf`:

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
# homelab server
PublicKey           = <server-public-key>
Endpoint            = vpn.example.org:51820
AllowedIPs          = 10.8.0.0/24, 192.168.1.0/24
PersistentKeepalive = 25
```

Bring both up and verify:

```bash
wg-quick up wg0
wg show                          # handshake time, transfer counters
ping 10.8.0.1                    # from the laptop
systemctl enable --now wg-quick@wg0   # persist across boots
```

For the laptop to reach the LAN behind the server (that `192.168.1.0/24` in its AllowedIPs), enable forwarding on the server — `sysctl -w net.ipv4.ip_forward=1` — and add a masquerade rule for `10.8.0.0/24` out the LAN interface with nftables. Now `http://192.168.1.50:3000` — the Grafana box fed by your sensors — works from anywhere, with nothing but UDP 51820 exposed.

## AllowedIPs is two things at once

The name misleads people into treating it as only an ACL. It is also the routing input: `wg-quick` reads each peer's `AllowedIPs` and installs kernel routes for those prefixes via the interface. So on the laptop, `AllowedIPs = 10.8.0.0/24, 192.168.1.0/24` means both "route these destinations into the tunnel" and "accept packets from the server only if sourced from these ranges." Set it to `0.0.0.0/0` and you get a full-tunnel VPN — wg-quick even handles the default-route gymnastics with an fwmark and a policy-routing rule. On the server side, the narrow `10.8.0.2/32` means the laptop can never spoof another tunnel address, no matter what it sends.

## NAT and persistent keepalive

WireGuard is silent by design — no handshake happens until there is traffic, and an idle tunnel sends nothing. That stealth breaks behind NAT: the laptop's conntrack mapping expires, and the server (which has no stable endpoint for a roaming peer) can no longer reach it. `PersistentKeepalive = 25` fixes this by emitting a tiny keepalive every 25 seconds, holding the NAT mapping open. The rule of thumb: set it on the peer *behind* NAT, pointing at the reachable peer — and on IoT-ish nodes that must stay reachable for pushes from the server, it is non-negotiable.

## Dropping wg-quick: systemd-networkd native

If a machine already uses systemd-networkd (common on headless homelab boxes), WireGuard is just another netdev — no `wg-quick@` unit, no shell script at all. Server config, `/etc/systemd/network/50-wg0.netdev`:

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

Two differences from wg-quick to know. First, networkd does *not* install routes for `AllowedIPs` unless you ask — that is what `RouteTable = main` does (per-peer `RouteTable=` works too, and `off` disables it). Second, the key material lives in unit-style files, so lock them down the way systemd.netdev(5) recommends: `chown root:systemd-network` and `chmod 0640` on both the `.netdev` and the referenced key file. Apply with `networkctl reload`, inspect with `networkctl status wg0` — and `wg show` still works, because underneath it is the same kernel interface.

**Try next:** add a preshared key (`wg genpsk`, then `PresharedKey=` under each `[Peer]`) for post-quantum-hedge symmetric layering, and watch `wg show wg0 latest-handshakes` while toggling your laptop between Wi-Fi and a phone hotspot to see roaming re-pin the endpoint.
