---
title: "run0: privilege elevation without the setuid bit"
date: 2026-07-30
track: linux-tools
summary: "sudo has been setuid-root for decades, which means it runs privileged code inside an environment an attacker controls. systemd's run0 flips the model: it asks PID 1 to spawn a clean root process instead. Here's how it works and how to use it."
reading_time: 5
tags: [systemd, sudo, security, polkit, privilege-escalation]
sources:
  - title: "\"run0\" as a sudo replacement (LWN.net)"
    url: "https://lwn.net/Articles/971745/"
  - title: "run0(1) — Arch manual pages"
    url: "https://man.archlinux.org/man/run0.1.en"
  - title: "Systemd v256 Introduces run0: A Safer Alternative to sudo (Linuxiac)"
    url: "https://linuxiac.com/systemd-v256-introduces-run0/"
  - title: "systemd 258 Released (Phoronix)"
    url: "https://www.phoronix.com/news/systemd-258"
---

Type `ls -l $(which sudo)` and look at the permission bits: `-rwsr-xr-x`. That `s` is the setuid bit. It means every time you run `sudo`, the kernel starts the binary as **root**, but with an environment — variables, file descriptors, terminal, cwd — that *you*, the unprivileged caller, fully control. Privileged code, hostile inputs. sudo spends a lot of its codebase carefully scrubbing that environment, and history shows how easy it is to miss a spot (Baron Samedit, CVE-2021-3156, was a heap overflow reachable by any local user).

systemd 256 (June 2024) shipped **run0** to attack that structural problem instead of patching around it. As of mid-2026 systemd is up to the 261 series, with 258 as the long-lived autumn-2025 release, and run0 has been present the whole time.

## The setuid problem, concretely

A setuid binary inherits the caller's world and has to defend against all of it:

- `$LD_PRELOAD`, `$LD_LIBRARY_PATH`, `$PATH`, `$IFS` and friends
- inherited file descriptors and `RLIMIT_*` values
- the controlling terminal (TIOCSTI injection, historically)
- a `cwd` the attacker chose

Every one of these has been a real exploit primitive at some point. Lennart Poettering's framing in the announcement is blunt: an execution context for privileged code that's "half under the control of unprivileged code and that needs careful manual clean-up is just not how security engineering should be done in 2024 anymore."

## What run0 does differently

run0 is not setuid. It's a symlink to `systemd-run`, and it behaves differently based on the name it's invoked as. When you run it, it does **not** try to become root itself. It sends a request over D-Bus to **PID 1** (systemd, already running as root) asking it to spawn your command as a *fresh transient unit*.

That process is born from PID 1's clean context — not yours. It gets the service manager's environment, not your polluted one. run0 allocates a new **pseudo-terminal**, connects it to your real terminal, and shuttles bytes between them. There is no privileged code running in a process you control, so there's nothing for a malicious `$LD_PRELOAD` to hook.

Authentication goes through **polkit**, not the setuid binary and not `/etc/sudoers`. Because polkit runs as its own service, the authentication prompt is isolated from your terminal — a compromised TTY can't fake or intercept it the way it can with a plain password prompt.

And the visible cue: by default run0 **tints your terminal background** — reddish when the target is root, yellowish for any other UID — so you have a hard-to-miss signal that this shell is dangerous. It reverts when the session ends.

## Basic usage

Run a single command as root:

```bash
run0 systemctl restart nginx
run0 nano /etc/hosts
```

With no command, you get an interactive root shell (with the red background):

```bash
run0
```

Run as a *specific* user instead of root with `-u`/`--user` (and `-g`/`--group`):

```bash
run0 -u postgres psql
run0 --user=www-data -g www-data ./deploy.sh
```

Push a long-running job into the background — run0 sets the terminal background color for it via `--background`, and you can disable the tint entirely with an empty value:

```bash
run0 --background='' systemctl status        # no color tint
run0 --nice=10 -D /srv/app ./batch-job        # niceness + working dir
```

Because every invocation is a transient systemd unit, you can attach resource controls directly with `--property`, which has no clean `sudo` equivalent:

```bash
run0 --property=MemoryMax=200M --property=CPUQuota=50% ./stress-test
```

## Watch the environment difference

This is the part that surprises people migrating from sudo. Compare what each tool hands the child process:

```bash
sudo env | grep -E 'PATH|HOME|TERM|PWD'
# PATH, HOME, PWD, TERM largely carried over (or via secure_path)

run0 env | grep -E 'PATH|HOME|TERM|SUDO'
# a clean, service-manager PATH; TERM copied from caller;
# SUDO_USER / SUDO_UID / SUDO_GID set for you
```

run0 starts from the system service manager's environment and adds only a small, explicit set: `$TERM` copied from the caller, plus `$SUDO_USER`, `$SUDO_UID`, and `$SUDO_GID` for compatibility. It does **not** carry over your `$PATH`, `$HOME`, or arbitrary exported variables. If you need one, pass it explicitly:

```bash
run0 --setenv=DEPLOY_KEY --setenv=RAILS_ENV=production ./deploy.sh
```

## sudo vs run0 at a glance

| | sudo | run0 |
|---|---|---|
| Mechanism | setuid-root binary | request to PID 1 via `systemd-run` |
| Runs privileged code in caller's context | yes | no — clean fork from PID 1 |
| Auth backend | `/etc/sudoers`, PAM | polkit |
| Auth prompt isolation | on the caller's TTY | separate polkit agent |
| Environment | inherited (then scrubbed) | service-manager env + a few extras |
| Visual danger cue | none by default | red/yellow terminal tint |
| Resource limits per call | no | `--property=MemoryMax=…`, etc. |
| Portability | everywhere | needs systemd + polkit |

## Limitations and gotchas

run0 is not a drop-in replacement, and pretending it is will break things:

- **No environment inheritance.** Scripts that assume `sudo` carries `$PATH`, `$HOME`, or custom exports will fail. Use `--setenv=` or `-i` for a login-style environment.
- **It needs systemd and polkit.** No systemd (containers, `chroot`, non-systemd distros, PID-1-less environments), no run0. sudo has no such dependency.
- **Different mental model for rules.** sudoers granularity (`user ALL=(ALL) /usr/bin/systemctl restart nginx`) becomes polkit policy. Powerful, but a different language to learn.
- **The child is a systemd unit,** not a descendant of your shell. Job-control habits and process-tree assumptions change; backgrounded work is tied to the session, not orphaned into a daemon.
- **Ecosystem maturity.** sudo has decades of tooling, muscle memory, and audit integrations. run0 is young by comparison.

The point of run0 isn't that sudo is broken — it's that the *setuid model* forces sudo to solve an impossible problem (safely running root code inside an attacker's environment) that run0 sidesteps by never entering that environment at all.

**Try next:** run `run0 --property=MemoryMax=100M stress-ng --vm 2 --vm-bytes 200M` and watch the transient unit get OOM-killed under a limit you set at the command line — something sudo can't express.
