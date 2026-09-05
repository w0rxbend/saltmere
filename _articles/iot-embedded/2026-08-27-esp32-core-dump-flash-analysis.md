---
title: "Post-Mortem Debugging on the ESP32: Core Dumps to Flash and esp-coredump Analysis"
date: 2026-08-27
track: iot-embedded
summary: "ESP-IDF's core dump component snapshots the crashed task's registers, every task's stack, and optionally chosen data sections into a dedicated flash partition from inside the panic handler, so a device that crashes in the field carries its own evidence across the reboot. This article walks the write path on panic, the partition sizing arithmetic, what the ELF format adds over the legacy binary format, and how esp-coredump plus the GNU Debugger (GDB) reconstruct a full backtrace from the stored blob."
reading_time: 7
tags: [esp32, esp-idf, core-dump, debugging, gdb, flash]
sources:
  - title: "Core Dump — ESP-IDF Programming Guide (stable)"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/core_dump.html"
  - title: "Core Dump — ESP-IDF Programming Guide v5.0"
    url: "https://docs.espressif.com/projects/esp-idf/en/v5.0/esp32/api-guides/core_dump.html"
  - title: "espressif/esp-coredump — GitHub"
    url: "https://github.com/espressif/esp-coredump"
  - title: "Fatal Errors (panic handler) — ESP-IDF Programming Guide"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/fatal-errors.html"
---

**Gist.** A crash on a deployed ESP32 normally evaporates: the panic handler prints a backtrace to a universal asynchronous receiver-transmitter (UART) nobody is listening to, then the chip reboots. ESP-IDF's core dump component instead serialises the crashed task's registers, a snapshot of every FreeRTOS task's stack and task control block (TCB), and optionally selected data sections into a dedicated flash partition before the reset, and the `esp-coredump` tool later reassembles that blob with the application's Executable and Linkable Format (ELF) file into a symbolised backtrace or a live GNU Debugger (GDB) session. The cost is a reserved flash partition sized for the worst-case task set, one flash write per crash, and panic-handler code that must itself run correctly on a machine that has already failed.

## The write path on panic

When a fatal error reaches the panic handler — an unhandled exception, a failed `assert`, an interrupt-watchdog timeout with panics enabled — the handler runs with the scheduler stopped and interrupts disabled. With `CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH` set, it walks the FreeRTOS task list and writes, per task, the TCB and the used portion of the stack; **the crashed task's registers and stack are always captured**, and the remaining tasks are snapshotted in priority order up to `CONFIG_ESP_COREDUMP_MAX_TASKS_NUM`. The destination is a partition of type `data`, subtype `coredump`, declared in the partition table:

```
# Name,     Type, SubType,  Offset, Size
nvs,        data, nvs,      0x9000, 0x6000
phy_init,   data, phy,      0xf000, 0x1000
factory,    app,  factory,  0x10000, 1M
coredump,   data, coredump, ,       64K
```

Two details of this path are load-bearing. First, **the dump is written by code that cannot trust the crashed task's stack**: the component supports a dedicated core-dump stack sized by `CONFIG_ESP_COREDUMP_STACK_SIZE` (with 0 meaning the interrupt stack is reused), and Espressif's documentation recommends a value larger than 1300 bytes to avoid overflowing it during the dump itself. A too-small dedicated stack turns one crash into a second, unrecorded one. Second, the blob carries an integrity checksum — cyclic redundancy check (CRC32), or SHA-256 when the ELF format is selected — so a dump truncated by power loss mid-write is detected at read time rather than decoded into a fictional backtrace.

External pseudo-static RAM (PSRAM) contents are **not** included in the dump, so a task whose stack or data lives in external RAM leaves a hole in the evidence.

## Sizing the partition: flash cost versus diagnostic depth

The partition must hold the worst-case dump, and the documented arithmetic is direct: roughly **20 bytes of header overhead plus, per task, 12 bytes plus the TCB size plus the maximum stack size**. A firmware with a dozen tasks at 4 KiB stacks each therefore budgets on the order of 50 KiB before optional extras. Three knobs trade flash for depth:

- `CONFIG_ESP_COREDUMP_MAX_TASKS_NUM` caps how many tasks are snapshotted. Cutting it shrinks the partition but can drop exactly the task that held the wedged mutex.
- `CONFIG_ESP_COREDUMP_CAPTURE_DRAM` additionally captures internal data RAM sections (`.bss`, `.data`, heap regions), which multiplies dump size but lets GDB read global state, not only stacks.
- The `COREDUMP_DRAM_ATTR`, `COREDUMP_RTC_ATTR`, and `COREDUMP_RTC_FAST_ATTR` attributes mark **individual variables** for inclusion, a middle path: a ring buffer of recent events or a state-machine variable travels with the dump without paying for the whole heap.

Flash wear is the quieter half of the trade-off. A crash costs one erase-and-write cycle of the partition, which is negligible for a device that crashes occasionally; a device stuck in a crash loop rewrites the same sectors on every boot cycle. The component keeps only the most recent dump — a new panic overwrites the previous one — so a transient first crash followed by a secondary crash during recovery leaves only the second dump as evidence.

## ELF versus the legacy binary format

The component writes one of two formats, selected under "Component config → Core dump → Core dump data format". The **legacy binary format** is a compact custom layout: smaller, cheaper to produce, retained for backward compatibility. The **ELF format** stores the same crash as a standard ELF core file — the format desktop GDB has consumed for decades — and the documentation describes it as carrying extended information about broken tasks and crashed software at the cost of more flash space, and as the recommended choice for new designs. Two consequences follow:

- **SHA-256 integrity checking works only with the ELF format**; the binary format is limited to CRC32.
- An ELF core file is a self-describing container of memory segments with load addresses, so the analysis side degrades gracefully: standard tooling can map any captured region, including the DRAM sections and attribute-marked variables above, rather than only the fields the custom parser knows about.

The weaker true statement is worth making explicitly: Espressif's documentation records *what* each format stores and which is recommended, not a byte-level accounting of the difference, so partition budgeting should be validated by producing a real dump from the actual task set rather than computed from the format choice alone.

## From blob to backtrace: esp-coredump and GDB

`esp-coredump` is a standalone Python utility (published on the Python Package Index as `esp-coredump`, and bundled with ESP-IDF, where `idf.py` wraps it) with two commands:

```sh
# Symbolised report: crashed task's registers, per-task backtraces,
# task list, captured memory regions. Reads the dump over the serial
# port directly from the flash partition.
idf.py coredump-info

# Convert the stored dump into an ELF core file and open GDB on it,
# paired with the build's application ELF.
idf.py coredump-debug
```

Underneath, `info_corefile` prints the crashed task's registers, call stack, the list of tasks, and the memory contents stored in the dump (TCBs and stacks); `dbg_corefile` creates the core ELF and launches a GDB session against it. Inside that session the full post-mortem repertoire applies: `bt` on the faulting task, `thread apply all bt` across every captured task, `p` on any variable that landed in a captured region. **The analysis requires the exact ELF built alongside the flashed binary** — symbol addresses come from the ELF, not the dump — so the build artefacts for every released firmware version must be archived with the release, or field dumps from older firmware become undecodable.

The dump can also leave the device without a cable at crash time. The UART destination Base64-encodes the dump into the boot log; the flash destination decouples capture from retrieval entirely, so firmware can read the partition after reboot (checking whether a valid dump is present) and ship the blob over the network to a server where `esp-coredump` runs against the archived ELF. That is the configuration that makes the mechanism a fleet instrument rather than a bench convenience: the device reboots, resumes service, and the backtrace arrives with the next telemetry upload.

## Pitfalls

- Analysing a dump against an ELF from a different build silently produces a wrong backtrace, because symbolisation uses the ELF's addresses; only archiving the ELF of every released image keeps old field dumps decodable.
- A `coredump` partition sized below the worst-case task set truncates the dump on the crash that involves the most tasks — typically the interesting one.
- A dedicated core-dump stack smaller than the recommended ~1300 bytes overflows during the dump, so the crash that most needed recording is the one that corrupts its own record.
- A crash loop erases and rewrites the same flash sectors every cycle, and each new panic overwrites the previous dump, so the first crash of a cascade — usually the root cause — is lost.
- Tasks beyond `CONFIG_ESP_COREDUMP_MAX_TASKS_NUM` are absent from the dump, so a deadlock partner holding the lock can be invisible while the blocked task is fully captured.
- Data in external PSRAM is not captured, so moving large buffers or task stacks to PSRAM removes them from post-mortem visibility.
- SHA-256 integrity checking is unavailable in the legacy binary format; a binary-format dump corrupted in a way CRC32 misses decodes without complaint.
