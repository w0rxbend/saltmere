---
title: "Portable eBPF with libbpf and CO-RE: compile once, run everywhere"
date: 2026-07-30
track: linux-tools
summary: "A struct field's offset can move between kernel versions, which used to mean recompiling your eBPF program on every target host. CO-RE fixes that: compile one .bpf.o, ship it, and libbpf patches the field offsets at load time using the kernel's own BTF. Here's the full workflow — vmlinux.h, BPF_CORE_READ, the bpftool-generated skeleton, and the build commands."
reading_time: 6
tags: [ebpf, bpf, libbpf, co-re, btf, bpftool, linux-tools]
sources:
  - title: "BPF CO-RE reference guide — Andrii Nakryiko"
    url: "https://nakryiko.com/posts/bpf-core-reference-guide/"
  - title: "Building BPF applications with libbpf-bootstrap — Andrii Nakryiko"
    url: "https://nakryiko.com/posts/libbpf-bootstrap/"
  - title: "libbpf/libbpf-bootstrap: Scaffolding for BPF application development"
    url: "https://github.com/libbpf/libbpf-bootstrap"
  - title: "BPF CO-RE — eBPF Docs"
    url: "https://docs.ebpf.io/concepts/core/"
  - title: "BPF Portability and CO-RE — BPF blog"
    url: "https://facebookmicrosites.github.io/bpf/blog/2020/02/19/bpf-portability-and-co-re.html"
---

An eBPF program that reads kernel memory has a problem the moment it leaves your machine. Say you read `task->mm->exe_file`. The byte offset of `mm` inside `struct task_struct` is baked into the compiled instructions — but that offset is not stable. Enable a config option, bump a point release, switch distros, and a field shifts. The program that worked on your 6.6 laptop reads garbage, or fails the verifier, on the 5.15 box in production.

The old answer (BCC) was to ship the Clang/LLVM toolchain *and* kernel headers to every target and recompile on the fly — slow, heavy, and it broke on any host without matching headers. **CO-RE (Compile Once – Run Everywhere)** replaces that with: compile a single `.bpf.o`, and let `libbpf` fix up the offsets when it loads the program on each host. Same object file, any kernel.

## BTF is what makes it possible

The trick needs one thing at runtime: a machine-readable description of the *target* kernel's types. That's **BTF (BPF Type Format)** — a compact type-info blob. A kernel built with `CONFIG_DEBUG_INFO_BTF=y` embeds its own BTF and exposes it at:

```
/sys/kernel/btf/vmlinux
```

Check for it before anything else:

```bash
ls -l /sys/kernel/btf/vmlinux          # exists => the kernel ships BTF
bpftool btf dump file /sys/kernel/btf/vmlinux format c | head
```

Most modern distro kernels (recent Ubuntu, Fedora, RHEL 9, Debian, Arch) enable this. If it's missing you either rebuild the kernel with the option or pull a matching BTF from the [BTFHub](https://github.com/aquasecurity/btfhub) archive and load it as an external BTF. With BTF present, `libbpf` knows the real layout of every struct on the running kernel — the raw material for relocation.

## Generate vmlinux.h

Your BPF C code needs kernel type definitions to compile against. Instead of chasing kernel-header packages, dump every type the running kernel knows about into a single header:

```bash
bpftool btf dump file /sys/kernel/btf/vmlinux format c > vmlinux.h
```

This header defines `struct task_struct`, `struct file`, and thousands more, each tagged with `__attribute__((preserve_access_index))`. That attribute is the compiler hook: it tells Clang to emit a **CO-RE relocation record** for every field access instead of a hard-coded offset. You commit `vmlinux.h` to your repo; it just supplies type *shapes*, not the offsets — those get resolved per host.

## A minimal CO-RE program

`exec_snoop.bpf.c` — hook the exec tracepoint, read a couple of `task_struct` fields through CO-RE, and push an event to userspace:

```c
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_core_read.h>

char LICENSE[] SEC("license") = "Dual BSD/GPL";

struct {
    __uint(type, BPF_MAP_TYPE_PERF_EVENT_ARRAY);
    __uint(key_size, sizeof(u32));
    __uint(value_size, sizeof(u32));
} events SEC(".maps");

struct event { int pid; int ppid; char comm[16]; };

SEC("tp/sched/sched_process_exec")
int handle_exec(void *ctx)
{
    struct task_struct *task = (struct task_struct *)bpf_get_current_task();
    struct event e = {};

    e.pid = bpf_get_current_pid_tgid() >> 32;

    /* Each field offset here becomes a CO-RE relocation, not a fixed number */
    e.ppid = BPF_CORE_READ(task, real_parent, tgid);

    bpf_get_current_comm(&e.comm, sizeof(e.comm));
    bpf_perf_event_output(ctx, &events, BPF_F_CURRENT_CPU, &e, sizeof(e));
    return 0;
}
```

The key line is `BPF_CORE_READ(task, real_parent, tgid)`. It expands to a chain of `bpf_core_read()` calls — one safe kernel read per `->` hop — and, crucially, records a relocation for `task_struct.real_parent` and for `task_struct.tgid`. Never dereference kernel pointers with a bare `task->real_parent->tgid`; that reads a literal offset with no relocation and no safe-read wrapper. Always go through `BPF_CORE_READ` (or `bpf_core_read`) for kernel memory.

## How a relocation actually runs on a different kernel

Here's the whole idea in one sentence: **Clang records "the offset of `tgid` within `task_struct`" as a symbolic relocation, and `libbpf` resolves it against the target kernel's BTF just before loading.**

Concretely:

1. `preserve_access_index` makes Clang emit, alongside each field access, a record like *"struct task_struct, member `tgid`"* — described by type, not by number.
2. At load time `libbpf` reads `/sys/kernel/btf/vmlinux`, finds `struct task_struct`, and computes the real byte offset of `tgid` *on this kernel*.
3. It patches that offset directly into the BPF instructions, then hands the program to the verifier.

So on kernel A where `tgid` sits at offset 1232, the loaded instruction uses 1232; on kernel B where it moved to 1288, the *same shipped `.bpf.o`* is patched to 1288. Nothing recompiled. CO-RE also handles fields that were renamed, relocated, or that don't exist at all — `bpf_core_field_exists()` and `bpf_core_type_exists()` let you branch on kernel capabilities and degrade gracefully instead of failing to load. (Note: only the field *offset* is relocated automatically — a field's *size* is not, so read sizes deliberately.)

## Build it and generate the skeleton

```bash
# 1. BPF object. -g is mandatory: it emits the BTF that CO-RE relies on.
clang -g -O2 -target bpf -D__TARGET_ARCH_x86 \
      -c exec_snoop.bpf.c -o exec_snoop.bpf.o
llvm-strip -g exec_snoop.bpf.o          # drop DWARF; keep BTF

# 2. Skeleton: a C header with the object bytes + typed accessors baked in
bpftool gen skeleton exec_snoop.bpf.o > exec_snoop.skel.h
```

The generated `exec_snoop.skel.h` embeds the compiled object and gives you typed open/load/attach/destroy functions and handles for every map, program, and global variable — no separate `.o` file to deploy.

## The user-space loader

```c
#include "exec_snoop.skel.h"

int main(void)
{
    struct exec_snoop_bpf *skel;
    int err;

    skel = exec_snoop_bpf__open();          /* parse embedded object   */
    if (!skel) return 1;

    err = exec_snoop_bpf__load(skel);       /* CO-RE relocate + verify */
    if (err) goto cleanup;

    err = exec_snoop_bpf__attach(skel);     /* hook the tracepoint     */
    if (err) goto cleanup;

    /* ... poll skel->maps.events via perf_buffer__poll() ... */

cleanup:
    exec_snoop_bpf__destroy(skel);
    return err;
}
```

`__load()` is where the relocation described above happens — it reads the target kernel's BTF, patches offsets, and only then submits to the verifier. Compile and link against libbpf:

```bash
clang -c user_loader.c -o user_loader.o
clang user_loader.o -lbpf -lelf -lz -o exec_snoop
sudo ./exec_snoop
```

Copy that one `exec_snoop` binary to a host running a completely different kernel — as long as it exposes `/sys/kernel/btf/vmlinux`, it loads and runs unchanged. That's the payoff: no toolchain on the target, no kernel headers, no per-host recompile.

Don't wire the Makefile by hand for real projects — clone [libbpf/libbpf-bootstrap](https://github.com/libbpf/libbpf-bootstrap), which vendors libbpf and bpftool as submodules and gives you the full `vmlinux.h → clang → skeleton → link` pipeline already working.

**Try next:** clone `libbpf-bootstrap`, build the `examples/c` tree, and run `bootstrap`; then edit `bootstrap.bpf.c` to add one more `BPF_CORE_READ` field (e.g. `BPF_CORE_READ(task, group_leader, pid)`), rebuild only the BPF object, and confirm the same binary still loads on a VM running a different kernel version.
