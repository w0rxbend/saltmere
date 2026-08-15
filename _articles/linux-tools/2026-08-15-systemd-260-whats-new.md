---
title: "systemd 259 and 260: What Operators Actually Need to Know"
date: 2026-08-15
track: linux-tools
summary: "Two systemd releases landed since v258: v259 (December 2025) made the journal persistent by default and shipped experimental musl support, and v260 (March 2026) deleted System V script compatibility outright and introduced mstack for declarative overlay mounts. Here's the operator-relevant tour, with commands."
reading_time: 5
tags: [systemd, systemd-networkd, journald, mstack, run0, linux-tools]
sources:
  - title: "systemd NEWS file (official changelog)"
    url: "https://github.com/systemd/systemd/blob/main/NEWS"
  - title: "Releases · systemd/systemd (GitHub)"
    url: "https://github.com/systemd/systemd/releases"
  - title: "systemd 260 Released: mstack, SysV Service Scripts Removed & AI Agents Documentation (Phoronix)"
    url: "https://www.phoronix.com/news/systemd-260-Released"
  - title: "systemd 259 Released With Experimental Musl libc Support, More Features (Phoronix)"
    url: "https://www.phoronix.com/news/systemd-259"
  - title: "systemd 259 Released With Major Changes Ahead of Legacy SysV Removal (Linuxiac)"
    url: "https://linuxiac.com/systemd-259-released-with-major-changes-ahead-of-legacy-sysv-removal/"
---

Quick version check first: as of August 2026 the latest stable systemd is **v260** (released 17 March 2026), following **v259** (17 December 2025); v261 is in release candidates. v258 back in September 2025 was the headline-grabber — cgroup v1 support deleted, run0 polish (covered in our [run0 article](/articles/linux-tools/2026-07-30-run0-setuid-free-privilege-elevation)) — but the two releases since then carry plenty that changes day-to-day operations. Here is what matters, verified against the official NEWS file.

## v260: System V scripts are gone. Actually gone.

v259 warned; v260 executed. The `systemd-sysv-generator`, `systemd-rc-local-generator` (and with it `rc-local.service`), and the `systemd-sysv-install` hook behind `systemctl enable` for init scripts have all been removed. The NEWS file is blunt: "Please make sure to update your software *now* to include a native systemd unit file instead of a legacy System V script." Before your distro ships v260, audit for stragglers:

```bash
ls /etc/init.d/ 2>/dev/null                     # any non-stub scripts left?
systemctl status some-legacy.service            # "Loaded: ... generated" = sysv-generated
systemd-analyze verify /etc/systemd/system/*.service
```

Anything still living in `/etc/init.d` needs a real unit file — usually ten lines of `[Service]` with `ExecStart=` — before the upgrade, not after.

Baselines moved too: v260 requires Linux kernel ≥ 5.10 (recommended 5.14, full functionality at 6.6), and v259 already bumped glibc to ≥ 2.34. Only ancient appliances will notice, but check your oldest boxes.

## v260: mstack, declarative mount stacks

The genuinely new subsystem in v260 is **mstack**: a way to define an overlayfs/bind-mount arrangement by structuring a `.mstack/` directory according to a spec, instead of hand-writing mount units or `ExecStartPre=` mount incantations. It arrives in three forms: a `systemd-mstack` CLI for interactive use, a `RootMStack=` unit setting so a service can run on top of a composed stack, and a `--mstack=` option for `systemd-nspawn` containers. If you have used `systemd-sysext` to layer images over `/usr` (see our [sysext article](/articles/linux-tools/2026-07-31-systemd-sysext-extensions)), mstack generalizes the idea from "extend the OS image" to "compose arbitrary mount trees per service or container." It is young — expect the spec to move — but `systemd-mstack --help` on a v260 box is worth ten minutes.

## v259: your journal is persistent now

The journald `Storage=` default flipped from `auto` to `persistent`. Practically: fresh installs write logs to `/var/log/journal` even if nobody created that directory, and logs survive reboots. That is what most operators wanted anyway — but on small flash-backed IoT gateways it means journald is now writing to disk where it previously stayed in tmpfs. Check and cap it:

```bash
journalctl --disk-usage
grep -r Storage= /etc/systemd/journald.conf*    # set Storage=volatile to opt out
sudo journalctl --vacuum-size=200M
```

Set `SystemMaxUse=` in `journald.conf` on anything with an SD card you care about.

## v259: run0 --empower, dlopen diets, faster boots

`run0` gained `--empower`: elevated privileges — full ambient capabilities plus an "empower" group membership — *without* switching identity to root. It slots between plain `run0` (become root) and `run0 --property=` fine-tuning, and it is the right tool for "this command needs CAP_NET_ADMIN, not a root shell."

```bash
run0 --empower -- ip link set dev wg0 up
```

Footprint and speed work landed too: a pile of dependencies (libaudit, PAM, libacl, libblkid, libseccomp, libselinux, libmount) are now `dlopen()`ed on demand rather than hard-linked — shrinking containers that ship systemd — libcap is not linked at all, and `systemd-modules-load` loads configured kernel modules in parallel. And the long-running portability taboo broke: v259 ships *experimental, incomplete* musl libc support, with documented gaps around NSS, `DynamicUser=`, and per-user services. Alpine-style systemd images are no longer a contradiction in terms, just an adventure.

## Networking and resource control odds and ends

Three smaller items worth flagging. First, systemd-networkd dropped iptables/libiptc in v259 — nftables only, so if `IPMasquerade=` quietly depended on the legacy backend, migrate (our [nftables article](/articles/linux-tools/2026-07-26-nftables-modern-firewall) covers the mapping). Second, v260 adds `MemoryTHP=`, per-service control of Transparent Huge Pages — no more system-wide `/sys/kernel/mm/transparent_hugepage/enabled` compromises between the database that hates THP and everything else. Third, Varlink keeps spreading: v260 introduces a service registry under `/run/varlink/registry/` and networkd link-control methods, all pokeable with the bundled client:

```bash
ls /run/varlink/registry/
varlinkctl introspect /run/systemd/io.systemd.Hostname
```

**Try next:** on a v260 test VM, pick one real service you still start from `/etc/init.d`, port it to a native unit, then rebuild its filesystem view with `systemd-mstack` instead of bind-mount `ExecStartPre=` lines — the before/after diff of the unit file is the whole pitch.
