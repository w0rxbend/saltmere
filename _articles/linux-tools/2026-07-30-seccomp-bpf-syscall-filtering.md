---
title: "seccomp-bpf: Filtering the Syscalls a Process Can Make"
date: 2026-07-30
track: linux-tools
summary: "How seccomp-bpf uses a classic-BPF filter installed through prctl(2) or seccomp(2) to restrict which syscalls a process may issue — the seccomp_data buffer, return actions from ALLOW to KILL_PROCESS, and how Docker and Kubernetes wire it up as their default sandbox."
reading_time: 6
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

**Gist.** Nearly every privileged operation a process performs — opening a file, creating a thread, loading a kernel module — crosses the syscall boundary, so the syscall table is the kernel's exposed attack surface. Secure computing mode with filters (seccomp-bpf) lets a process hand the kernel a classic-BPF program that is evaluated on entry to every syscall and returns an action determining the call's fate. The cost is that the filter is irrevocable, inherited by every descendant, and evaluated with no access to userspace memory, so a filter that is too narrow terminates the process and one that dereferences pointer arguments cannot be written at all.

## Two modes

seccomp exposes two modes, selected through `prctl(2)` or through the `seccomp(2)` syscall directly.

**Strict mode** (`SECCOMP_SET_MODE_STRICT`) is the original 2005 feature. Once enabled, the thread may issue only `read()`, `write()`, `_exit()` and `sigreturn()`; any other syscall raises `SIGKILL`. Its use is confined to executing fully untrusted computation over already-open file descriptors.

**Filter mode** (`SECCOMP_SET_MODE_FILTER`) is the general mechanism. The caller supplies a **classic BPF program (cBPF, the packet-filter language — not eBPF)** that the kernel evaluates on entry to every syscall.

One prerequisite is load-bearing: installing a filter without `CAP_SYS_ADMIN` requires the *no-new-privileges* bit to be set first.

```c
prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);
```

The bit guarantees that the filtered process cannot acquire privileges — for example by executing a setuid binary — and so cannot escape the sandbox by re-entering it as a more privileged identity.

**Filters are one-way and cumulative.** There is no call to remove one. They survive `fork()` and `execve()`, and installing several does not replace the previous filter: **every installed filter runs on every syscall, and the most severe return action wins.** A library that installs a permissive filter therefore cannot widen a restriction imposed earlier by the process; the only achievable direction is narrower.

## What the filter sees

The BPF program does not read the process's registers. It reads a fixed, read-only buffer, `seccomp_data`, laid out as follows.

```c
struct seccomp_data {
    int   nr;                    /* syscall number       */
    __u32 arch;                  /* AUDIT_ARCH_* value    */
    __u64 instruction_pointer;   /* CPU IP at the syscall */
    __u64 args[6];               /* the 6 syscall args    */
};
```

Two consequences follow from this being the entire input. First, **the filter cannot dereference `args[]`**: a pointer argument is visible only as an integer, so a rule such as "permit `openat` only under `/tmp`" is not expressible, and any attempt to resolve the string in a supervisor is subject to the classic time-of-check/time-of-use race, since another thread may rewrite the buffer between the check and the kernel's own read.

Second, **`nr` is meaningless without `arch`**. Syscall number 1 is `write` on x86-64 and `exit` on i386. A filter that matches on `nr` alone can be defeated by a process issuing the same numeric call under a different calling convention, such as the x32 application binary interface (ABI) — a known class of sandbox-escape bug. The `arch` field must be loaded and compared before any comparison against `nr`, with a deny action on the mismatch path.

## Return actions

The filter returns a 32-bit value: **the high 16 bits select the action, the low 16 bits carry data** (an errno value, or a value passed to a tracer). Ordered from most to least severe:

| Action | Effect |
|---|---|
| `SECCOMP_RET_KILL_PROCESS` | Kill the whole process, core dump |
| `SECCOMP_RET_KILL_THREAD` | Kill only the offending thread |
| `SECCOMP_RET_TRAP` | Deliver `SIGSYS` synchronously; syscall not run |
| `SECCOMP_RET_ERRNO` | Skip the syscall, return the errno in the low bits |
| `SECCOMP_RET_USER_NOTIF` | Hand the call to a userspace supervisor to decide |
| `SECCOMP_RET_TRACE` | Notify an attached `ptrace` tracer |
| `SECCOMP_RET_LOG` | Log the syscall, then allow it |
| `SECCOMP_RET_ALLOW` | Run it, no interference |

That ordering is also the precedence rule for stacked filters: the most severe action returned by any filter is the one applied.

`ERRNO` denies without terminating — the caller observes a failure such as `EPERM` and may continue on its error path, which tolerates over-tight profiles far better than an immediate kill. `KILL_PROCESS` denies loudly and leaves a core dump for diagnosis. `USER_NOTIF`, added in Linux 5.0, forwards the call to a supervisor process holding a notification file descriptor, which can inspect the call, emulate it, or reject it.

## A minimal filter by hand

The following program permits every syscall except `execve`, which is converted into `EPERM`.

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

The cBPF jump encoding is the reason larger filters are not written by hand: each `BPF_JUMP` carries **relative** true and false offsets, so inserting a rule silently moves the target of every jump that spans the insertion point. **libseccomp** provides a resolver and assembler over the same interface.

```c
scmp_filter_ctx ctx = seccomp_init(SCMP_ACT_ALLOW);   /* default allow */
seccomp_rule_add(ctx, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(execve), 0);
seccomp_load(ctx);                                    /* compiles + installs BPF */
```

It performs architecture and ABI multiplexing, resolves syscall names to numbers per architecture, and compiles argument-value rules into the corresponding comparisons.

## How containers use it

**Docker** ships a default profile whose `defaultAction` is `SCMP_ACT_ERRNO` — deny by default — with an allowlist of the syscalls ordinary applications require. The Docker documentation describes the profile as blocking around 44 of the 300-odd syscalls, among them `mount`, `kexec_load`, `init_module`, `reboot` and the keyring calls. A per-container override is passed at run time.

```bash
docker run --security-opt seccomp=/path/to/profile.json myimage
```

The profile is JSON.

```json
{
  "defaultAction": "SCMP_ACT_ERRNO",
  "syscalls": [
    { "names": ["read", "write", "openat", "close"], "action": "SCMP_ACT_ALLOW" },
    { "names": ["clock_settime"], "action": "SCMP_ACT_ERRNO", "errnoRet": 1 }
  ]
}
```

**Kubernetes** exposes the same machinery through `securityContext`. The value `RuntimeDefault` instructs the container runtime to apply its own default profile; the alternative `Localhost` names a profile file on the node. **Unconfined remains the pod default unless a profile is requested.**

```yaml
spec:
  securityContext:
    seccompProfile:
      type: RuntimeDefault      # or Localhost, with localhostProfile: my/profile.json
```

The field may be set pod-wide as above or per container. Cluster operators can change the cluster-wide default with the kubelet's `--seccomp-default` flag, so that pods receive `RuntimeDefault` without declaring it.

To derive a profile empirically, run the workload once with `--security-opt seccomp=unconfined` and once under the default profile, and compare the syscall sets recorded by `strace -f -c`.

## Pitfalls

- **Matching `nr` without first checking `arch`.** A process switching to another ABI reaches a different syscall under the number the filter permits; the sandbox is bypassed with no error reported.
- **Expecting to relax a filter later.** Filters cannot be removed and stack with most-severe-wins precedence, so a library installing `SCMP_ACT_ALLOW` after an application's restrictive filter changes nothing.
- **Omitting `PR_SET_NO_NEW_PRIVS`.** `prctl(PR_SET_SECCOMP, ...)` fails for a process without `CAP_SYS_ADMIN`, and the failure is reported by the return value rather than by termination — an unchecked call leaves the process entirely unfiltered.
- **Filtering on pointer arguments.** `seccomp_data.args[]` holds the raw integers; the filter cannot read the buffers they address, and a supervisor that reads them is racing every other thread in the process.
- **Deny-by-default profiles broken by a libc or kernel upgrade.** A new libc release may switch to a newer syscall (an older `open` replaced by `openat`, for instance), and a profile allowlisting only the previous name turns a routine operation into `EPERM` or a kill at start-up.
- **Choosing `KILL_PROCESS` while tuning.** The process dies at the first missing entry with no record of the syscalls it would have made next, whereas `SECCOMP_RET_LOG` or `SCMP_ACT_ERRNO` lets a single run enumerate the whole set.
- **Setting `seccompProfile` on the pod and assuming containers inherit it unconditionally.** A container-level `securityContext` overrides the pod-level profile for that container.
