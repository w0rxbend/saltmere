---
title: "Async embedded Rust on the ESP32: Embassy + esp-hal"
date: 2026-07-26
track: iot-embedded
summary: "A no_std tour of the esp-rs stack — esp-hal plus the Embassy async executor — for event-driven sensor nodes. Toolchain setup with espup, a minimal await-on-a-timer task, and how it stacks up against ESP-IDF/FreeRTOS."
reading_time: 5
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

My air-quality nodes spend almost all their time doing nothing: wait for a timer, read a sensor over I2C, publish, sleep. That is the shape async was invented for. On the ESP32 you no longer have to reach for ESP-IDF and FreeRTOS in C to get it — the `esp-rs` project ships a mature bare-metal Rust stack, and it pairs cleanly with the Embassy executor. This is the Rust angle to the same hardware the rest of this track covers.

## The no_std picture

Two crates carry the weight:

- **`esp-hal`** — a bare-metal (`no_std`) hardware abstraction layer maintained by `esp-rs`, on its 1.x line. No RTOS, no C, no `libc` — it talks directly to the peripherals. It covers the whole family: the Xtensa parts (ESP32, ESP32-S2, ESP32-S3) and the RISC-V parts (ESP32-C2/C3/C5/C6/C61, ESP32-H2, ESP32-P4).
- **Embassy** — a `no_std` async executor and timer/HAL ecosystem. You write `async fn` tasks, `.await` on timers and peripherals, and the executor multiplexes them cooperatively on a single stack.

Why async instead of a superloop or a hand-rolled RTOS? An event-driven sensor node is a set of independent "wait, then do a little work" loops. With Embassy each becomes a task that `.await`s. While one task waits on a 2-second `Timer`, the executor runs others or puts the core to sleep — no busy-polling, no manually juggling FreeRTOS task priorities and stack sizes. You get concurrency without threads, and the borrow checker still enforces that your ISR and your task don't race over shared state.

One current-facts note worth flagging: as `esp-hal` moved to 1.x the Embassy integration glue has been reshuffled between releases (the old `esp-hal-embassy` init path is being folded into an `esp-rtos`-style runtime crate). The concepts below are stable; the exact init call name is the thing most likely to have shifted, so scaffold from an up-to-date `esp-rs` template and let `cargo build` guide any import fixes.

## Toolchain: espup, and the Xtensa catch

The one thing to know before you `cargo build`: **the Xtensa targets are not upstream in the Rust compiler.** LLVM's Xtensa backend isn't in stock `rustc`, so for the classic ESP32 and the S2/S3 you install Espressif's Rust fork with **`espup`**. RISC-V chips (the -C and -H series) need no fork — they build on plain stable Rust.

| Chip family | Arch | Toolchain | Target triple |
|---|---|---|---|
| ESP32, S2, S3 | Xtensa | esp fork via `espup` | `xtensa-esp32-none-elf` |
| ESP32-C2, C3 | RISC-V | stable `rustup` | `riscv32imc-unknown-none-elf` |
| ESP32-C6, H2 | RISC-V | stable `rustup` | `riscv32imac-unknown-none-elf` |

For a classic ESP32 (Xtensa):

```bash
cargo install espup --locked
espup install                 # installs the esp Rust + LLVM fork
. $HOME/export-esp.sh         # put the esp fork on PATH (source per shell)
cargo install espflash --locked   # the flasher/monitor
```

For a RISC-V part like the ESP32-C3, skip `espup` entirely:

```bash
rustup toolchain install stable --component rust-src
rustup target add riscv32imc-unknown-none-elf
cargo install espflash --locked
```

## A minimal Embassy task

Here is a self-contained node: main blinks a status LED on a 500 ms cadence, while a spawned task wakes every 2 seconds to sample a sensor. Both use `embassy_time::{Timer, Duration}` and neither blocks the other. (Init/module paths on this fast-moving stack drift between releases — treat this as the shape, not a pinned copy-paste.)

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

#[esp_hal::main]
async fn main(spawner: Spawner) {
    let config = esp_hal::Config::default().with_cpu_clock(CpuClock::max());
    let peripherals = esp_hal::init(config);

    // Hand a timer to the Embassy runtime: this installs the async time
    // driver and starts the executor. (Init entry point varies by release.)
    let timg0 = TimerGroup::new(peripherals.TIMG0);
    esp_hal_embassy::init(timg0.timer0);

    spawner.spawn(sensor_task()).unwrap();

    // Drive the on-board LED from main; await instead of a busy delay.
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

Wire `espflash` in as the Cargo runner in `.cargo/config.toml`:

```toml
[target.'cfg(any(target_arch = "riscv32", target_arch = "xtensa"))']
runner = "espflash flash --monitor"
```

Now `cargo run --release` builds, flashes over USB, and drops you into a serial monitor showing the `println!` output. (Or invoke it directly: `espflash flash --monitor target/xtensa-esp32-none-elf/release/my-app`.)

## How it compares to ESP-IDF / FreeRTOS

| | esp-hal + Embassy (Rust) | ESP-IDF + FreeRTOS (C) |
|---|---|---|
| Runtime | `no_std`, no RTOS required | FreeRTOS tasks + IDF services |
| Concurrency | async tasks on one stack | preemptive threads, per-task stacks |
| Waiting | `.await` a timer/peripheral | block a task or poll |
| Memory safety | compiler-enforced | manual |
| Wi-Fi / BLE stack | via esp-rs radio crates (`no_std`) | mature, first-party |
| Maturity | fast-moving, 1.x | battle-tested, huge ecosystem |

The honest trade-off: ESP-IDF is the incumbent with the deepest driver and connectivity support, and if you need the full networking stack it is still the safer bet. But for a battery-minded sensor node, Embassy's await-and-sleep model is a better structural fit than a superloop, and Rust's guarantees remove a whole class of ISR/shared-state bugs before the board even powers on.

**Try next:** swap the `println!` stub for a real `embassy`-friendly I2C read — bring up an SEN5x over `esp-hal`'s async I2C in `sensor_task`, and add a second task that debounces the BOOT button with `.await` so a press forces an immediate sample.
