---
title: "strace: watch what a process actually asks the kernel"
date: 2026-07-26
track: linux-tools
summary: "When a program 'can't find the config' or just hangs, guessing wastes time. strace attaches via ptrace and prints every syscall a process makes — the file it really opened, the syscall it's stuck in, the errno it swallowed. Here's how it works, what it costs, and the flags worth memorizing."
reading_time: 5
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

A process says "config not found." You've checked the obvious path three times and it's right there. This is exactly the moment to stop reasoning about what the program *should* do and go look at what it *actually* does. `strace` prints every syscall a process makes, with arguments and return values, in real time. It doesn't care what the program thinks its logic is — it shows you the conversation with the kernel, which is the only conversation that determines behavior.

## How it works: ptrace, and what that costs you

`strace` is built entirely on the `ptrace(2)` system call — the same primitive debuggers use to control a process's execution. When you `strace` a program, the kernel puts the tracee into a stopped state at every syscall entry and exit, hands control to `strace`, lets it inspect and print the syscall, then resumes the tracee. That's a full context switch to `strace` and back, twice per syscall.

This is why a traced process runs slower — the man page says so directly: "a traced process runs slowly." Brendan Gregg measured the worst case with `dd`, a program that does nothing but issue syscalls back to back: unt­raced it finished in 0.1 seconds, traced it took 46 seconds — a **442x slowdown**. That's an extreme case (dd has essentially no work between syscalls, so the tracing overhead *is* the runtime), but it makes the point: strace is a debugging tool, not a profiler, and definitely not something to leave attached to a latency-sensitive production process without a plan. If you need low-overhead visibility into what syscalls a live service is making, reach for `perf trace` or eBPF-based tools first and save `strace` for the cases where you need the full argument/return detail on a process you can afford to slow down.

One consequence of the ptrace mechanism worth knowing: it needs `CAP_SYS_PTRACE` (or to be tracing your own process as your own user), and containers commonly block it. Docker's default seccomp profile doesn't whitelist `ptrace`, so `strace` inside a stock container fails outright — not because of a missing Linux capability but because the seccomp filter drops the syscall before it reaches the kernel's permission check. `--cap-add=SYS_PTRACE` works because Docker's implementation also widens the seccomp whitelist when you add that capability, not because the capability alone was the blocker.

## The essential flags

| Flag | What it does |
|---|---|
| `-f` | Follow forks — trace child processes created via `fork`/`vfork`/`clone` too |
| `-e trace=SET` | Restrict output to a syscall set: names (`open,read`), or classes like `%file`, `%network`, `%process`, `%signal`, `%memory`; prefix `!` to exclude |
| `-p PID` | Attach to an already-running process (or comma-separated list of PIDs) |
| `-c` | Suppress per-call output, print a summary: calls, errors, and time per syscall |
| `-T` | Show wall-clock time spent inside each syscall, appended as `<seconds>` |
| `-y` | Decode file descriptors — print the path/socket behind each fd argument |
| `-s SIZE` | Max length of printed strings (default 32); raise it to stop truncated buffers |
| `-o FILE` | Write trace output to a file instead of stderr |
| `-tt` | Print a timestamp with microsecond precision on each line |

## Reading the output

Each line is `syscall(args) = return_value`, with errors decoded automatically:

```
openat(AT_FDCWD, "/etc/myapp/config.yml", O_RDONLY) = -1 ENOENT (No such file or directory)
openat(AT_FDCWD, "/etc/myapp.conf", O_RDONLY) = 3
read(3, "port: 8080\nhost: 0.0.0.0\n", 4096) = 26
close(3)                                = 0
```

Two things jump out immediately: the program tried `/etc/myapp/config.yml` first (and it doesn't exist), then fell back to `/etc/myapp.conf`, which it opened as fd 3 and read from. No amount of reading the source beats seeing this — maybe the fallback path was undocumented, maybe an env var pointed somewhere unexpected. The trace doesn't lie about it.

For multi-threaded or forked traces (`-f`), each line is prefixed with `[pid NNNN]`. Long-running syscalls that haven't returned yet show as `<unfinished ...>`, and completion appears later as `<... syscall resumed>`:

```
[pid  8842] read(4, <unfinished ...>
[pid  8843] futex(0x7f2c1c000b34, FUTEX_WAIT, 2, NULL <unfinished ...>
[pid  8842] <... read resumed>"GET / HTTP/1.1\r\n", 8192) = 342
```

## Practical recipes

**"Why can't it find that file?"** Filter to filesystem calls and grep for the failing path:

```bash
strace -f -e trace=%file -o /tmp/trace.log ./myapp
grep ENOENT /tmp/trace.log
```

`%file` covers every syscall that takes a filename argument (`open`, `openat`, `stat`, `access`, `unlink`, `chmod`, and friends), so this catches path lookups even when the app uses a function you didn't expect.

**"Which config does it actually read?"** Attach to a running process (or trace from launch) and watch for opens against paths matching your config's basename:

```bash
strace -f -e trace=openat -p 12345
```

If the binary is already running, `-p` attaches live — Ctrl-C detaches cleanly and leaves the process running. This is the fastest way to settle an argument about which of three `nginx.conf` copies is the one actually in effect.

**"Where is it hanging on a syscall?"** Attach and just watch the last line printed — whatever syscall is sitting `<unfinished ...>` (or simply the last line with no return value yet) is where it's stuck:

```bash
strace -f -T -p 12345
```

Common culprits: `futex` (waiting on a lock), `read`/`recvfrom` on a socket with nothing arriving (network stall, not a hang), or `connect` taking forever (firewall silently dropping SYNs rather than rejecting). `-T` shows how long each completed call took, so a `connect` that finally returns after 21 seconds is a giveaway even without watching it stall live.

**"What's actually slow?"** Skip the firehose and get numbers:

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

That `errors` column on `openat` — 12 failed opens — is often the whole investigation right there.

**Bonus: decode the fds.** Add `-y` to any of the above and file descriptor numbers get annotated with what they point to:

```
read(3</etc/myapp.conf>, "port: 8080\n", 4096) = 12
```

No more cross-referencing fd numbers against earlier `open` calls by hand.

**Try next:** run `strace -c -f` against a normal shell command you think you understand well (`ls -la`, `curl` to localhost) and read the summary before looking at per-call output — you'll usually find one syscall dominating that you didn't expect, which is the right instinct to build before you need it on something broken.
