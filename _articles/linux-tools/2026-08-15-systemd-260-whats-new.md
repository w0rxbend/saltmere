---
title: "systemd 259 and 260: The Operator-Relevant Changes"
date: 2026-08-15
track: linux-tools
summary: "Two systemd releases landed since v258: v259 (December 2025) made the journal persistent by default and shipped experimental musl support, and v260 (March 2026) removed System V script compatibility outright and introduced mstack for declarative overlay mounts. An operator-oriented tour, with commands."
reading_time: 6
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

**Gist.** systemd v259 (December 2025) and v260 (March 2026) change defaults and remove compatibility layers that many production hosts still depend on: the System V init script bridge is gone, journald now writes to disk by default, and a new subsystem named mstack composes mount trees declaratively. The mechanism in each case is the same — behaviour that used to be synthesised at runtime by a generator or inferred from the filesystem is now either declared explicitly in a unit or not available at all. The cost is a migration burden that falls before the upgrade rather than after it: an unported init script becomes a service that does not exist, and a host that never had `/var/log/journal` begins consuming persistent storage without being asked.

As of August 2026 the latest stable systemd is **v260**, following **v259**. v258 (September 2025) carried the larger structural change — control group (cgroup) v1 support was deleted and run0 was refined (see the [run0 article](/articles/linux-tools/2026-07-30-run0-setuid-free-privilege-elevation)) — but the two releases since then alter day-to-day operations. The claims below are taken from the official NEWS file.

## v260: removal of the System V compatibility path

v259 warned about the removal; v260 performed it. Three components are gone:

- **`systemd-sysv-generator`**, the generator that ran early in each manager startup, scanned `/etc/init.d`, and synthesised a `.service` unit in a transient directory for every script it found.
- **`systemd-rc-local-generator`**, and with it `rc-local.service`, the unit that executed `/etc/rc.local` late in boot.
- **`systemd-sysv-install`**, the hook that `systemctl enable` invoked to delegate enablement of an init script to the distribution's own tooling.

The failure mode follows directly from the generator's position in the startup sequence. Generators run before the manager computes the transaction, so under v259 an init script *became* a unit and every subsequent command — `systemctl status`, `systemctl enable`, dependency resolution from other units — operated on that synthesised unit as though it had been written by hand. Under v260 the generator does not run, so no unit is synthesised, and **the script does not fail: it ceases to exist as far as the manager is concerned.** A unit that declares `After=some-legacy.service` against a name that no longer resolves does not block, and a boot that formerly started the service completes without it. Silence, not an error, is the symptom.

The NEWS file states the requirement plainly, directing maintainers to update software now to include a native systemd unit file instead of a legacy System V script. The audit is cheap and belongs before the distribution ships v260:

```bash
ls /etc/init.d/ 2>/dev/null                     # non-stub scripts remaining?
systemctl status some-legacy.service            # "Loaded: ... generated" = sysv-generated
systemd-analyze verify /etc/systemd/system/*.service
```

The marker to look for is the word `generated` in the `Loaded:` line, which identifies a unit produced by a generator rather than read from disk. Anything still resident in `/etc/init.d` requires a native unit file — at minimum a `[Service]` section with an `ExecStart=` — written before the upgrade.

Baselines moved in the same window: both releases raise the minimum Linux kernel and GNU C Library (glibc) versions systemd builds and runs against, and the README in the source tree records the exact figures for a given release. These bounds bind only on long-lived appliances and older images; the numbers should be read off the README of the version being deployed rather than assumed.

## v260: mstack, declarative mount stacks

The new subsystem in v260 is **mstack**. It defines an overlayfs and bind-mount arrangement by **structuring a `.mstack/` directory according to a specification**, in place of hand-written mount units or a sequence of `ExecStartPre=` mount commands. It is exposed in three forms:

- a `systemd-mstack` command-line tool for interactive use;
- a **`RootMStack=` unit setting**, so a service runs on top of a composed stack;
- a **`--mstack=` option for `systemd-nspawn`**, applying the same composition to a container.

The distinction from the older mechanism is where the arrangement is recorded. An `ExecStartPre=` mount sequence is imperative: the resulting tree exists only as the accumulated effect of commands that ran, and its teardown is a separate obligation. A `.mstack/` directory is a description that the manager reads. For readers who have used `systemd-sysext` to layer images over `/usr` (see the [sysext article](/articles/linux-tools/2026-07-31-systemd-sysext-extensions)), mstack widens the same idea from extending the operating-system image to composing arbitrary mount trees per service or container. The subsystem is new and its specification should be expected to change; `systemd-mstack --help` on a v260 host is the current authority.

## v259: persistent journal by default

The journald `Storage=` default changed from `auto` to `persistent`. The two values differ in one condition. Under `auto`, journald writes to `/var/log/journal` **only if that directory already exists**, and otherwise falls back to `/run/log/journal`, which lives on a tmpfs and is therefore discarded at reboot. Under `persistent`, journald **creates `/var/log/journal` if it is absent** and writes there. A fresh install consequently retains logs across reboots without any operator action.

The consequence worth planning for is on flash-backed devices. A gateway whose journal previously stayed in RAM now issues writes to the storage device on every logged message, and the journal grows until a limit applies. The relevant controls:

```bash
journalctl --disk-usage
grep -r Storage= /etc/systemd/journald.conf*    # Storage=volatile opts out
sudo journalctl --vacuum-size=200M
```

`Storage=volatile` restores tmpfs-only behaviour. `SystemMaxUse=` in `journald.conf` bounds the on-disk total and is the setting to apply on any host backed by an SD card.

## v259: run0 --empower, dlopen'ed dependencies, parallel module loading

`run0` gained **`--empower`**, which raises the privileges of the invoking user **without switching identity to root**. It occupies the space between plain `run0`, which becomes root, and `run0 --property=` fine-tuning — applicable where a command needs elevated capabilities rather than a root shell. The precise set granted is documented in `run0(1)` on the host and is the authority for whether a given command is covered.

```bash
run0 --empower -- ip link set dev wg0 up
```

Footprint and startup work landed alongside. A set of dependencies — libaudit, PAM, libacl, libblkid, libseccomp, libselinux, libmount — is now loaded through `dlopen()` on demand instead of being linked at build time, so the corresponding shared object need not be present unless the feature that uses it is exercised; and `systemd-modules-load` loads its configured kernel modules in parallel. v259 also ships **experimental and incomplete musl libc support**, with documented gaps around the Name Service Switch (NSS), `DynamicUser=`, and per-user services.

## Networking and resource control

Three smaller items. First, **systemd-networkd dropped iptables and libiptc in v259**, leaving nftables as the only backend; a configuration relying on `IPMasquerade=` through the legacy path needs migration (the [nftables article](/articles/linux-tools/2026-07-26-nftables-modern-firewall) covers the mapping). Second, v260 adds **`MemoryTHP=`**, per-service control of Transparent Huge Pages (THP). The pre-existing control, `/sys/kernel/mm/transparent_hugepage/enabled`, is system-wide, so a host running one workload that tolerates THP poorly and others that do not care had a single policy to set; `MemoryTHP=` moves the setting to the unit. Third, v260 introduces a **Varlink service registry under `/run/varlink/registry/`** and networkd link-control methods, both reachable with the bundled client:

```bash
ls /run/varlink/registry/
varlinkctl introspect /run/systemd/io.systemd.Hostname
```

## Pitfalls

- A service started from `/etc/init.d` disappears after the v260 upgrade without an error: the generator that synthesised its unit was removed, so the unit name no longer resolves and dependent units treat it as absent rather than failed.
- `/etc/rc.local` stops running on v260 because `systemd-rc-local-generator` and `rc-local.service` were removed, not because the file lost its execute bit.
- A flash-backed device fills its storage after the v259 upgrade: `Storage=persistent` creates `/var/log/journal` where `Storage=auto` previously left the journal on tmpfs, and no size bound applies unless `SystemMaxUse=` is set.
- `systemctl enable` on an init script fails on v260 because `systemd-sysv-install` — the hook that forwarded enablement to the distribution's tooling — no longer exists.
- Network address translation configured through `IPMasquerade=` stops taking effect after v259 if it depended on the iptables backend, which was removed along with libiptc.
- A host that satisfies systemd's minimum kernel and glibc versions can still lack full functionality: the README distinguishes the minimum it builds against from the version at which every feature is available, and the gap is silent rather than a build or boot failure.
- `.mstack/` layouts written against v260 may require revision: the subsystem is new and its specification is not settled.
