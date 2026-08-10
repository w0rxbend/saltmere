---
title: "drgn: The Programmable Kernel Debugger You Script in Python"
date: 2026-08-10
track: linux-tools
summary: "drgn is a debugger-as-a-library from Meta that reads live kernel data structures through /proc/kcore and lets you walk them in plain Python. A hands-on tour: install it, attach to the running kernel, list processes off init_task, and use the drgn.helpers.linux helpers."
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

The fastest way to understand a running system is to poke at its actual state, not to read about what its state is supposed to be. That is the pitch for **drgn** (pronounced "dragon"), a debugger written by Omar Sandoval and the Linux kernel team at Meta. It is unusual in a useful way: instead of giving you a command prompt with a fixed vocabulary, it gives you a Python library. Kernel types, variables, and structs show up as ordinary Python objects, and you debug by writing code against them.

This article is a practical tour. We will install drgn, attach it to the live kernel, walk the task list off `init_task`, inspect struct fields, and use the batteries-included helpers in `drgn.helpers.linux`.

## What drgn actually is

drgn is two things at once: a Python library (`import drgn`) and a CLI (`drgn`) that drops you into a REPL with a debugger pre-attached. Under the hood it reads memory and DWARF debugging information. For a live kernel it reads memory through `/proc/kcore`; it can equally attach to a userspace process or open a kernel/userspace core dump. It needs debug info for the kernel you are inspecting — typically a `vmlinux` with DWARF, a distro `kernel-debuginfo` package, or `debuginfod`.

The key design idea, in Sandoval's words from his LWN write-up, is a "debugger as a library": drgn "magically wraps the types, variables, and such so that you can do anything you want with them." You are not scripting a debugger's macro language. You are writing Python that happens to reference kernel memory.

## How it differs from crash and gdb

If you have used the **crash** utility or **gdb** on a kernel, drgn occupies a deliberately different niche:

- **crash** is purpose-built and fast for common tasks, but it exposes a fixed set of commands. As soon as you want to do something its authors did not anticipate — cross-reference two subsystems, compute an aggregate, filter a list by a custom predicate — you hit a wall. drgn's answer is "it's just Python," so arbitrary analysis is the normal case.
- **gdb** is oriented around breakpoints and stepping, which is a poor fit for a production box you cannot stop. Its Python scripting largely wraps existing gdb commands. drgn inverts that: scripting is the primary interface.
- **eBPF/bpftrace** are excellent for live tracing but cannot analyze a historical crash dump. drgn works against both a live kernel and a dump with the same API.

Another distinguishing property is **lazy reading**. A drgn object can be a *reference* (re-read from memory on each access) or a *value* (a static snapshot). Nothing is copied until you ask for it, which is what makes it practical to "hold" enormous interconnected structures and only materialize the fields you touch.

## Install

Via pip (the package ships prebuilt wheels for common platforms):

```
$ sudo pip3 install drgn
```

Or use a distribution package — drgn is packaged for Fedora, RHEL/CentOS, Debian/Ubuntu, Arch, Gentoo, Oracle Linux, and openSUSE, e.g.:

```
$ sudo dnf install drgn      # Fedora / RHEL
$ sudo apt install python3-drgn   # Debian / Ubuntu
```

## Attach to the live kernel

Running the CLI with no arguments and root privileges attaches to the running kernel and hands you a `Program` object already bound to the name `prog`:

```
$ sudo drgn
>>> prog
Program(<host kernel>)
```

The same binary attaches to other targets:

```
$ drgn -p $PID        # attach to a running userspace process
$ drgn -c vmcore      # open a kernel core dump / vmcore
```

## The `prog` object: types, variables, memory

`prog` is a `drgn.Program`, and it is the entry point for everything: look up types, access variables, and read raw memory.

```
>>> prog.type("struct list_head")
struct list_head {
        struct list_head *next;
        struct list_head *prev;
}
>>> prog["jiffies"]              # same as prog.variable("jiffies")
(volatile unsigned long)4391639989
>>> prog.read(0xffffffffbe411e10, 16)
b'...'
```

Accessing a variable and a struct field reads exactly like C, minus the pointer punctuation:

```
>>> prog["init_task"].comm
(char [16])"swapper/0"
```

`init_task` is the kernel's first task (PID 0, the idle/`swapper` thread), so its `comm` is `swapper/0` — a nice confirmation you are talking to real memory.

The syntax translation is worth memorizing:

- `ptr.member` auto-dereferences, like C's `ptr->member`
- `ptr[0]` dereferences a pointer (there is no `*ptr`)
- `var.address_of_()` takes an address, like C's `&var`

References vs. values shows up clearly with something that changes:

```
>>> jiffies = prog["jiffies"]
>>> jiffies.value_()
4391639989
>>> import time; time.sleep(1)
>>> jiffies.value_()          # re-read: it moved
4391640290
>>> snap = jiffies.read_()    # freeze a value snapshot
>>> snap.value_()
4391640291
>>> time.sleep(1); snap.value_()
4391640291                    # unchanged — values are static
```

## Walk the task list off `init_task`

Listing every process is the "hello world" of kernel introspection. Rather than hand-walking the linked list, use `for_each_task` from `drgn.helpers.linux.pid`, which yields `struct task_struct *`:

```
>>> from drgn.helpers.linux.pid import for_each_task, find_task
>>> for task in for_each_task():
...     print(task.pid.value_(), task.comm.string_().decode())
...
0 swapper/0
1 systemd
2 kthreadd
...
```

`.string_()` reads a NUL-terminated `char[]` out of memory as `bytes`. To jump straight to one task by PID:

```
>>> task = find_task(1)
>>> task.comm
(char [16])"systemd"
>>> task.parent.comm            # ptr->parent->comm, no arrows needed
(char [16])"swapper/0"
```

## The generic list walker

Kernel data structures are stitched together with `struct list_head`. The helper `list_for_each_entry` turns any such intrusive list into a Python generator. Here it walks the loaded-modules list, filtering by reference count:

```
>>> from drgn.helpers.linux.list import list_for_each_entry
>>> for mod in list_for_each_entry('struct module',
...                                prog['modules'].address_of_(),
...                                'list'):
...     if mod.refcnt.counter > 10:
...         print(mod.name.string_().decode())
```

The three arguments are the element type, the address of the list head, and the name of the `list_head` member embedded in each entry — exactly the information `container_of()` needs in C, spelled out explicitly.

## Mounts and per-CPU data

drgn ships domain-specific helpers so you rarely re-derive plumbing. The mount table lives in `drgn.helpers.linux.fs`:

```
>>> from drgn.helpers.linux.fs import for_each_mount, mount_dst, mount_fstype
>>> for mnt in for_each_mount(prog):
...     print(mount_fstype(mnt).decode(), mount_dst(mnt).decode())
...
ext4 /
proc /proc
tmpfs /run
```

There is also `print_mounts(prog)` if you just want the table dumped. And per-CPU variables — a recurring source of confusion in kernel code — are read with `per_cpu` from `drgn.helpers.linux.percpu`, which resolves the base for a given CPU:

```
>>> from drgn.helpers.linux.percpu import per_cpu
>>> rq = per_cpu(prog['runqueues'], 0)   # struct rq for CPU 0
>>> rq.nr_running
(unsigned int)1
```

## Why this matters

The reason drgn feels different is that it collapses the gap between "reading the kernel source" and "querying the kernel." Once state is Python objects, the full language is available: list comprehensions to filter tasks, `collections.Counter` to tally states, a script committed to your repo that reproduces a diagnosis on the next incident. You are learning the system by inspecting the real thing, and the inspection is itself code you can keep.

**Try next:** boot a throwaway VM, `sudo drgn` into it, and write a five-line loop over `for_each_task()` that counts tasks by `task.__state` — then compare your count to `ps`. When it matches, you have verified you are reading the same reality the scheduler sees.
