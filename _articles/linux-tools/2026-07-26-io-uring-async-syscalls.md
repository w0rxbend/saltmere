---
title: "io_uring: two ring buffers and (almost) zero syscalls per I/O"
date: 2026-07-26
track: linux-tools
summary: "read()/write() cost one syscall each; epoll still needs a syscall per readiness check. io_uring replaces both with two ring buffers shared between kernel and userspace, so a batch of operations can be submitted and their results collected without crossing the kernel boundary each time — at a security cost distributions are still arguing about."
reading_time: 6
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

**Gist.** Every classic Linux input/output (I/O) call — `read()`, `write()`, `epoll_wait()` — crosses the syscall boundary at least once per operation, and at high request rates that boundary crossing *is* the workload. io_uring, merged into the mainline kernel by Jens Axboe in **Linux 5.1** (2019), replaces "one syscall per operation" with two ring buffers mapped into both kernel and userspace address spaces, so an arbitrary number of operations can be queued and reaped per boundary crossing. The cost is a large, directly reachable kernel surface prominent enough that several vendors now disable or filter it by default.

## The two rings

The design reduces to one sentence: **two single-producer/single-consumer ring buffers, shared by `mmap()` between the kernel and the process, one carrying requests and one carrying results.**

```
 userspace                              kernel
 ---------                              ------
 write SQE ---> [ Submission Queue ] ---> pick up SQE, do the I/O
                                                    |
 read  CQE <--- [ Completion Queue ] <--- write CQE when done
```

- **Submission queue (SQ).** The application fills an `io_uring_sqe` structure — opcode, file descriptor, offset, buffer pointer, length — into the next free ring slot. No syscall is involved in producing it.
- **Completion queue (CQ).** When an operation finishes, the kernel writes an `io_uring_cqe` into this ring: a result code with the same sign convention as the corresponding syscall (negative errno on failure) plus the opaque `user_data` value the request was tagged with. No syscall is involved in producing it either.

Each ring is described by a **head index and a tail index, both shared memory words**. The producer advances the tail after writing the entry; the consumer advances the head after reading it. Because the two sides run concurrently on different CPUs, the ordering between "entry contents visible" and "tail visible" is the load-bearing invariant: the entry must be written before the tail update becomes observable, or the consumer reads a slot whose contents are not yet valid. Axboe's *Efficient IO with io_uring* documents this explicitly, and it is the principal reason applications are told to use liburing rather than manipulate the indices directly — **the barriers, not the arithmetic, are what hand-rolled ring code gets wrong.** A ring is a fixed-size array; when the tail reaches the head the queue is full and submission must wait for the consumer to drain it.

The one remaining syscall is `io_uring_enter()`, and its meaning is different in kind from `read()`: rather than "perform this operation", it means "consider everything queued since the last call, and optionally block until at least *N* completions are available". Fifty queued reads cost one `io_uring_enter()`. LWN's original coverage states that applications "fill in an `io_uring_sqe` structure" per operation and can "add multiple SQEs before making the system call as well" — **batching is the interface, not a later optimisation**.

`IORING_SETUP_SQPOLL` removes even that call. The kernel creates a dedicated thread that polls the submission ring and picks up entries as they appear, so a steady-state producer adds SQEs and reaps CQEs with **no syscalls at all**. The qualification matters: the poll thread sleeps after a configurable idle period (`sq_thread_idle`), and the application must then issue one `io_uring_enter()` to wake it. The wake-up condition is signalled in the ring's shared flags word (`IORING_SQ_NEED_WAKEUP`), so the application checks a memory location rather than guessing.

Independently of the ring mechanics, io_uring supplied Linux with genuinely asynchronous **buffered** I/O. The older AIO interface stayed non-blocking only for direct I/O, or when the requested page happened to be resident in the page cache; a cache miss reverted to a blocking submission. io_uring's fallback to kernel worker threads means a buffered read that misses cache does not stall the submitter's progress on the rest of the batch.

## liburing

The raw interface requires `io_uring_setup()`, `mmap()`-ing the SQ and CQ regions, and maintaining the index arithmetic and barriers by hand. Application code uses **liburing**, Axboe's companion library, which performs setup and exposes a request-scoped API.

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

    /* submission side: fill an SQE; no syscall is made here */
    sqe = io_uring_get_sqe(&ring);            /* NULL when the SQ ring is full */
    io_uring_prep_read(sqe, fd, buf, sizeof(buf) - 1, 0);
    io_uring_sqe_set_data(sqe, "hostname-read"); /* echoed back in the CQE */

    io_uring_submit(&ring);                   /* the one syscall */

    /* completion side: block until a CQE is available */
    io_uring_wait_cqe(&ring, &cqe);
    if (cqe->res < 0) {                       /* negative errno, not -1 */
        fprintf(stderr, "read failed: %s\n", strerror(-cqe->res));
    } else {
        printf("read %d bytes, tag=%s: %s", cqe->res,
               (char *)io_uring_cqe_get_data(cqe), buf);
    }
    io_uring_cqe_seen(&ring, cqe);            /* advances the CQ head */

    io_uring_queue_exit(&ring);
    close(fd);
    return 0;
}
```

Built with `cc file.c -luring -o read_demo`. Four calls carry the mechanism: `io_uring_get_sqe` claims the next free submission slot, `io_uring_prep_*` fills it (a preparation helper exists for most syscalls — `writev`, `accept`, `connect`, `openat`, `fsync`, `send`, `recv`, `timeout`), `io_uring_submit` performs the boundary crossing, and `io_uring_wait_cqe` blocks for a result. `io_uring_cqe_seen` is not bookkeeping: **until it is called the slot is not released, and a loop that omits it will exhaust the completion ring.**

The single-request form above shows the API, not the design point. liburing's own `io_uring-cp.c` example demonstrates the intended shape: it keeps up to **64 reads in flight**, issuing `io_uring_prep_readv` for each and pairing every completed read with a queued write, so storage and memory bandwidth stay occupied instead of alternating one syscall at a time. Completions arrive in **whatever order the kernel finishes the work, not submission order**, which is why every request carries a `user_data` tag: reassociating a CQE with its request is the application's responsibility.

Availability is checkable before it is relied upon:

```bash
uname -r                                  # 5.1+ minimum; SQPOLL, fixed files etc. need newer
grep -i io_uring /boot/config-$(uname -r) # CONFIG_IO_URING=y
ls /sys/kernel/debug/tracing/events/io_uring/ 2>/dev/null  # tracepoints, if debugfs is mounted
```

The kernel version check is necessary but not sufficient: a seccomp-BPF (Berkeley Packet Filter) policy or container runtime profile can block `io_uring_setup` on a kernel that supports it, in which case setup fails at runtime rather than at build time.

## The security position

The same property that makes io_uring fast — a direct userspace path to a wide range of kernel operations — enlarged the attack surface, and years of fuzzing found defects in it. In mid-2023 Google's security team published an oss-security mailing list post on limiting io_uring, drawn from an analysis of 42 Linux kernel exploits, reporting that a large share of the Linux kernel exploits submitted through Google's vulnerability rewards programme used io_uring as the primary bug class. The reported response was operational: **ChromeOS disabled io_uring entirely**, **Android blocks application access via seccomp-BPF** with SELinux confinement to system processes planned, **GKE Autopilot moved to disable it by default**, and use was restricted across Google's internal production infrastructure. Similar defaults appear elsewhere: container runtimes and hardened distribution profiles filter `io_uring_setup`, and io_uring has featured in Linux rootkit proof-of-concept work because it performs I/O without invoking the traditional syscalls that endpoint-detection tooling hooks.

The resulting position is narrow rather than universal. io_uring suits a trusted, performance-critical service that controls its own inputs — a database, a storage engine, a proxy — and is treated elsewhere as a large kernel attack surface: gated behind an explicit seccomp profile, kept away from sandboxed or multi-tenant workloads, and re-checked against the platform's current default, which has been tightening rather than loosening.

## Pitfalls

- **`io_uring_get_sqe` returns NULL when the submission ring is full**, and dereferencing it faults; the fix is to submit and drain completions before claiming more slots, not to enlarge the queue depth.
- **Omitting `io_uring_cqe_seen` leaks completion slots.** The head index never advances, the CQ fills, and further completions are dropped or the ring reports overflow — presenting as work that was submitted but whose results never arrive.
- **Completions are unordered relative to submissions.** Code that assumes the *n*-th CQE corresponds to the *n*-th SQE corrupts state as soon as one operation finishes out of order; the `user_data` tag is the only correlation.
- **A negative `cqe->res` is an errno value, not −1.** Testing `res < 0` and then reading `errno` yields an unrelated stale value; the error is `-cqe->res`.
- **Buffers and file descriptors referenced by an SQE must remain valid until the matching CQE arrives.** A stack buffer that goes out of scope, or a descriptor closed early, is still referenced by an in-flight kernel operation.
- **SQPOLL is not unconditionally syscall-free.** After the configured idle period the poll thread sleeps, and a submitter that never re-checks the ring's wake-up flag queues entries that are never picked up.
- **Kernel version is a weak availability test.** seccomp filters in container runtimes and hardened distributions reject `io_uring_setup` on kernels that fully support io_uring, so setup must be error-checked and a fallback I/O path retained.
