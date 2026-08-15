---
title: "Async embedded Rust on the ESP32: Embassy + esp-hal"
date: 2026-07-26
track: iot-embedded
summary: "A no_std tour of the esp-rs stack — esp-hal plus the Embassy async executor — for event-driven sensor nodes: toolchain constraints imposed by the Xtensa backend, a minimal await-on-a-timer task, and a comparison with ESP-IDF/FreeRTOS."
reading_time: 6
tags: [esp32, rust, embassy, esp-hal, async, no_std, espflash, espup]
sources:
  - title: "esp-hal — no_std HALs for ESP32 (esp-rs)"
    url: "https://github.com/esp-rs/esp-hal"
  - title: "The Rust on ESP Book — Toolchain"
    url: "https://docs.espressif.com/projects/rust/book/getting-started/toolchain.html"
  - title: "Embassy — modern async embedded framework"
    url: "https://embassy.dev/"
  - title: "The Rust on ESP Book — espflash"
    url: "https://docs.espressif.com/projects/rust/book/tooling/espflash.html"
  - title: "espup — Espressif Rust toolchain installer"
    url: "https://github.com/esp-rs/espup"
---

**Gist.** An air-quality sensor node spends nearly all of its wall-clock time waiting — for a timer, for an inter-integrated-circuit (I2C) transaction, for a network publish to complete — and a superloop or a real-time operating system (RTOS) charges either busy-polling or one preallocated stack per concurrent activity for that waiting. The `esp-rs` stack answers with **`esp-hal`**, a bare-metal (`no_std`) hardware abstraction layer, plus **Embassy**, a `no_std` async executor that multiplexes cooperatively scheduled `async fn` tasks **on a single stack**, suspending the whole set when every task is awaiting. The cost is cooperative scheduling — a task that never reaches an `.await` starves the others — and an ecosystem that is younger and less driver-complete than ESP-IDF, with the Embassy initialisation entry point still moving between releases.

## The no_std picture

Two crates carry the weight.

**`esp-hal`** is a bare-metal hardware abstraction layer (HAL) maintained by the `esp-rs` project, on its 1.x line. It requires no RTOS, no C, and no `libc`; it drives the peripheral registers directly. Coverage spans both instruction-set families in the ESP32 line: the **Xtensa** parts (ESP32, ESP32-S2, ESP32-S3) and the **RISC-V** parts (ESP32-C2/C3/C5/C6/C61, ESP32-H2, ESP32-P4).

**Embassy** supplies the async executor together with a timer and HAL ecosystem, also `no_std`. Tasks are written as `async fn` and suspend at `.await` points on timers and peripherals.

The structural argument for async here is that an event-driven node decomposes into independent "wait, then perform a short burst of work" loops. Each loop becomes a task. **While one task awaits a two-second `Timer`, the executor either runs a ready task or, when none is ready, has no work to poll** — no busy-polling loop, and no per-task stack sizing or priority assignment of the kind FreeRTOS requires. Concurrency is obtained without threads. Rust's borrow-checking rules continue to apply across the interrupt-service-routine (ISR) boundary, so shared state reached from both an ISR and a task must be expressed in a type the compiler accepts rather than by convention.

One moving part deserves explicit flagging rather than confident detail: **as `esp-hal` moved to its 1.x line, the Embassy integration glue has been reshuffled between releases**, with the former `esp-hal-embassy` initialisation path being folded into an `esp-rtos`-style runtime crate. The mechanisms described below are stable; the name of the initialisation call is the element most likely to have changed, so the reliable procedure is to scaffold from a current `esp-rs` template and let `cargo build` surface the import corrections.

## Toolchain: espup, and the Xtensa constraint

The constraint that determines the whole setup is this: **the Xtensa targets are not upstream in the Rust compiler.** LLVM's Xtensa backend is absent from stock `rustc`, so the classic ESP32 and the S2/S3 require Espressif's Rust fork, installed by **`espup`**. The RISC-V chips (the -C and -H series) need no fork; they build on plain stable Rust, because their backend is upstream.

| Chip family | Arch | Toolchain | Target triple |
|---|---|---|---|
| ESP32, S2, S3 | Xtensa | esp fork via `espup` | `xtensa-esp32-none-elf` |
| ESP32-C2, C3 | RISC-V | stable `rustup` | `riscv32imc-unknown-none-elf` |
| ESP32-C6, H2 | RISC-V | stable `rustup` | `riscv32imac-unknown-none-elf` |

For a classic ESP32 (Xtensa):

```bash
cargo install espup --locked
espup install                 # installs the esp Rust + LLVM fork
. $HOME/export-esp.sh         # puts the esp fork on PATH; source per shell
cargo install espflash --locked   # flasher and serial monitor
```

For a RISC-V part such as the ESP32-C3, `espup` is not involved:

```bash
rustup toolchain install stable --component rust-src
rustup target add riscv32imc-unknown-none-elf
cargo install espflash --locked
```

The `. $HOME/export-esp.sh` step is per-shell state, not a persistent installation record: a shell that has not sourced it resolves `cargo` to the stock toolchain, which has no Xtensa target.

## A minimal Embassy task

The following node runs two concurrent activities: `main` toggles a status light-emitting diode (LED) on a 500 ms cadence, and a spawned task wakes every 2 s to sample a sensor. Both suspend through `embassy_time::{Timer, Duration}`, so neither blocks the other. Initialisation and module paths on this stack drift between releases; the listing shows the shape rather than a pinned copy.

```rust
#![no_std]
#![no_main]

use embassy_executor::Spawner;
use embassy_time::{Duration, Timer};
use esp_hal::{
    clock::CpuClock,
    gpio::{Level, Output, OutputConfig},
    timer::timg::TimerGroup,
};
use esp_backtrace as _;      // panic + backtrace handler
use esp_println::println;    // println! over the UART

#[esp_hal_embassy::main]
async fn main(spawner: Spawner) {
    let config = esp_hal::Config::default().with_cpu_clock(CpuClock::max());
    let peripherals = esp_hal::init(config);

    // Handing a hardware timer to the Embassy runtime installs the async time
    // driver that backs Timer. (Init entry point varies by release.)
    let timg0 = TimerGroup::new(peripherals.TIMG0);
    esp_hal_embassy::init(timg0.timer0);

    spawner.spawn(sensor_task()).unwrap();

    // The LED loop suspends at .await rather than spinning in a busy delay.
    let mut led = Output::new(peripherals.GPIO2, Level::Low, OutputConfig::default());
    loop {
        led.toggle();
        Timer::after(Duration::from_millis(500)).await;
    }
}

#[embassy_executor::task]
async fn sensor_task() {
    let mut n = 0u32;
    loop {
        Timer::after(Duration::from_secs(2)).await;   // yields the CPU
        // Replace with a real I2C read, e.g. an SEN5x particulate sensor.
        println!("sample {n}: pm2.5 = ... ug/m3");
        n += 1;
    }
}
```

The load-bearing line is `esp_hal_embassy::init(timg0.timer0)`: **the executor's notion of time is backed by a concrete hardware timer group**, so `Timer::after` resolves to a hardware deadline rather than a counted delay loop. `main` is itself a task; its `loop` returns control at each `.await`, which is the only point at which `sensor_task` can be resumed.

`espflash` is wired in as the Cargo runner through `.cargo/config.toml`:

```toml
[target.'cfg(any(target_arch = "riscv32", target_arch = "xtensa"))']
runner = "espflash flash --monitor"
```

With that runner in place, `cargo run --release` builds, flashes over universal serial bus (USB), and opens a serial monitor carrying the `println!` output. The equivalent direct invocation is `espflash flash --monitor target/xtensa-esp32-none-elf/release/my-app`.

## Comparison with ESP-IDF / FreeRTOS

| | esp-hal + Embassy (Rust) | ESP-IDF + FreeRTOS (C) |
|---|---|---|
| Runtime | `no_std`, no RTOS required | FreeRTOS tasks + IDF services |
| Concurrency | async tasks on one stack | preemptive threads, per-task stacks |
| Waiting | `.await` a timer or peripheral | block a task or poll |
| Memory safety | compiler-enforced | manual |
| Wi-Fi / Bluetooth Low Energy | via esp-rs radio crates (`no_std`) | first-party, long-established |
| Maturity | fast-moving, 1.x | long-established, large ecosystem |

The trade-off is asymmetric by area rather than uniform. ESP-IDF is the incumbent with the deepest driver and connectivity coverage, and a design that depends on the full networking stack is better served there. For a battery-oriented sensor node whose duty cycle is dominated by waiting, the await-and-suspend model matches the workload's structure more closely than a superloop, and the compiler rejects a class of ISR/shared-state aliasing errors before the board is powered.

**Extension.** Replacing the `println!` stub with an SEN5x read over `esp-hal`'s async I2C exercises the peripheral-await path rather than only the timer path; adding a second task that debounces the BOOT button through `.await` introduces an event source that is not timer-driven.

## Pitfalls

- **A task that computes without reaching an `.await` stops every other task.** Scheduling is cooperative on a single stack, so a long blocking loop inside one task is not preempted; the LED cadence visibly stalls.
- **A blocking delay primitive defeats the model.** Spinning in a busy delay rather than awaiting a `Timer` keeps the executor from finding an idle point, so nothing else runs and the core does not become idle.
- **Building for the ESP32/S2/S3 in a shell that has not sourced `export-esp.sh` fails at target resolution**, because stock `rustc` has no Xtensa backend; the symptom is an unknown-target error rather than a compile error in the code.
- **Copying an initialisation snippet across `esp-hal` releases breaks the build**, since the Embassy integration path has been reshuffled during the 1.x transition — the failure appears as an unresolved import or missing function, not as misbehaviour at runtime.
- **Omitting a panic handler crate such as `esp_backtrace` fails the link in `no_std`**, where no default handler exists; the `use esp_backtrace as _;` line is load-bearing despite naming nothing.
- **Sharing state between an ISR and a task through a plain mutable static does not compile**, and substituting an `unsafe` block to force it reintroduces exactly the data race the type system was rejecting.
