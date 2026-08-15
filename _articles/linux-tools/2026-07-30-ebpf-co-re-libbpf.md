---
title: "Portable eBPF with libbpf and CO-RE: compile once, run everywhere"
date: 2026-07-30
track: linux-tools
summary: "A struct field's offset can move between kernel versions, which previously forced recompilation of an eBPF program on every target host. CO-RE removes that requirement: one .bpf.o is compiled, shipped, and patched at load time by libbpf using the target kernel's own BTF. This article covers the full workflow — vmlinux.h, BPF_CORE_READ, the bpftool-generated skeleton, and the build commands."
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

**Gist.** An eBPF program that dereferences kernel structures compiles field accesses into fixed byte offsets, and those offsets differ between kernel configurations and releases, so an object file built on one host reads the wrong bytes on another. **CO-RE (Compile Once – Run Everywhere)** makes Clang emit each field access as a *symbolic* relocation record, which `libbpf` resolves against the target kernel's own type information immediately before the program is submitted to the verifier. The cost is a hard runtime dependency on that type information being present on every target host, plus a relocation step that can fail at load time rather than at build time.

## The portability failure being solved

A read of `task->mm->exe_file` shows the shape of the problem. The byte offset of `mm` within `struct task_struct` is encoded directly in the compiled BPF instructions. That offset is not part of any stable interface: enabling a kernel configuration option, moving to another point release, or switching distributions can shift it. The resulting failure is not a clean error. If the offset now lands inside a different member, the program reads a valid but wrong value; if it lands outside the object the verifier rejects the load. **The wrong-value case is the dangerous one, because the program loads and runs.**

The predecessor approach, BCC, avoided this by shipping the Clang/LLVM toolchain and kernel headers to every target and compiling on that host at runtime. That guarantees correct offsets by construction, at the price of toolchain size, compilation latency on every start, and outright failure on hosts whose kernel headers are absent or mismatched.

## BTF supplies the target layout

CO-RE requires one thing at runtime: a machine-readable description of the *target* kernel's types. That is **BTF (BPF Type Format)**, a compact type-information blob. A kernel built with `CONFIG_DEBUG_INFO_BTF=y` embeds its own BTF and exposes it at a fixed path:

```
/sys/kernel/btf/vmlinux
```

Its presence is the precondition for everything that follows:

```bash
ls -l /sys/kernel/btf/vmlinux          # exists => the kernel ships BTF
bpftool btf dump file /sys/kernel/btf/vmlinux format c | head
```

Mainstream distribution kernels — recent Ubuntu, Fedora, RHEL 9, Debian and Arch among them — enable the option, but older and custom-built kernels frequently do not. Where it is absent, the two options are rebuilding the kernel with `CONFIG_DEBUG_INFO_BTF=y`, or obtaining a matching BTF blob from the [BTFHub](https://github.com/aquasecurity/btfhub) archive and supplying it to `libbpf` as an external BTF. With BTF available, `libbpf` knows the true layout of every struct on the running kernel, which is the raw material for relocation.

## vmlinux.h supplies the build-time shapes

BPF C source needs kernel type declarations to compile against. Rather than depending on kernel-header packages, every type the running kernel describes can be dumped into a single header:

```bash
bpftool btf dump file /sys/kernel/btf/vmlinux format c > vmlinux.h
```

The generated header declares `struct task_struct`, `struct file` and thousands of other types, wrapped in a `#pragma clang attribute` block that applies `__attribute__((preserve_access_index))` to every record type in the header. **That attribute is the compiler hook: it directs Clang to emit a CO-RE relocation record for every field access on the type instead of materialising a constant offset.** The header therefore contributes type *shapes* only — member names and nesting — and not the offsets, which are resolved per host. It is a normal build input and can be committed to the repository.

## A minimal CO-RE program

`exec_snoop.bpf.c` attaches to the exec tracepoint, reads two `task_struct` fields through CO-RE, and forwards an event to user space:

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

The load-bearing line is `BPF_CORE_READ(task, real_parent, tgid)`. It expands to a chain of `bpf_core_read()` calls — **one safe kernel read per `->` hop** — and records a relocation for `task_struct.real_parent` and for `task_struct.tgid`. A bare `task->real_parent->tgid` compiles to a literal offset with neither a relocation record nor a safe-read wrapper, and is therefore both unportable and an unchecked kernel dereference.

## What the relocation does at load time

The mechanism reduces to one statement: **Clang records "the offset of `tgid` within `task_struct`" symbolically, and `libbpf` resolves that record against the target kernel's BTF immediately before the load.**

The sequence is:

1. `preserve_access_index` causes Clang to emit, alongside each field access, a record naming the type and the member — *"struct task_struct, member `tgid`"* — rather than a number.
2. At load time `libbpf` reads `/sys/kernel/btf/vmlinux`, locates `struct task_struct`, and computes the byte offset of `tgid` **on that kernel**.
3. It patches the computed offset into the BPF instruction stream, and only then submits the program to the verifier.

Consequently, if `tgid` sits at one offset on kernel A and at a different offset on kernel B, the *same shipped `.bpf.o`* is patched differently on each, with no recompilation. A field that has merely moved within its struct is therefore handled transparently. A field that has been renamed or removed is not: the relocation names the member, so it no longer matches. For those cases `bpf_core_field_exists()` and `bpf_core_type_exists()` let the program test the target kernel's types and branch, degrading rather than failing to load. **The width of the generated read still comes from the type as declared at compile time**, so a member that changed size on the target is read at the old width unless the program consults `bpf_core_field_size()` and handles the difference itself.

## Build and skeleton generation

```bash
# 1. BPF object. -g is mandatory: it emits the BTF that CO-RE relies on.
clang -g -O2 -target bpf -D__TARGET_ARCH_x86 \
      -c exec_snoop.bpf.c -o exec_snoop.bpf.o
llvm-strip -g exec_snoop.bpf.o          # drop DWARF; keep BTF

# 2. Skeleton: a C header with the object bytes + typed accessors baked in
bpftool gen skeleton exec_snoop.bpf.o > exec_snoop.skel.h
```

The generated `exec_snoop.skel.h` embeds the compiled object and exposes typed open, load, attach and destroy functions plus handles for every map, program and global variable, which removes the need to deploy a separate `.o` file.

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

**`__load()` is the phase in which relocation occurs**: it reads the target kernel's BTF, patches the offsets, and only afterwards submits the program to the verifier. `__open()` merely parses the embedded object, so any error arising from a mismatch between the recorded relocations and the target kernel surfaces at `__load()`.

```bash
clang -c user_loader.c -o user_loader.o
clang user_loader.o -lbpf -lelf -lz -o exec_snoop
sudo ./exec_snoop
```

The resulting `exec_snoop` binary can be copied to a host running a different kernel and loads unchanged, provided that host exposes `/sys/kernel/btf/vmlinux`. No toolchain, kernel headers, or per-host recompilation are required on the target.

For real projects, [libbpf/libbpf-bootstrap](https://github.com/libbpf/libbpf-bootstrap) vendors libbpf and bpftool as submodules and provides a working `vmlinux.h → clang → skeleton → link` pipeline, rather than a hand-written Makefile.

## Pitfalls

- **Omitting `-g` from the BPF compile.** Clang compiles the source without complaint, but `-g` is what emits the BTF and the CO-RE relocation records, so the resulting object carries nothing for `libbpf` to relocate against.
- **Dereferencing kernel pointers directly.** `task->real_parent->tgid` compiles without complaint and encodes a fixed offset with no safe-read wrapper; on a kernel whose layout differs, it reads an unrelated member or is rejected by the verifier.
- **Assuming the read width follows the target kernel.** The offset is patched, but the load size comes from the type as declared at compile time, so a member widened in a later kernel is read at the old width unless `bpf_core_field_size()` is consulted explicitly.
- **Targets without `CONFIG_DEBUG_INFO_BTF=y`.** The binary is portable only to hosts exposing `/sys/kernel/btf/vmlinux`; where the file is absent the load fails unless an external BTF, such as one from BTFHub, is supplied.
- **Treating a successful build as proof of portability.** Relocation is resolved at load time on each host, so a program that builds cleanly can still fail on a kernel where a referenced field has been renamed or removed — the case `bpf_core_field_exists()` exists to handle.
- **Regenerating `vmlinux.h` per host and assuming it changes the outcome.** The header supplies type shapes for compilation; the offsets used at runtime come from the target kernel's BTF regardless of which host produced the header.
