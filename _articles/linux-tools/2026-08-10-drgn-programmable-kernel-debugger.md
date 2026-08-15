---
title: "drgn: A Programmable Kernel Debugger Scripted in Python"
date: 2026-08-10
track: linux-tools
summary: "drgn is a debugger-as-a-library from Meta that reads live kernel data structures through /proc/kcore and exposes them as ordinary Python objects. A tour of installation, attaching to the running kernel, listing processes from init_task, and the drgn.helpers.linux helper modules."
reading_time: 6
tags:
  - linux
  - kernel
  - debugging
  - python
  - drgn
sources:
  - title: "drgn — official documentation"
    url: "https://drgn.readthedocs.io/en/stable/index.html"
  - title: "drgn User Guide"
    url: "https://drgn.readthedocs.io/en/stable/user_guide.html"
  - title: "osandov/drgn — Programmable debugger (GitHub)"
    url: "https://github.com/osandov/drgn"
  - title: "A kernel debugger in Python: drgn (LWN.net)"
    url: "https://lwn.net/Articles/789641/"
  - title: "Using drgn on production kernels (LWN.net)"
    url: "https://lwn.net/Articles/952942/"
---

**Gist.** Inspecting the state of a running kernel normally means a debugger with a fixed command vocabulary, which stops at whatever queries its authors anticipated. **drgn** (pronounced "dragon"), created by Omar Sandoval at Meta, replaces the command vocabulary with a Python library: kernel types, variables and structures are wrapped as ordinary Python objects, and analysis is written as Python code against them. The cost is that drgn needs matching DWARF debugging information for the exact kernel under inspection, and that every field access is a memory read rather than a lookup in a cached snapshot.

## What drgn is

drgn is simultaneously a Python library (`import drgn`) and a command-line program (`drgn`) that opens a read-eval-print loop with a debugger already attached. It reads target memory and DWARF debugging information. For a live kernel, memory is read through **`/proc/kcore`**; the same code attaches to a userspace process or opens a kernel or userspace core dump. Debug information must be supplied: a `vmlinux` carrying DWARF, a distribution `kernel-debuginfo` package, or `debuginfod`.

Sandoval's description in the LWN write-up is "debugger as a library": the types, variables and similar entities are wrapped so that arbitrary operations can be performed on them. The interface is not a debugger macro language; it is Python that happens to reference kernel memory.

## Position relative to crash, gdb and eBPF

- **crash** is purpose-built and fast for common tasks but exposes a fixed set of commands. A query its authors did not anticipate — cross-referencing two subsystems, computing an aggregate, filtering a list by a custom predicate — has no expression. In drgn the analysis is arbitrary Python, so such queries are the normal case rather than the exception.
- **gdb** is organised around breakpoints and stepping, which does not apply to a production machine that cannot be stopped. Its Python scripting largely wraps existing gdb commands; drgn inverts the relationship, making scripting the primary interface.
- **eBPF (extended Berkeley Packet Filter) and bpftrace** trace a live system but cannot analyse a historical crash dump. drgn presents the same application programming interface over a live kernel and over a dump.

## References and values

A drgn object is either a **reference** — its bytes are re-read from target memory on each access — or a **value**, a static snapshot taken once. **Nothing is copied until it is requested**, which is what makes it practical to hold a handle on a large interconnected structure and materialise only the fields touched. The distinction is observable on a quantity that changes:

```
>>> jiffies = prog["jiffies"]
>>> jiffies.value_()
4391639989
>>> import time; time.sleep(1)
>>> jiffies.value_()          # re-read from memory: it moved
4391640290
>>> snap = jiffies.read_()    # freeze a value snapshot
>>> snap.value_()
4391640291
>>> time.sleep(1); snap.value_()
4391640291                    # unchanged: values are static
```

The consequence for analysis is that **a loop over references observes a kernel that is mutating underneath it**. Two reads of the same field in the same iteration may disagree. `read_()` is the operation that pins a value against that drift.

## Installation

The published package ships prebuilt wheels for common platforms:

```
$ sudo pip3 install drgn
```

drgn is also packaged for Fedora, RHEL/CentOS, Debian/Ubuntu, Arch, Gentoo, Oracle Linux and openSUSE:

```
$ sudo dnf install drgn            # Fedora / RHEL
$ sudo apt install python3-drgn    # Debian / Ubuntu
```

## Attaching to a target

Invoked with no arguments and root privileges, the command-line program attaches to the running kernel and binds a `Program` object to the name `prog`:

```
$ sudo drgn
>>> prog["init_task"].pid
(pid_t)0
```

The same binary attaches to other targets:

```
$ drgn -p $PID        # a running userspace process
$ drgn -c vmcore      # a kernel core dump / vmcore
```

## The `prog` object

`prog` is a `drgn.Program` and is the entry point for type lookup, variable access and raw memory reads:

```
>>> prog.type("struct list_head")
struct list_head {
        struct list_head *next;
        struct list_head *prev;
}
>>> prog["jiffies"]              # equivalent to prog.variable("jiffies")
(volatile unsigned long)4391639989
>>> prog.read(0xffffffffbe411e10, 16)
b'...'
```

Field access reads as C does, minus the pointer punctuation:

```
>>> prog["init_task"].comm
(char [16])"swapper/0"
```

`init_task` is the kernel's first task — PID 0, the idle or `swapper` thread — so a `comm` of `swapper/0` confirms that the read reached real memory.

Three syntax translations carry the difference from C:

- `ptr.member` auto-dereferences, corresponding to C's `ptr->member`
- `ptr[0]` dereferences a pointer; there is no `*ptr` form
- `var.address_of_()` takes an address, corresponding to C's `&var`

## Walking the task list

Enumerating processes is the canonical first exercise. Instead of hand-walking the linked list, `for_each_task` from `drgn.helpers.linux.pid` yields `struct task_struct *`:

```
>>> from drgn.helpers.linux.pid import for_each_task, find_task
>>> for task in for_each_task(prog):
...     print(task.pid.value_(), task.comm.string_().decode())
...
0 swapper/0
1 systemd
2 kthreadd
...
```

`.string_()` reads a NUL-terminated `char[]` out of target memory and returns `bytes`; the decode step is therefore mandatory before string comparison. A single task is reachable by PID:

```
>>> task = find_task(prog, 1)
>>> task.comm
(char [16])"systemd"
>>> task.parent.comm            # ptr->parent->comm, no arrows required
(char [16])"swapper/0"
```

## The generic intrusive-list walker

Kernel data structures are stitched together with `struct list_head` embedded inside each element rather than in separate node objects. `list_for_each_entry` turns any such intrusive list into a Python generator. The example below walks the loaded-module list and filters by reference count:

```
>>> from drgn.helpers.linux.list import list_for_each_entry
>>> for mod in list_for_each_entry('struct module',
...                                prog['modules'].address_of_(),
...                                'list'):
...     if mod.refcnt.counter > 10:
...         print(mod.name.string_().decode())
```

The three arguments are **the element type, the address of the list head, and the name of the `list_head` member embedded in each entry** — precisely the information C's `container_of()` requires, written out explicitly instead of inferred by the compiler. A wrong member name yields entries offset by the difference between the two members, not an error.

## Filesystem and per-CPU helpers

Domain-specific helpers remove the need to re-derive plumbing. The mount table lives in `drgn.helpers.linux.fs`:

```
>>> from drgn.helpers.linux.fs import for_each_mount, mount_dst, mount_fstype
>>> for mnt in for_each_mount(prog):
...     print(mount_fstype(mnt).decode(), mount_dst(mnt).decode())
...
ext4 /
proc /proc
tmpfs /run
```

`print_mounts(prog)` dumps the same table directly. Per-CPU variables are read with `per_cpu` from `drgn.helpers.linux.percpu`, which resolves the per-CPU base address for a given processor:

```
>>> from drgn.helpers.linux.percpu import per_cpu
>>> rq = per_cpu(prog['runqueues'], 0)   # struct rq for CPU 0
>>> rq.nr_running
(unsigned int)1
```

Reading the raw per-CPU symbol without `per_cpu` yields the object at the per-CPU offset base, not any processor's instance.

## Consequences

drgn narrows the distance between reading kernel source and querying kernel state. Once state is Python objects, the whole language applies: comprehensions filter tasks, `collections.Counter` tallies states, and a script checked into a repository reproduces a prior diagnosis during the next incident. A verification exercise: in a throwaway virtual machine, loop over `for_each_task(prog)` counting tasks by `task.__state` and compare the totals against `ps`. Agreement establishes that the reads observe the same state the scheduler does.

## Pitfalls

- **Debug information that does not match the running kernel produces wrong field offsets, not an error.** DWARF supplies structure layout; a `vmlinux` from a different build reads correct addresses with incorrect member offsets, so fields appear populated with garbage.
- **Iterating references while the kernel runs can observe a torn view.** Each attribute access is a fresh memory read, so a task freed mid-loop leaves subsequent reads pointing at reused memory. `read_()` snapshots a value at a single instant.
- **`list_for_each_entry` given the wrong member name silently returns misaligned pointers.** The helper computes element addresses by subtracting the member offset; a wrong name subtracts a wrong constant and every field thereafter is misread.
- **`comm` is a `char[16]`, not a Python string.** Comparing it directly against a `str` never matches; `.string_()` returns `bytes`, requiring an explicit decode.
- **Attaching to the live kernel requires root and a readable `/proc/kcore`.** Kernels built without `CONFIG_PROC_KCORE`, or systems restricting it, leave core dumps as the only target.
- **Breakpoints and single-stepping are absent.** drgn reads state; workflows built around stopping the target belong to gdb, and tracing execution over time belongs to eBPF or bpftrace.
