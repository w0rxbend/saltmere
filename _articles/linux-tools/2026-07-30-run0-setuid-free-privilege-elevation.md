---
title: "run0: privilege elevation without the setuid bit"
date: 2026-07-30
track: linux-tools
summary: "sudo has been setuid-root for decades, which means privileged code executes inside an environment the unprivileged caller controls. systemd's run0 inverts the model: it asks PID 1 to spawn a fresh root process instead. This article covers the mechanism, the environment it produces, and what the change costs."
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

**Gist.** A set-user-ID (setuid) binary such as `sudo` is started by the kernel with an effective user ID of root while every inherited resource — environment variables, file descriptors, resource limits, controlling terminal, current working directory — was chosen by the unprivileged caller, so the program must scrub a hostile context before it can be trusted. systemd's `run0`, shipped in systemd 256 (June 2024), removes that requirement: it carries no setuid bit and instead asks PID 1, which is already root, to spawn the requested command as a fresh transient unit born from the service manager's own context. The cost is that the elevated process is no longer a child of the caller's shell and inherits almost nothing from it, so scripts written against sudo's inheritance semantics break.

## The structural defect in the setuid model

The permission bits on sudo (`-rwsr-xr-x`) place the `s` in the owner-execute position. On execution the kernel sets the effective user ID to the file owner, root, but performs no cleanup of what the caller supplied. The privileged program therefore begins life holding attacker-chosen state along at least four axes:

- **Dynamic-linker variables** — `LD_PRELOAD`, `LD_LIBRARY_PATH` — plus `PATH` and `IFS`, which steer which code is loaded and which binary a name resolves to.
- **Inherited file descriptors and `RLIMIT_*` values**, which fix what the privileged process can write to and how much of a resource it may consume before failing.
- **The controlling terminal**, historically abusable through `TIOCSTI` input injection.
- **The current working directory**, chosen by the caller.

Each of these has served as an exploit primitive. The defence is manual and exhaustive: sudo devotes substantial code to sanitising the inherited environment, and a single omission is sufficient. **Baron Samedit (CVE-2021-3156) was a heap overflow in sudo reachable by any local user**, privileged or not. The announcement's framing, from Lennart Poettering, is that an execution context for privileged code "half under the control of unprivileged code and that needs careful manual clean-up" is not how security engineering should be done.

## The delegation mechanism

`run0` is a symbolic link to `systemd-run` and changes behaviour according to the name under which it is invoked. It **never attempts to raise its own privilege**. The sequence is:

1. The unprivileged `run0` process sends a request over D-Bus to **PID 1**, the system service manager, which is already running as root.
2. PID 1 spawns the requested command as a **fresh transient unit**, forked from the service manager's context rather than from the caller's process.
3. `run0` allocates a new **pseudo-terminal (PTY)**, attaches it to the caller's real terminal, and relays bytes between the two.

The invariant this establishes is that **no privileged code ever executes inside a process image the caller constructed**. A malicious `LD_PRELOAD` has nothing to hook, because the linker in the privileged process read the service manager's environment, not the caller's. The relay in step 3 means the caller's terminal is on the far side of a PTY boundary rather than being the elevated process's controlling terminal.

Authorisation is delegated to **polkit**, not to `/etc/sudoers` and not to logic inside a setuid binary. The decision is made by the polkit service, which runs as a separate process, and the credential is collected by a polkit authentication agent rather than by the elevating binary itself. Whether that agent is out of reach of a hostile terminal depends on which agent is in use: a graphical agent prompts outside the terminal, while the text agent still reads from it.

The visible signal is a terminal background tint applied by default: **reddish when the target identity is root, yellowish for any other user ID**. It is reverted when the session ends.

## Invocation surface

A single command as root:

```bash
run0 systemctl restart nginx
run0 nano /etc/hosts
```

With no command, an interactive root shell is started, with the corresponding tint:

```bash
run0
```

A different target identity is selected with `-u`/`--user` and `-g`/`--group`:

```bash
run0 -u postgres psql
run0 --user=www-data -g www-data ./deploy.sh
```

The tint is controlled by `--background`; an empty value disables it:

```bash
run0 --background='' systemctl status        # no colour tint
run0 --nice=10 --working-directory=/srv/app ./batch-job
```

Because each invocation is a transient systemd unit, unit properties — including resource controls — can be attached per call through `--property`. There is no direct `sudo` equivalent:

```bash
run0 --property=MemoryMax=200M --property=CPUQuota=50% ./stress-test
```

## The environment handed to the child

This is the behavioural difference most likely to surface during migration. The two tools can be compared directly:

```bash
sudo env | grep -E 'PATH|HOME|TERM|PWD'
# PATH, HOME, PWD, TERM largely carried over (or supplied via secure_path)

run0 env | grep -E 'PATH|HOME|TERM|SUDO'
# a clean, service-manager PATH; TERM copied from the caller;
# SUDO_USER / SUDO_UID / SUDO_GID set for compatibility
```

`run0` starts from the system service manager's environment and adds only a small, explicit set: **`TERM` copied from the caller, plus `SUDO_USER`, `SUDO_UID` and `SUDO_GID`** for compatibility with existing scripts. It does **not** propagate the caller's `PATH`, `HOME`, or arbitrary exported variables. Anything required must be passed explicitly:

```bash
run0 --setenv=DEPLOY_KEY --setenv=RAILS_ENV=production ./deploy.sh
```

## Comparison

| | sudo | run0 |
|---|---|---|
| Mechanism | setuid-root binary | request to PID 1 via `systemd-run` |
| Runs privileged code in caller's context | yes | no — fresh fork from PID 1 |
| Auth backend | `/etc/sudoers`, PAM | polkit |
| Auth prompt isolation | on the caller's TTY | separate polkit agent |
| Environment | inherited, then scrubbed | service-manager env plus a few variables |
| Visual danger cue | none by default | red/yellow terminal tint |
| Resource limits per call | no | `--property=MemoryMax=…`, etc. |
| Portability | everywhere | requires systemd and polkit |

`run0` has been present since systemd 256; systemd 258 followed in 2025, and the tool has been carried forward in subsequent releases.

The claim is not that sudo is defective. It is that the setuid model obliges sudo to solve a problem with no complete solution — executing root code safely inside an environment supplied by an attacker — which `run0` avoids by never entering that environment.

## Pitfalls

- **A script that relies on `sudo` forwarding `PATH`, `HOME` or a custom export fails under `run0` with a command-not-found or a wrong-file error**, because the child receives the service manager's environment; each required variable must be named explicitly with `--setenv=`.
- **`run0` is unavailable wherever systemd or polkit is absent** — many containers, `chroot` environments, non-systemd distributions, and any context without a systemd PID 1 — because the elevation is a D-Bus request to PID 1 rather than a kernel privilege transition. sudo has no such dependency.
- **A sudoers rule such as `user ALL=(ALL) /usr/bin/systemctl restart nginx` has no direct translation**; authorisation must be re-expressed as polkit policy, a separate rule language with separate semantics.
- **Job-control habits misfire because the elevated command is a transient unit, not a descendant of the invoking shell**; process-tree assumptions and signal delivery through the shell do not hold, because the privileged process is a child of PID 1 reached over a PTY rather than a child of the shell.
- **Audit and tooling integrations built around sudo do not carry over**, since the privileged execution is attributed to a systemd unit rather than to a setuid invocation.
