---
title: "nftables: One Firewall Framework for All Address Families"
date: 2026-07-26
track: linux-tools
summary: How nftables unifies iptables/ip6tables/arptables/ebtables into one engine, the tables/chains/hooks model, named sets and verdict maps, and a stateful host firewall for /etc/nftables.conf.
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

**Gist.** Linux packet filtering was historically split across four user-space tools — `iptables`, `ip6tables`, `arptables`, `ebtables` — over the x_tables kernel engine, with rulesets loaded rule by rule so that a reload passed through partially applied states. nftables replaces all four with a single `nft` binary over the `nf_tables` kernel framework, in which rules compile to generic expressions, sets and maps are first-class kernel objects, and an entire ruleset loads as one transaction. The cost is a new rule language and a migration path: existing tooling either goes through the `iptables-nft` compatibility layer or is rewritten, and translated rules retain the linear shape of the originals until restructured into sets.

## What changed relative to x_tables

nftables was merged into the Linux kernel in 3.13 (2014) and is the backend behind `iptables` on current Debian, Ubuntu, RHEL/Fedora and Arch releases. The properties that distinguish it from x_tables:

- **One tool covering every address family.** IPv4, IPv6, ARP and bridge/Ethernet filtering share one syntax. An `inet` family table matches both IPv4 and IPv6 in a single rule set, removing the duplication that `ip6tables` previously required.
- **Atomic ruleset replacement.** `nft -f ruleset.nft` loads tables, chains, sets and rules as **one transaction**; no half-applied state is visible to traffic during the reload.
- **A generic in-kernel expression set.** The kernel exposes expressions — payload, meta, ct, immediate, lookup — that `nft` compiles rules into, in place of the fixed per-module match/target extensions of iptables. The same generic `lookup` expression is what makes sets and maps native rather than a separate `ipset` subsystem.
- **Named sets and maps inside the ruleset**, updated independently of the rules that reference them, matched by binary search or hashing rather than as a linear list of rules.
- **Fewer duplicate lookups at scale.** One `inet` pass replaces separate IPv4 and IPv6 passes, set and map lookups replace long linear chains, and there is no per-rule kernel module dispatch.

## The model: tables, chains, hooks

Every object lives in a **table**, scoped to a **family**:

| Family | Matches |
|---|---|
| `ip` | IPv4 only |
| `ip6` | IPv6 only |
| `inet` | IPv4 and IPv6 together (the common choice for host firewalls) |
| `arp` | ARP traffic |
| `bridge` | Ethernet frames on a bridge |
| `netdev` | Ingress, per-interface, earliest possible hook |

A table is a namespace holding **chains**. A chain is either a plain container reached by `jump`/`goto`, or a **base chain**, which attaches to a Netfilter **hook** in the kernel network stack: `prerouting`, `input`, `forward`, `output`, `postrouting`, or, in the `netdev` family, `ingress`. A base chain declares `type`, `hook` and `priority`:

```
chain input {
    type filter hook input priority 0; policy drop;
}
```

Priority is an integer and **lower priority runs first**. The wiki documents keyword aliases: `raw` (-300), `mangle` (-150), `dstnat` (-100), `filter` (0), `security` (50), `srcnat` (100). **Connection tracking hooks at priority -200**, so NAT chains must sit above that value; `dstnat` at -100 and `srcnat` at 100 both satisfy the constraint. Because ordering is expressed by a single integer, **multiple base chains may attach to the same hook** and are traversed in priority order — raw filtering, connection tracking, network address translation (NAT) and policy filtering can be separate, independently editable chains rather than fixed tables whose relative order is baked into the framework.

## A stateful host firewall

A complete `/etc/nftables.conf` for a server accepting SSH and HTTP/HTTPS, dropping everything else, covering IPv4 and IPv6 in one `inet` table:

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

The chain **policy is `drop`**, so a packet reaching the end of `input` unmatched is discarded; the final rule exists to record a rate-limited sample of those packets before the policy applies. Rule order is load-bearing: the `ct state established,related accept` rule short-circuits the return traffic of connections already permitted, so later rules examine only new or invalid flows. `ct state invalid drop` discards packets that connection tracking cannot associate with a flow.

Load with `nft -f /etc/nftables.conf` and persist with `systemctl enable --now nftables.service`; Debian ships example rulesets under `/usr/share/doc/nftables/examples/`. The `flush ruleset` statement at the top of the file combined with a single `-f` load is where atomicity matters — **the flush and the new ruleset are one transaction**, so a syntax error later in the file leaves the previous ruleset intact rather than a flushed, empty one.

## Sets and verdict maps

Anonymous sets such as `{ ssh, http, https }` are compiled inline and cannot be modified without reloading the rule containing them. A **named set** is addressable at runtime:

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
        # ... remaining rules unchanged
    }
}
```

`flags interval` marks the set as holding ranges and prefixes rather than only single addresses, which is what allows `203.0.113.0/24` as an element. Membership changes without touching the ruleset text:

```
nft add element inet filter blackhole { 192.0.2.55 }
nft delete element inet filter blackhole { 192.0.2.55 }
```

A **named map** associates a key with a value. When the value type is `verdict`, the map replaces a chain of per-port rules with a single lookup:

```nft
map port_verdicts {
    type inet_service : verdict
    elements = { 22 : accept, 80 : accept, 443 : accept, 23 : drop }
}
```

referenced as:

```
tcp dport vmap @port_verdicts
```

This is a **verdict map (vmap)**: rather than evaluating N rules in sequence, the kernel performs one lookup and applies the associated verdict directly. The saving grows with the number of entries, which is the case that matters for rulesets covering many ports, addresses or per-tenant policies.

## Migrating from iptables

The project ships a compatibility layer, `iptables-nft`, which accepts iptables syntax and installs rules into the `nf_tables` backend:

```
update-alternatives --set iptables /usr/sbin/iptables-nft
update-alternatives --set ip6tables /usr/sbin/ip6tables-nft
```

This has been the default on Debian since Buster and on most current distributions, so `iptables -L` on a current system is likely already reading `nf_tables` rather than legacy x_tables.

`iptables-translate` converts individual rules:

```
$ iptables-translate -A INPUT -p tcp --dport 22 -m conntrack --ctstate NEW -j ACCEPT
nft add rule ip filter INPUT tcp dport 22 ct state new counter accept
```

An entire saved ruleset converts through the restore path:

```
iptables-save > save.txt
iptables-restore-translate -f save.txt > ruleset.nft
nft -f ruleset.nft
```

The translator preserves rule structure one-for-one. Repeated port and address rules remain repeated rules; collapsing them into sets and maps is a separate manual step.

## iptables and nftables compared

| | iptables | nftables |
|---|---|---|
| Tools | iptables, ip6tables, arptables, ebtables | one `nft` binary |
| IPv4/IPv6 | separate tables, duplicated rules | one `inet` table, both |
| Ruleset reload | rule-by-rule, non-atomic | single atomic transaction |
| Dynamic groups | separate `ipset` subsystem | native sets/maps in-kernel |
| Rule dispatch | linear match per rule | linear rules plus set/map lookups |
| Config syntax | flag-based CLI | declarative chain/rule blocks, scriptable |

A useful next experiment: a `netdev` ingress chain dropping malformed packets before connection tracking observes them, compared against `nft --debug=netlink` output to see what executes before priority 0.

## Pitfalls

- **Running native `nft` rules and legacy (non-nft) `iptables` against the same traffic.** Two kernel subsystems evaluate overlapping chains and the resulting verdict depends on hook ordering that neither tool displays, producing filtering behaviour that neither ruleset alone explains.
- **Setting `policy drop` on `input` without first accepting `iifname "lo"`.** Local services that communicate over loopback fail with connection-refused or timeouts while the ruleset appears to concern only external traffic.
- **Omitting the ICMPv6 neighbour discovery types on an IPv6 network.** Neighbour solicitation and advertisement are dropped, so address resolution fails and the host becomes unreachable over IPv6 even though the accept rules for TCP services are present.
- **Placing a NAT base chain at priority below -200.** Connection tracking has not yet run at that point, so the NAT chain sees packets without conntrack state.
- **Using an anonymous set where membership must change.** `{ ... }` sets are compiled into the rule; adding an element requires replacing the rule, whereas `nft add element` works only against a named set.
- **Treating `iptables-translate` output as a finished ruleset.** The translation is rule-for-rule, so a ruleset with hundreds of single-port rules produces hundreds of single-rule nftables entries rather than one verdict map.
- **Loading a new ruleset without `flush ruleset` at the top.** The load is still atomic, but it adds to the existing ruleset instead of replacing it, leaving stale rules whose position may precede the new ones.
