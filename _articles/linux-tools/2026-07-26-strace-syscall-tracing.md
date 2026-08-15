---
title: "strace: observing what a process asks the kernel"
date: 2026-07-26
track: linux-tools
summary: "When a program reports a missing configuration file or stops making progress, the source code describes intent rather than behaviour. strace attaches through ptrace(2) and prints every system call a process issues — the file actually opened, the call it is blocked in, the errno it discarded. This article covers the mechanism, its measured cost, and the flags that carry the work."
reading_time: 6
tags: [strace, ptrace, syscalls, debugging, linux-tools]
sources:
  - title: "strace(1) — Linux manual page (man7.org)"
    url: "https://man7.org/linux/man-pages/man1/strace.1.html"
  - title: "strace: Wow, much syscall — Brendan Gregg"
    url: "https://www.brendangregg.com/blog/2014-05-11/strace-wow-much-syscall.html"
  - title: "Why strace doesn't work in Docker — Julia Evans"
    url: "https://jvns.ca/blog/2020/04/29/why-strace-doesnt-work-in-docker/"
  - title: "A zine about strace — Julia Evans"
    url: "https://jvns.ca/blog/2015/04/14/strace-zine/"
  - title: "strace cheat sheet — Packagecloud Blog"
    url: "https://blog.packagecloud.io/strace-cheat-sheet/"
---

**Gist.** A process's source code states what it intends to do; only its system calls determine what it does, and the two diverge whenever a fallback path, an environment variable or a swallowed error code intervenes. `strace` uses `ptrace(2)` to stop the traced process at every system-call boundary and print the call with decoded arguments and return value. The mechanism costs **two stops per system call**, each requiring a switch to the tracer and back, which on a syscall-bound workload dominates the runtime entirely.

## Mechanism: ptrace and the per-syscall stop

`strace` is built on the `ptrace(2)` system call, the same primitive that debuggers use to control another process's execution. The tracer establishes a tracing relationship with the tracee; thereafter the kernel places the tracee in a **stopped state at system-call entry and again at system-call exit**. At each stop the kernel hands control to `strace`, which reads the tracee's registers and memory to decode the call and its arguments, prints a line, and resumes the tracee.

The invariant that makes the output trustworthy is that **the tracee cannot make forward progress past a boundary until the tracer resumes it**. Nothing is sampled and nothing is dropped: every call in the selected set appears, in issue order, with the value the kernel returned. That same invariant is the cost. Each system call now involves the tracee stopping, the scheduler running `strace`, `strace` reading tracee memory, and the tracee being resumed — twice.

The man page states the consequence plainly: "a traced process runs slowly." Brendan Gregg measured the extreme with `dd`, a program that issues system calls back to back with almost no computation between them, and reported a **442x slowdown** under tracing. The ratio is worst-case precisely because `dd` has no work to amortise the stops against; a process that spends most of its time in userspace computation is affected far less. The operational reading is that `strace` is a debugging tool rather than a profiler. For low-overhead visibility into the system calls a live service issues, `perf trace` and eBPF-based tools are the appropriate instruments; `strace` is for cases that require full argument and return detail on a process that can afford to be slowed.

## Why it fails inside a container

Tracing requires either `CAP_SYS_PTRACE` or that the tracer and tracee belong to the same user. Containers frequently block it, and **the blocking layer is seccomp rather than capabilities**. Docker's default seccomp profile does not whitelist `ptrace`, so the call is rejected by the filter before the kernel's permission check is ever reached. The observable symptom is that `strace` fails outright inside such a container even when the process is owned by the same user that would be permitted to trace it outside one. Whether a given deployment exhibits this depends on the runtime's profile: the failure is a property of the seccomp profile in force, not of containers as such.

`--cap-add=SYS_PTRACE` restores tracing because **Docker's implementation also widens the seccomp whitelist when that capability is added**, not because the capability by itself was the obstacle. Diagnosing this as a capability problem leads to the right command for the wrong reason, and the reasoning fails on any runtime whose seccomp profile is configured independently of capabilities.

## The essential flags

| Flag | Effect |
|---|---|
| `-f` | Follow forks — trace children created via `fork`/`vfork`/`clone` |
| `-e trace=SET` | Restrict output to a system-call set: names (`open,read`) or classes such as `%file`, `%network`, `%process`, `%signal`, `%memory`; a `!` prefix excludes |
| `-p PID` | Attach to an already-running process; the option is repeatable to attach to several |
| `-c` | Suppress per-call output and print a summary of calls, errors and time per system call |
| `-T` | Append the wall-clock time spent inside each call as `<seconds>` |
| `-y` | Decode file descriptors — print the path or socket behind each descriptor argument |
| `-s SIZE` | Maximum printed string length, default 32; raising it prevents truncated buffers |
| `-o FILE` | Write output to a file rather than standard error |
| `-tt` | Prefix each line with a timestamp at microsecond precision |

## Reading the output

Each line has the form `syscall(args) = return_value`, with error returns decoded to their symbolic name and message:

```
openat(AT_FDCWD, "/etc/myapp/config.yml", O_RDONLY) = -1 ENOENT (No such file or directory)
openat(AT_FDCWD, "/etc/myapp.conf", O_RDONLY) = 3
read(3, "port: 8080\nhost: 0.0.0.0\n", 4096) = 25
close(3)                                = 0
```

The sequence establishes two facts that the source alone does not: the program attempted `/etc/myapp/config.yml`, received `ENOENT`, and **continued to a second path that is the file in effect**. Whether the fallback is undocumented or an environment variable redirected the lookup, the trace records the resolution rather than the intent.

Under `-f`, each line is prefixed with `[pid NNNN]`. A call that has not yet returned is printed as `<unfinished ...>` and its completion appears later as `<... syscall resumed>`, so lines belonging to one call may be separated by lines from other threads:

```
[pid  8842] read(4, <unfinished ...>
[pid  8843] futex(0x7f2c1c000b34, FUTEX_WAIT, 2, NULL <unfinished ...>
[pid  8842] <... read resumed>"GET / HTTP/1.1\r\n", 8192) = 342
```

## Recipes

**Locating a failed path lookup.** Restricting to the filesystem class and filtering for `ENOENT` isolates every unsuccessful name resolution:

```bash
strace -f -e trace=%file -o /tmp/trace.log ./myapp
grep ENOENT /tmp/trace.log
```

`%file` covers every system call that takes a filename argument — `open`, `openat`, `stat`, `access`, `unlink`, `chmod` and others — so the filter catches lookups performed through an unanticipated library function.

**Identifying the configuration file in effect.** Attaching to a running process and restricting to `openat` shows which of several candidate files is opened:

```bash
strace -f -e trace=openat -p 12345
```

`-p` attaches to a live process; interrupting `strace` detaches and leaves the process running.

**Locating a stall.** The call left without a return value is the one the process is blocked in:

```bash
strace -f -T -p 12345
```

Frequent cases are `futex` (blocked on a lock), `read` or `recvfrom` on a socket receiving nothing, and `connect` blocked because SYN packets are being dropped rather than rejected. `-T` records the duration of each completed call, so a `connect` that returns only after the kernel exhausts its SYN retransmissions is visible in a saved trace without observing the stall live.

**Aggregating instead of reading every line.** The summary mode reports counts, error counts and time per system call:

```bash
strace -f -c -o /tmp/summary.txt ./myapp
```

```
% time     seconds  usecs/call     calls    errors syscall
------ ----------- ----------- --------- --------- ----------------
 61.42    0.048113          12      3901           read
 22.05    0.017271          44       387        12 openat
  9.88    0.007738           9       841           futex
```

The `errors` column is often the entire result: **12 failed `openat` calls out of 387** localises the problem without reading a single per-call line.

**Descriptor decoding.** Adding `-y` annotates each descriptor with its target, removing the need to correlate descriptor numbers against earlier `open` calls by hand:

```
read(3</etc/myapp.conf>, "port: 8080\n", 4096) = 11
```

## Pitfalls

- Attaching to a latency-sensitive process in production can slow it by orders of magnitude; on a syscall-bound workload the reported worst case is 442x, because every call costs two stops and the switches to the tracer and back that each one implies.
- Tracing inside a container whose seccomp profile omits `ptrace` fails even for a same-user process, because the filter rejects the call before the capability check runs.
- Adding `--cap-add=SYS_PTRACE` and concluding the capability was the missing piece misattributes the fix: Docker widens the seccomp whitelist alongside the capability, and the same reasoning does not transfer to a runtime that configures the two separately.
- Omitting `-f` loses everything a forked or cloned child does, so a program that performs its real work in a subprocess produces a trace that ends shortly after `clone`.
- The default `-s 32` truncates printed strings, so a path or buffer longer than 32 characters is shown cut off and can be misread as a different value.
- Under `-f`, a call printed as `<unfinished ...>` has its return value on a later, non-adjacent line; reading the interleaved output as if each line were complete attributes results to the wrong call.
- Descriptor numbers are reused after `close`, so without `-y` a descriptor correlated against an earlier `open` may refer to a different file by the time the line in question was written.
