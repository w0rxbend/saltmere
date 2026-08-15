---
title: "An E-Paper Air-Quality Dashboard on the ESP32: Zero Watts Between Updates"
date: 2026-08-15
track: iot-embedded
summary: "An e-paper panel holds its image with the power off, which makes it the display that fits a deep-sleeping battery node. Choosing between 2.9\" SSD1680 and 4.2\" UC8176/SSD1683-class panels, wiring for the GxEPD2 library, full vs partial refresh trade-offs, and a wake-fetch-render-hibernate loop that spends microamps between updates."
reading_time: 6
tags: [e-paper, esp32, gxepd2, deep-sleep, air-quality, ssd1680, low-power]
sources:
  - title: "ZinggJM/GxEPD2 — Arduino display library for SPI e-paper displays"
    url: "https://github.com/ZinggJM/GxEPD2"
  - title: "2.9inch e-Paper Module — Waveshare Wiki"
    url: "https://www.waveshare.com/wiki/2.9inch_e-Paper_Module"
  - title: "Waveshare 2.9\" e-Paper V2 panel specification (PDF)"
    url: "https://files.waveshare.com/upload/7/79/2.9inch-e-paper-v2-specification.pdf"
  - title: "GDEY029T94 — 2.9\" 296x128 fast-refresh e-paper, Good Display"
    url: "https://www.good-display.com/product/389.html"
---

**Gist.** A liquid-crystal display (LCD) or organic light-emitting diode (OLED) panel draws current continuously to hold an image, which contradicts a battery node that sleeps between five-minute measurements. Electrophoretic **e-paper** is *bistable*: once pigment particles are driven to black or white they remain in place with supply removed, so the panel consumes energy only during a refresh and nothing while the image is displayed. The cost is refresh latency measured in seconds rather than milliseconds, and a choice between a flashing full refresh that clears ghosting and a fast partial refresh that accumulates it.

The persistence property is what makes e-paper the companion to the deep-sleep duty cycling described in the [months-on-a-LiPo article](/articles/iot-embedded/2026-07-26-esp32-deep-sleep-power/): the reading stays legible for the entire sleep interval at no energy cost.

## Panel selection

The commonly available serial peripheral interface (SPI) modules come from **Waveshare** and **Good Display**; Waveshare modules commonly carry Good Display glass mounted on a breakout carrying level shifting and the boost circuit that generates the panel's drive rails. Three sizes cover most dashboard use:

| Panel | Resolution | Controller | Full refresh | Partial refresh |
|---|---|---|---|---|
| 2.9" B/W (e.g. GDEY029T94 / Waveshare 2.9" V2) | 296×128 | SSD1680 | ~2 s | ~0.3 s |
| 4.2" B/W (GDEW042T2, EOL → GDEQ042T81) | 400×300 | UC8176 → SSD1683 | several seconds | supported on newer glass |
| 7.5" B/W | 800×480 | UC8179-class | several seconds | limited |

Black/white glass is the appropriate choice for a numeric dashboard. **Three-colour red or yellow panels are specified at refresh times on the order of 15 seconds and offer no partial mode**; the third pigment is driven by additional waveform phases that separate it from the other two. The 4.2" line has churned: the UC8176-based GDEW042T2 is end-of-life and later parts such as the GDEQ042T81 use the SSD1683, so **the driver class depends on the revision of the board in hand, not on the nominal size**.

## GxEPD2 and wiring

**GxEPD2** is the widely used Arduino library for these modules. Its structure has three load-bearing properties: one driver class per panel, so the waveform and command set are selected at compile time; `Adafruit_GFX` as the drawing surface; and **paged rendering**, which lets a 400×300 image be produced in horizontal bands so the full framebuffer never has to be resident in random-access memory (RAM). Paged rendering is why the drawing code sits inside a `firstPage()`/`nextPage()` loop and is executed once per band.

Wiring is SPI plus three control lines:

| Module pin | ESP32 GPIO | Role |
|---|---|---|
| VCC | 3V3 | 3.3 V supply |
| GND | GND | ground |
| DIN | 23 (MOSI) | SPI data |
| CLK | 18 (SCK) | SPI clock |
| CS | 5 | chip select |
| DC | 17 | data/command |
| RST | 16 | reset |
| BUSY | 4 | refresh-in-progress flag |

**BUSY is the line that constrains the design.** A refresh occupies the controller for seconds and the library polls BUSY for its duration, so the pin cannot be shared with another peripheral: a second device driving it produces either a premature return from the refresh or a poll that never terminates.

## Full and partial refresh

A **full refresh** drives every pixel through an inversion waveform — the visible black/white flashing — and clears **ghosting**, the faint residue of the previously displayed image. It takes seconds. A **partial refresh** rewrites only a defined window without the flash, in roughly 0.3 s on the SSD1680, but each partial cycle leaves residue behind, and the partial waveform exercises the pigment less thoroughly than the full one.

The resulting policy is a ratio, not a rule of physics: partial refreshes for routine value updates, a full refresh at some interval (every ten partials, or hourly) to wipe accumulated ghosting. **The tolerable number of consecutive partials varies by panel and must be measured on the specific glass**, since ghosting is a visual threshold rather than a documented count.

A node waking every five minutes need not optimise this at all. One full refresh per wake costs roughly 2 s of panel activity out of a 300 s cycle, and the flash occurs while the node is unobserved.

The state that must be maintained across all of this is simple: **the panel must never be left mid-waveform with power removed**. An interrupted refresh leaves pigment partially driven, which shows as a smeared image that only a subsequent full refresh clears.

## The wake-render-hibernate loop

The firmware is a straight line rather than a `loop()`, because the ESP32 restarts from the reset vector after each deep-sleep wake; only variables marked `RTC_DATA_ATTR`, which live in real-time clock (RTC) retention memory, survive the cycle.

```cpp
#include <GxEPD2_BW.h>
#include <Fonts/FreeSansBold18pt7b.h>

GxEPD2_BW<GxEPD2_290_GDEY029T94, GxEPD2_290_GDEY029T94::HEIGHT>
  display(GxEPD2_290_GDEY029T94(/*CS=*/5, /*DC=*/17, /*RST=*/16, /*BUSY=*/4));

RTC_DATA_ATTR uint32_t wakes = 0;    // survives deep sleep in RTC memory

void setup() {
  float pm25, co2;
  fetchReadings(&pm25, &co2);          // MQTT or HTTP GET, ~3 s on Wi-Fi

  display.init(0, wakes == 0);         // full init only on first boot
  display.setRotation(1);
  display.setFont(&FreeSansBold18pt7b);
  display.setFullWindow();
  display.firstPage();
  do {                                 // body runs once per page band
    display.fillScreen(GxEPD_WHITE);
    display.setCursor(8, 40);
    display.printf("PM2.5  %.1f", pm25);
    display.setCursor(8, 90);
    display.printf("CO2    %.0f", co2);
  } while (display.nextPage());

  display.hibernate();                 // deep-sleep the panel controller
  wakes++;
  esp_sleep_enable_timer_wakeup(300ULL * 1000000ULL);  // 5 min
  esp_deep_sleep_start();
}
void loop() {}
```

Two lines carry the power behaviour. `display.init(0, wakes == 0)` performs the initial-boot reset sequence only on the first cycle, since `wakes` persists in RTC memory. **`display.hibernate()` places the SSD1680 in its own deep-sleep mode, whose specified current is around a microamp, rather than leaving it in standby**; GxEPD2 brings it back with the RST pulse on the next `init()`. Omitting the call leaves the controller drawing standby current for the entire sleep interval, which on this budget is of the same order as the microcontroller itself.

Because the drawing body executes once per page band, any side effect placed inside it — incrementing a counter, consuming a queue — executes multiple times per refresh.

## The power budget

Per five-minute cycle on a 2.9" panel: deep sleep at ~10 µA (ESP32 plus hibernated panel and a low-dropout regulator with low quiescent current) for 295 s ≈ 0.8 µAh; Wi-Fi fetch at ~80 mA average for 3 s ≈ 67 µAh; refresh at ~8 mA for 2 s ≈ 4 µAh. The total is ~72 µAh per cycle, ~0.9 mAh/h, which draws a 2000 mAh lithium-polymer cell down over roughly three months.

**The radio dominates: the fetch is about 93% of the cycle's charge and the refresh about 6%.** Once the image persists at no cost, the battery budget is a networking problem. Shortening or eliminating the fetch — ESP-NOW instead of a full Wi-Fi association, or batching via [store-and-forward](/articles/iot-embedded/2026-07-31-littlefs-store-and-forward/) — moves the number far more than any panel optimisation. The data source can be the same [SEN5x node](/articles/iot-embedded/2026-07-24-esp32-sen5x-air-quality-mqtt/) already publishing to MQTT.

## Beyond GxEPD2

For large panels and richer layouts, the **epdiy** project drives bare parallel-interface e-paper glass, including salvaged e-reader panels, from ESP-IDF with greyscale waveforms. **LVGL** can target an e-paper flush callback for widget-style dashboards, at the cost of pairing a toolkit designed around continuous redraw with a display whose refresh takes seconds. For a numbers-and-icons air-quality readout, GxEPD2 with stock `Adafruit_GFX` fonts is sufficient machinery.

## Pitfalls

- **BUSY shared with another peripheral.** The library either returns from a refresh before the waveform completes, leaving a smeared image, or polls a line another device holds and blocks indefinitely.
- **`hibernate()` omitted after rendering.** The controller stays in standby for the whole sleep interval, adding a draw of the same order as the ~10 µA sleep budget itself.
- **Side effects inside the `firstPage()`/`nextPage()` body.** Paged rendering executes that block once per band, so a counter incremented there advances several times per refresh.
- **Driver class chosen by panel size.** A 4.2" board may carry UC8176 or SSD1683 depending on revision; the wrong class produces a blank or corrupted image with no error, since the controller has no reply channel to detect the mismatch.
- **Partial refreshes without a scheduled full refresh.** Ghosting accumulates until old digits remain readable behind new ones; only a full inversion waveform clears it.
- **Three-colour panel selected for a frequently updated dashboard.** Refresh takes on the order of 15 seconds and no partial mode exists, so the value on screen is stale for a large fraction of a short update period.
- **Power removed mid-waveform.** Pigment left partially driven shows as a smear that persists until the next full refresh, because bistability preserves the interrupted state as faithfully as a finished one.
- **Non-RTC variables expected to survive sleep.** Deep sleep restarts from the reset vector; anything not marked `RTC_DATA_ATTR` is reinitialised, so a wake counter kept in an ordinary global stays at zero and forces a full init every cycle.
