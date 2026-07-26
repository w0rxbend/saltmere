---
title: "nftables: One Firewall Framework to Rule Them All"
date: 2026-07-26
track: linux-tools
summary: How nftables unifies iptables/ip6tables/arptables/ebtables into one engine, the tables/chains/hooks model, and a working stateful host firewall you can paste into /etc/nftables.conf.
reading_time: 6
tags: [linux, nftables, iptables, firewall, netfilter, networking, security]
sources:
  - title: "nft(8) — nftables — Debian manpages"
    url: "https://manpages.debian.org/testing/nftables/nft.8.en.html"
  - title: "Quick reference-nftables in 10 minutes - nftables wiki"
    url: "https://wiki.nftables.org/wiki-nftables/index.php/Quick_reference-nftables_in_10_minutes"
  - title: "Netfilter hooks - nftables wiki"
    url: "https://wiki.nftables.org/wiki-nftables/index.php/Netfilter_hooks"
  - title: "Sets - nftables wiki"
    url: "https://wiki.nftables.org/wiki-nftables/index.php/Sets"
  - title: "Moving from iptables to nftables - nftables wiki"
    url: "https://wiki.nftables.org/wiki-nftables/index.php/Moving_from_iptables_to_nftables"
---

## Why nftables exists

For twenty years, Linux packet filtering meant `iptables`, `ip6tables`, `arptables`, and `ebtables` — four separate tools, four separate rule languages, four separate kernel matching engines (x_tables), each reloaded rule-by-rule. Every `iptables -A` call was a distinct syscall, and reloading a large ruleset meant flushing chains and re-inserting rules one at a time, with a window where the firewall was partially applied.

`nftables`, merged into the kernel in 3.13 (2014) and now the default backend on Debian, Ubuntu, RHEL/Fedora, and Arch, replaces all four tools with a single `nft` binary and a single in-kernel framework (`nf_tables`). The pitch, straight from the project:

- **One tool, all families.** IPv4, IPv6, ARP, and bridge/Ethernet filtering share the same syntax. An `inet` family table matches both IPv4 and IPv6 in one rule set, killing the old duplicate-everything-in-ip6tables problem.
- **Atomic ruleset replacement.** `nft -f ruleset.nft` loads an entire ruleset — tables, chains, sets, rules — as one transaction. There's no half-applied state visible to traffic mid-reload.
- **A real data-plane VM.** The kernel exposes generic expressions (payload, meta, ct, immediate, lookup) that `nft` compiles rules into, instead of iptables' fixed set of per-module match/target extensions. This is also what makes maps and sets first-class instead of a bolted-on `ipset`.
- **Built-in sets and maps.** Named sets and key-value maps live inside the ruleset itself and update in O(log n) via binary search or hashing, rather than being matched as a linear list of rules.
- **Better performance at scale.** Fewer duplicate lookups (one ip+ip6 pass instead of two), native set/map lookups instead of long linear chains, and no per-rule kernel module dispatch overhead.

## The model: tables, chains, hooks

Everything in nftables lives in a **table**, scoped to a **family**:

| Family | Matches |
|---|---|
| `ip` | IPv4 only |
| `ip6` | IPv6 only |
| `inet` | IPv4 and IPv6 together (the common choice for host firewalls) |
| `arp` | ARP traffic |
| `bridge` | Ethernet frames on a bridge |
| `netdev` | Ingress, per-interface, earliest possible hook |

A table is just a namespace holding **chains**. A chain is either a plain container you `jump`/`goto` into, or a **base chain**, which attaches to a Netfilter **hook** in the kernel network stack: `prerouting`, `input`, `forward`, `output`, `postrouting`, or (netdev-only) `ingress`. A base chain declares `type`, `hook`, and `priority`:

```
chain input {
    type filter hook input priority 0; policy drop;
}
```

Priority is an integer — lower runs first — with conventional keyword aliases the wiki documents: `raw` (-300), `mangle` (-150), `dstnat` (-100), `filter` (0), `security` (50), `srcnat` (100). NAT chains (`dstnat`/`srcnat`) must sit at priority > -200 because conntrack itself hooks at -200; this is why NAT and filtering can coexist as separate base chains at different priorities instead of fighting over target/table ordering the way iptables' NAT and filter tables did.

Multiple base chains can attach to the same hook — packets traverse them in priority order — which is how you can split raw filtering, connection tracking, NAT, and policy filtering into independently manageable chains instead of one monolithic table.

## A complete stateful host firewall

This is a full `/etc/nftables.conf` for a typical server: SSH, HTTP/S in, everything else dropped, IPv4+IPv6 in one table via `inet`.

```nft
#!/usr/sbin/nft -f

flush ruleset

table inet filter {
    chain input {
        type filter hook input priority filter; policy drop;

        # loopback and established/related traffic first
        iifname "lo" accept
        ct state established,related accept
        ct state invalid drop

        # ICMP/ICMPv6 needed for path MTU discovery, neighbor discovery, etc.
        icmp type { echo-request, destination-unreachable, time-exceeded } accept
        icmpv6 type { echo-request, nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert } accept

        # allowed services
        tcp dport { ssh, http, https } ct state new accept

        # log then drop everything else (rate-limited)
        limit rate 5/minute log prefix "nft-drop: " counter drop
    }

    chain forward {
        type filter hook forward priority filter; policy drop;
    }

    chain output {
        type filter hook output priority filter; policy accept;
    }
}
```

Load it with `nft -f /etc/nftables.conf`, enable persistence with `systemctl enable --now nftables.service` (Debian ships example rulesets under `/usr/share/doc/nftables/examples/`). The `flush ruleset` at the top plus the single `-f` load is the atomicity guarantee in action — the whole thing lands or none of it does.

## Sets and named maps

Anonymous sets like `{ ssh, http, https }` above get compiled inline. For anything you'll update independently of the ruleset, use a **named set**:

```nft
table inet filter {
    set blackhole {
        type ipv4_addr
        flags interval
        elements = { 203.0.113.0/24, 198.51.100.7 }
    }

    chain input {
        type filter hook input priority filter; policy drop;
        ip saddr @blackhole drop
        ...
    }
}
```

Add or remove members without touching the ruleset text:

```
nft add element inet filter blackhole { 192.0.2.55 }
nft delete element inet filter blackhole { 192.0.2.55 }
```

**Named maps** go further, associating a key with a value — commonly a verdict, turning a whole chain of `tcp dport X accept` rules into one lookup:

```nft
map port_verdicts {
    type inet_service : verdict
    elements = { 22 : accept, 80 : accept, 443 : accept, 23 : drop }
}
```

used in a rule as:

```
tcp dport vmap @port_verdicts
```

This is a **verdict map (vmap)**: instead of walking N rules linearly, the kernel does one hash/interval lookup and jumps straight to the matching verdict — the performance win scales with ruleset size, which matters once you're managing hundreds of ports, IPs, or per-tenant rules.

## Migrating from iptables

You don't have to rewrite everything by hand. The nftables project ships a compatibility layer, `iptables-nft`, that accepts iptables syntax but installs rules into the nf_tables kernel backend:

```
update-alternatives --set iptables /usr/sbin/iptables-nft
update-alternatives --set ip6tables /usr/sbin/ip6tables-nft
```

This is the default on Debian since Buster and on most current distros — running `iptables -L` today likely already talks to nf_tables under the hood, not the legacy x_tables path.

For converting actual rule syntax, use `iptables-translate`:

```
$ iptables-translate -A INPUT -p tcp --dport 22 -m conntrack --ctstate NEW -j ACCEPT
nft add rule ip filter INPUT tcp dport 22 ct state new counter accept
```

And for a whole saved ruleset:

```
iptables-save > save.txt
iptables-restore-translate -f save.txt > ruleset.nft
nft -f ruleset.nft
```

Two caveats worth internalizing before you migrate: don't run native `nft` rules and legacy (non-nft) `iptables` simultaneously against the same traffic — mixing x_tables and nf_tables kernel subsystems on overlapping chains produces confusing, order-dependent results. And treat translated output as a starting draft, not a finished ruleset — collapse repeated port/IP rules into sets and maps afterward; the translator won't do that restructuring for you.

## iptables vs nftables at a glance

| | iptables | nftables |
|---|---|---|
| Tools | iptables, ip6tables, arptables, ebtables | one `nft` binary |
| IPv4/IPv6 | separate tables, duplicated rules | one `inet` table, both |
| Ruleset reload | rule-by-rule, non-atomic | single atomic transaction |
| Dynamic groups | bolted-on `ipset` | native sets/maps in-kernel |
| Rule dispatch | linear match per rule | linear rules + O(log n) set/map lookups |
| Config syntax | flag-based CLI | declarative chain/rule blocks, scriptable |

**Try next:** write a `netdev` ingress chain that drops spoofed/invalid packets before conntrack even sees them, and compare `nft --debug=netlink` output against the plain ruleset to see how much work happens before priority 0.
