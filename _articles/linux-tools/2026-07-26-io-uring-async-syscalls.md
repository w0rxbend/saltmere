---
title: "io_uring: two ring buffers and (almost) zero syscalls per I/O"
date: 2026-07-26
track: linux-tools
summary: "read()/write() cost one syscall each; epoll still needs a syscall per readiness check. io_uring replaces both with two ring buffers shared between kernel and userspace, so you can submit a batch of operations and collect their results without crossing the kernel boundary each time — at a security cost distros are still arguing about."
reading_time: 5
tags: [io_uring, liburing, async-io, syscalls, kernel, security]
sources:
  - title: "Efficient IO with io_uring (Jens Axboe)"
    url: "https://kernel.dk/io_uring.pdf"
  - title: "Ringing in a new asynchronous I/O API (LWN.net)"
    url: "https://lwn.net/Articles/776703/"
  - title: "liburing/examples/io_uring-cp.c (axboe/liburing)"
    url: "https://github.com/axboe/liburing/blob/master/examples/io_uring-cp.c"
  - title: "oss-security: Our learnings from 42 Linux kernel exploits, we are limiting io_uring"
    url: "https://www.openwall.com/lists/oss-security/2023/06/17/2"
  - title: "Google Limiting IO_uring Use Due To Security Vulnerabilities (Phoronix)"
    url: "https://www.phoronix.com/news/Google-Restricting-IO_uring"
---

Every classic Linux I/O call — `read()`, `write()`, even `epoll_wait()` — crosses the syscall boundary at least once per operation. That boundary isn't free: a mode switch, a TLB consideration, mitigations for speculative-execution bugs stacked on top. For a database doing hundreds of thousands of small reads a second, that overhead is the workload. io_uring, merged into the mainline kernel by Jens Axboe in **Linux 5.1** (March 2019), attacks this directly by replacing "one syscall per op" with "one shared memory region per *batch* of ops."

## The two rings

io_uring's whole design fits in one sentence: two ring buffers, mapped into both kernel and userspace, one for requests and one for results.

```
 userspace                              kernel
 ---------                              ------
 write SQE ---> [ Submission Queue ] ---> pick up SQE, do the I/O
                                                    |
 read  CQE <--- [ Completion Queue ] <--- write CQE when done
```

- **SQ (Submission Queue):** you write an `io_uring_sqe` describing an operation — read this fd at this offset into this buffer — into the next free ring slot. No syscall yet.
- **CQ (Completion Queue):** when the kernel finishes an operation, it writes an `io_uring_cqe` (a result code plus the user-data you tagged the request with) into this ring. Again, no syscall to *produce* it.

The only syscall left is `io_uring_enter()`, and its job changes completely: instead of "do this one operation," it means "look at everything I've queued since the last call, and optionally, wait until at least N results are ready." You can queue 50 reads and pay for a single `io_uring_enter()`. LWN's original coverage put it plainly: applications "fill in an `io_uring_sqe` structure" for each op and can "add multiple SQEs before making the system call as well" — batching is the entire point, not an optimization bolted on afterward.

Push further with `IORING_SETUP_SQPOLL`: the kernel spins up a dedicated thread that polls the SQ ring itself and submits work it finds there, so a steady-state producer can add SQEs and read CQEs with **no syscalls at all**, as long as the poll thread hasn't gone idle (it naps after ~1 second of no work and needs one wake-up call to restart). That's the mode people mean when they say io_uring can do I/O without ever entering the kernel per-request.

The other half of the win, independent of the ring design: io_uring gave Linux real async **buffered** I/O for the first time. The older AIO interface only stayed non-blocking for direct I/O or when the page was already cached; io_uring's worker-thread fallback means a buffered read that misses cache doesn't stall your whole submission path.

## liburing: don't hand-roll the ring math

The raw interface means calling `io_uring_setup()`, `mmap()`-ing the SQ/CQ regions yourself, and doing the index arithmetic on the ring by hand. Nobody does this in application code anymore — you use **liburing**, Axboe's companion library, which wraps setup and gives you a request-scoped API:

```c
#include <liburing.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>

#define QUEUE_DEPTH 8

int main(void) {
    struct io_uring ring;
    struct io_uring_sqe *sqe;
    struct io_uring_cqe *cqe;
    char buf[4096] = {0};

    int fd = open("/etc/hostname", O_RDONLY);
    if (fd < 0) { perror("open"); return 1; }

    io_uring_queue_init(QUEUE_DEPTH, &ring, 0);

    /* --- submission side: fill an SQE, no syscall yet --- */
    sqe = io_uring_get_sqe(&ring);
    io_uring_prep_read(sqe, fd, buf, sizeof(buf) - 1, 0);
    io_uring_sqe_set_data(sqe, "hostname-read"); /* tag for the completion */

    /* the one syscall: submit everything queued since last call */
    io_uring_submit(&ring);

    /* --- completion side: block until a CQE shows up --- */
    io_uring_wait_cqe(&ring, &cqe);
    if (cqe->res < 0) {
        fprintf(stderr, "read failed: %s\n", strerror(-cqe->res));
    } else {
        printf("read %d bytes, tag=%s: %s", cqe->res,
               (char *)io_uring_cqe_get_data(cqe), buf);
    }
    io_uring_cqe_seen(&ring, cqe); /* mark this CQE consumed */

    io_uring_queue_exit(&ring);
    close(fd);
    return 0;
}
```

Build it with `cc file.c -luring -o read_demo`. The four functions that matter are `io_uring_get_sqe` (grab the next free submission slot), `io_uring_prep_read` (fill it in — there's a `prep_*` for nearly every syscall: `writev`, `accept`, `connect`, `openat`, `fsync`, `send`, `recv`, `timeout`, and more), `io_uring_submit` (the actual syscall), and `io_uring_wait_cqe` (block for a result). Real programs queue many SQEs before one `submit()`, and drain many CQEs per `wait_cqe` loop — that's the batching liburing's own `io_uring-cp.c` example demonstrates: it keeps up to 64 reads in flight, calling `io_uring_prep_readv` for each and pairing each completed read with a queued write, so disk and memory bandwidth stay saturated instead of ping-ponging syscall-by-syscall.

Check whether your kernel actually has it before you rely on it:

```bash
uname -r                                  # want 5.1+; SQPOLL/fixed files/etc need newer
grep -i io_uring /boot/config-$(uname -r) # CONFIG_IO_URING=y
ls /sys/kernel/debug/tracing/events/io_uring/ 2>/dev/null  # tracepoints, if debugfs mounted
```

## The security caveat

io_uring's power is also its problem: it hands userspace a fast, direct path to a huge surface of kernel operations, and several years of fuzzing found that surface was full of holes. In mid-2023, Google's security team published "Our learnings from 42 Linux kernel exploits, we are limiting io_uring" on the oss-security mailing list, reporting that a large share of the Linux kernel exploits submitted through Google's vulnerability rewards program used io_uring as the primary bug class. The response was concrete, not theoretical: **ChromeOS disabled io_uring entirely**, **Android blocks app access via seccomp-bpf** (with SELinux confinement to system processes planned), **GKE Autopilot moved to disable it by default**, and Google restricted its use across internal production infrastructure. The pattern repeats across the industry — several container runtimes and hardened distro configs seccomp-filter `io_uring_setup`/`io_uring_enter` by default, and it has since shown up as a mechanism in Linux rootkit proof-of-concepts specifically because it lets an attacker perform I/O-adjacent operations without going through the traditional syscalls that EDR tools hook.

The practical takeaway for anyone reaching for io_uring: it's genuinely worth it for a trusted, performance-critical service (databases, storage engines, proxies) that controls its own inputs — but treat it like you'd treat any large new kernel attack surface exposed to untrusted code: gate it behind a seccomp profile, don't expose it to sandboxed or multi-tenant workloads without checking your distro's current default, and expect that default to keep tightening as more CVEs land.

**Try next:** rebuild the example above with `io_uring_prep_readv` across several files queued before a single `io_uring_submit()`, then trace it with `strace -c` to count how few `io_uring_enter` calls you actually made per completed read.
