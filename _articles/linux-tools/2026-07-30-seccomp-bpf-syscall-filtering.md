---
title: "seccomp-bpf: Filtering the Syscalls a Process Can Make"
date: 2026-07-30
track: linux-tools
summary: "How seccomp-bpf uses a classic-BPF filter over prctl/seccomp(2) to restrict which syscalls a process may issue — the seccomp_data buffer, return actions from ALLOW to KILL_PROCESS, and how Docker and Kubernetes wire it up as their default sandbox."
reading_time: 5
tags: [seccomp, bpf, linux, sandboxing, syscalls, containers, kubernetes, security]
sources:
  - title: "seccomp(2) — Linux manual page (man7.org)"
    url: "https://man7.org/linux/man-pages/man2/seccomp.2.html"
  - title: "Seccomp BPF (SECure COMPuting with filters) — kernel.org"
    url: "https://docs.kernel.org/userspace-api/seccomp_filter.html"
  - title: "A seccomp overview (LWN.net)"
    url: "https://lwn.net/Articles/656307/"
  - title: "Seccomp security profiles for Docker"
    url: "https://docs.docker.com/engine/security/seccomp/"
  - title: "Restrict a Container's Syscalls with seccomp (Kubernetes)"
    url: "https://kubernetes.io/docs/tutorials/security/seccomp/"
---

Every interesting thing a process does — open a file, spawn a thread, load a kernel module — is a syscall. The kernel's attack surface *is* the syscall table. **seccomp** ("secure computing") lets a process voluntarily hand the kernel a filter that says which of those ~350 syscalls it's still allowed to make. Get it wrong and the kernel kills you; that's the point.

## Two modes

seccomp has two flavors, selected through `prctl(2)` or the `seccomp(2)` syscall directly.

**Strict mode** (`SECCOMP_SET_MODE_STRICT`) is the original 2005 feature: once enabled, the thread may only call `read()`, `write()`, `_exit()`, and `sigreturn()`. Anything else earns a `SIGKILL`. It's a straitjacket for running fully untrusted bytecode and little else.

**Filter mode** (`SECCOMP_SET_MODE_FILTER`) is what everyone actually uses. You supply a **classic BPF** program (cBPF, the packet-filter language — *not* eBPF) that the kernel runs on entry to every syscall. The program returns an action that decides the syscall's fate.

One hard prerequisite: to install a filter without `CAP_SYS_ADMIN`, you must first set the *no-new-privs* bit:

```c
prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);
```

This guarantees a filtered process can't gain privileges (e.g. via a setuid binary) to escape the sandbox. Filters are one-way and inherited: they survive `fork()`/`execve()` and stack — install several and *all* run, most-restrictive action wins.

## What the filter sees

The BPF program doesn't get the process's registers directly. It reads a small, fixed, read-only struct — `seccomp_data`:

```c
struct seccomp_data {
    int   nr;                    /* syscall number       */
    __u32 arch;                  /* AUDIT_ARCH_* value    */
    __u64 instruction_pointer;   /* CPU IP at the syscall */
    __u64 args[6];               /* the 6 syscall args    */
};
```

Two fields matter most. `nr` is the syscall number — but numbers are meaningless without `arch`, because syscall 1 is `write` on x86-64 and `exit` on i386. **Always check `arch` first.** A filter that keys on `nr` alone can be defeated by a process flipping to a different calling convention (e.g. the x32 ABI), and it's a classic sandbox-escape bug.

## Return actions

The filter returns a 32-bit value; the high 16 bits are the action, the low 16 an optional data field (an errno, or a trace value). From most to least severe:

| Action | Effect |
|---|---|
| `SECCOMP_RET_KILL_PROCESS` | Kill the whole process, core dump |
| `SECCOMP_RET_KILL_THREAD` | Kill just the offending thread |
| `SECCOMP_RET_TRAP` | Deliver `SIGSYS` synchronously; syscall not run |
| `SECCOMP_RET_ERRNO` | Skip the syscall, return the errno in the low bits |
| `SECCOMP_RET_USER_NOTIF` | Hand the call to a userspace supervisor to decide |
| `SECCOMP_RET_TRACE` | Notify an attached `ptrace` tracer |
| `SECCOMP_RET_LOG` | Log the syscall, then allow it |
| `SECCOMP_RET_ALLOW` | Run it, no interference |

`ERRNO` is the polite deny — the program gets `EPERM` and often keeps running, which is far kinder to buggy code than an instant kill. `KILL_PROCESS` is the loud deny. `USER_NOTIF` (added in 5.0) is the powerful one: it forwards the syscall to a supervisor process holding a notification fd, which can inspect, emulate, or reject it — the mechanism behind things like rootless-container fd interception.

## A minimal filter by hand

Writing raw cBPF: allow everything except `execve`, which we turn into `EPERM`.

```c
#include <linux/seccomp.h>
#include <linux/filter.h>
#include <linux/audit.h>
#include <sys/prctl.h>
#include <errno.h>

struct sock_filter code[] = {
    /* load arch, verify x86-64 or kill */
    BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_X86_64, 1, 0),
    BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),

    /* load syscall nr */
    BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_execve, 0, 1),
    BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | (EPERM & SECCOMP_RET_DATA)),

    BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
};
struct sock_fprog prog = { .len = sizeof(code)/sizeof(code[0]), .filter = code };

prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);
prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &prog);
```

Nobody sane writes big filters this way. **libseccomp** gives you a resolver-and-assembler over it:

```c
scmp_filter_ctx ctx = seccomp_init(SCMP_ACT_ALLOW);   /* default allow */
seccomp_rule_add(ctx, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(execve), 0);
seccomp_load(ctx);                                    /* compiles + installs BPF */
```

It handles arch/ABI multiplexing, name-to-number resolution, and even argument-value rules for you.

## How containers use it

This is where seccomp earns its keep. **Docker** ships a default profile with `defaultAction: SCMP_ACT_ERRNO` — deny-by-default — then allowlists the syscalls normal apps need, blocking roughly 40+ dangerous ones (`mount`, `kexec_load`, `init_module`, `reboot`, keyring calls). Override it per container:

```bash
docker run --security-opt seccomp=/path/to/profile.json myimage
```

A profile is just JSON:

```json
{
  "defaultAction": "SCMP_ACT_ERRNO",
  "syscalls": [
    { "names": ["read", "write", "openat", "close"], "action": "SCMP_ACT_ALLOW" },
    { "names": ["clock_settime"], "action": "SCMP_ACT_ERRNO", "errnoRet": 1 }
  ]
}
```

**Kubernetes** exposes the same machinery through `securityContext`. The important value is `RuntimeDefault`, which tells the container runtime to apply *its* default profile (containerd's/Docker's) rather than running unconfined — which is still the pod default unless you ask:

```yaml
spec:
  securityContext:
    seccompProfile:
      type: RuntimeDefault      # or Localhost, with localhostProfile: my/profile.json
```

Set it pod-wide as above, or per-container. Cluster operators can flip the default for every workload with the kubelet's `--seccomp-default` flag so pods get `RuntimeDefault` without opting in. It's one of the cheapest, highest-leverage hardening steps you can apply to a cluster.

**Try next:** Run `docker run --rm --security-opt seccomp=unconfined alpine ...` versus the default and use `strace -f -c` to diff which syscalls your workload actually touches, then hand-write a tight allowlist profile from that trace.
