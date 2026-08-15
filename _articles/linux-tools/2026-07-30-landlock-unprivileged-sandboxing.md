---
title: "Landlock: sandboxing a process's filesystem access without root"
date: 2026-07-30
track: linux-tools
summary: "Landlock allows an ordinary, unprivileged program to restrict itself — read only /etc, write only /tmp — with the restriction enforced by the kernel and inherited by every child. This article covers the three-syscall model, the no_new_privs requirement, the ABI-versioning check, and a C program that sandboxes itself before performing risky work."
reading_time: 6
tags: [landlock, lsm, sandboxing, security, seccomp, syscalls]
sources:
  - title: "landlock(7) — Linux manual page"
    url: "https://man7.org/linux/man-pages/man7/landlock.7.html"
  - title: "Landlock: unprivileged access control — The Linux Kernel documentation"
    url: "https://docs.kernel.org/userspace-api/landlock.html"
  - title: "Landlock: Unprivileged Sandboxing (project site)"
    url: "https://landlock.io/"
  - title: "Landlock LSM: kernel documentation"
    url: "https://docs.kernel.org/security/landlock.html"
---

**Gist.** Conventional Linux sandboxing requires privilege to give away: a root-owned supervisor builds a chroot, a mount namespace, or an AppArmor profile *for* a less-trusted process. Landlock, a Linux Security Module (LSM) merged in kernel **5.13**, inverts the direction — an **unprivileged** process declares which filesystem (and, from a later ABI, network) accesses it will permit itself, and the kernel enforces that declaration on the calling thread and every descendant. The cost is irreversibility and a version-negotiation burden: a ruleset cannot be relaxed once applied, and the set of enforceable access rights depends on the running kernel's Landlock **application binary interface (ABI)** version, which the program must query rather than assume.

## The model: handle, allow, enforce

A Landlock ruleset operates by *subtraction from a named set*. The program does not enumerate everything it may do. It names the categories of access the kernel should **handle** — begin mediating — then adds **rules** granting specific paths within those categories. **Any access that is handled but not granted is denied; any access category never handled is untouched by Landlock and remains governed by the ordinary permission checks, discretionary access control (DAC) among them.** This is the single most load-bearing property of the model: forgetting a right in `handled_access_fs` does not fail closed, it leaves that right entirely unrestricted.

Three syscalls, in order:

1. **`landlock_create_ruleset()`** — declares the `handled_access_fs` bitmask (for example, read, write and execute on files). Returns a ruleset file descriptor.
2. **`landlock_add_rule()`** — for each path to be allowed, supplies a rule of the form *this directory file descriptor, these permitted accesses*. Called once per allowed path. The path-beneath rule type grants the listed accesses to the directory and everything under it.
3. **`landlock_restrict_self()`** — enforces the ruleset on the calling thread. **Irreversible.** From this point, every handled access outside the granted paths returns `EACCES`, and the restriction is inherited across `fork()` and `execve()`.

Before step 3, an unprivileged process must set **`no_new_privs`** via `prctl(PR_SET_NO_NEW_PRIVS, 1, …)`. This is the flag seccomp also requires; it guarantees the process cannot gain privileges through a set-user-ID binary. Without `CAP_SYS_ADMIN`, **`landlock_restrict_self()` fails unless `no_new_privs` is already set** — the ordering is part of the interface, not a convention.

Rulesets compose by intersection rather than replacement. **Calling `landlock_restrict_self()` a second time layers a further ruleset on top of the existing one; the effective policy is the conjunction, so each layer can only narrow access.** A child process therefore cannot widen what its parent granted, which is what makes the restriction safe to inherit.

## ABI versioning: query, do not assume

Landlock's enforceable rights grow per kernel release, so a binary compiled against a recent `linux/landlock.h` may run on a kernel that does not recognise the rights it names. The supported ABI version is obtained from the same creation syscall with a dedicated flag:

```c
int abi = landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION);
```

The progression of rights against the first kernel that shipped each:

| ABI | Kernel | Added |
|-----|--------|-------|
| 1 | 5.13 | Core filesystem rights (read/write/execute/read-dir/make-*/remove-*) |
| 2 | 5.19 | `LANDLOCK_ACCESS_FS_REFER` (link/rename across directories) |
| 3 | 6.2  | `LANDLOCK_ACCESS_FS_TRUNCATE` |
| 4 | 6.7  | Network rules: `BIND_TCP`, `CONNECT_TCP` |
| 5 | 6.10 | `LANDLOCK_ACCESS_FS_IOCTL_DEV` (ioctls on device files) |
| 6 | 6.12 | Scoping: abstract UNIX sockets and signals |

The required discipline is to **mask `handled_access_fs` down to the rights the queried ABI supports**, and then to make an explicit policy decision about the gap: fail closed and refuse to run, or degrade and record which rights are unenforced. The failure to avoid is silent: a program that names a right the kernel does not know, does not check the ABI, and proceeds believing the sandbox covers accesses that are in fact unmediated.

`LANDLOCK_ACCESS_FS_REFER` (ABI 2) deserves separate attention because it changes the default. Under ABI 1 there is no way to express a link or rename across directory boundaries, and such operations are refused whenever the ruleset is in force. Under ABI 2 and later, **`REFER` must be in the handled set and granted on both the source and destination hierarchies for a cross-directory rename or hard link to succeed** — granting it on one side only still yields `EXDEV`.

## A self-sandboxing program

The following process restricts itself to reading `/usr` and `/etc` and writing under `/tmp`, then demonstrates the effect. The handled set uses ABI-1 core rights, so it is enforceable on any kernel from 5.13 onward.

```c
#define _GNU_SOURCE
#include <linux/landlock.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <fcntl.h>
#include <stdio.h>
#include <unistd.h>

static int add_dir(int rs, const char *path, __u64 allowed) {
    struct landlock_path_beneath_attr pb = { .allowed_access = allowed };
    pb.parent_fd = open(path, O_PATH | O_CLOEXEC);   /* O_PATH: no read permission needed */
    if (pb.parent_fd < 0) return -1;
    int r = syscall(SYS_landlock_add_rule, rs,
                    LANDLOCK_RULE_PATH_BENEATH, &pb, 0);
    close(pb.parent_fd);
    return r;
}

int main(void) {
    int abi = syscall(SYS_landlock_create_ruleset, NULL, 0,
                      LANDLOCK_CREATE_RULESET_VERSION);
    if (abi < 1) return 1;                            /* Landlock absent or disabled */

    struct landlock_ruleset_attr ra = {
        .handled_access_fs =
            LANDLOCK_ACCESS_FS_READ_FILE  | LANDLOCK_ACCESS_FS_READ_DIR |
            LANDLOCK_ACCESS_FS_WRITE_FILE | LANDLOCK_ACCESS_FS_EXECUTE  |
            LANDLOCK_ACCESS_FS_MAKE_REG,
    };
    int rs = syscall(SYS_landlock_create_ruleset, &ra, sizeof(ra), 0);

    /* Grant: read /usr and /etc, read+write+create under /tmp. */
    add_dir(rs, "/usr", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR |
                        LANDLOCK_ACCESS_FS_EXECUTE);
    add_dir(rs, "/etc", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR);
    add_dir(rs, "/tmp", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_WRITE_FILE |
                        LANDLOCK_ACCESS_FS_READ_DIR  | LANDLOCK_ACCESS_FS_MAKE_REG);

    prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);           /* required for unprivileged use */
    syscall(SYS_landlock_restrict_self, rs, 0);       /* irreversible from here */
    close(rs);

    printf("/tmp/ok     : %s\n", fopen("/tmp/landlock_ok", "w") ? "allowed" : "DENIED");
    printf("/root/secret: %s\n", fopen("/root/secret", "w")     ? "allowed" : "DENIED");
    return 0;
}
```

Built with a `linux/landlock.h` that defines the referenced constants (`gcc sandbox.c -o sandbox`) and run as an ordinary user, the write under `/tmp` succeeds and the write outside the granted hierarchies fails with `EACCES` — even where the process's user ID would otherwise permit it. Closing the ruleset descriptor after `landlock_restrict_self()` does not lift the restriction; **the descriptor is a handle used to build and apply the policy, not the policy's lifetime.**

## Where it fits

Landlock applies when an application narrows its own reachable surface before parsing untrusted input, loading a plugin, or invoking a subprocess: a transcoder confined to its input and output directories, a build step barred from TCP `bind()` and `connect()` under ABI 4, a runtime executing user-supplied scripts. It composes with **seccomp**, which mediates syscalls rather than paths, and with **namespaces**, which change what is visible rather than what is permitted; none of the three requires privileged setup for the unprivileged case.

Its limits follow from its shape. Landlock expresses path- and access-oriented rules, not the full mandatory access control (MAC) policy language of SELinux; and older kernels enforce a smaller right set, which is why the ABI query is a correctness requirement rather than a nicety.

## Pitfalls

- **A right omitted from `handled_access_fs` is not restricted at all.** Handling read and write but not `EXECUTE` leaves the sandboxed process free to execute binaries anywhere the DAC permits — the ruleset denies nothing it was never told to mediate.
- **Naming a right the running kernel does not support without checking the ABI leaves that access unmediated.** The program believes it is sandboxed while the kernel is enforcing only the subset it recognises; the symptom is an access that succeeds in production on an older kernel and is denied on the developer's newer one.
- **`landlock_restrict_self()` fails for an unprivileged caller when `no_new_privs` was not set first.** The syscall returns an error and, if the return value is ignored, the process continues completely unsandboxed.
- **`landlock_restrict_self()` cannot be undone or loosened.** A later ruleset intersects with the current one, so a program that sandboxes itself too early cannot re-open a path it discovers it needs; the symptom is `EACCES` on a configuration file read after initialisation.
- **Cross-directory rename and hard link require `LANDLOCK_ACCESS_FS_REFER` on both hierarchies.** Granting it on the source only produces `EXDEV` from `rename()`, which callers frequently misread as a filesystem-boundary error and respond to with a copy-and-delete fallback that then fails on the delete.
- **Rules are added by directory file descriptor, so a path that cannot be opened yields no rule.** If `open(path, O_PATH)` fails because the directory does not exist yet, the grant is silently missing and every later access to that hierarchy is denied.
- **The restriction is inherited across `execve()`.** A helper binary spawned after enforcement runs under the parent's ruleset, so a subprocess that needs paths the parent never granted fails with `EACCES` and no diagnostic pointing at Landlock.
