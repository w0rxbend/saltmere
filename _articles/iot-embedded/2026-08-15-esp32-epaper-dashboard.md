---
title: "An E-Paper Air-Quality Dashboard on the ESP32: Zero Watts Between Updates"
date: 2026-08-15
track: iot-embedded
summary: "An e-paper panel holds its image with the power off, which makes it the only display that fits a deep-sleeping battery node. Choosing between 2.9\" SSD1680 and 4.2\" UC8176/SSD1683-class panels, wiring for the GxEPD2 library (v1.6.9), full vs partial refresh trade-offs, and a wake-fetch-render-hibernate loop that spends microamps between updates."
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

A battery-powered sensor node with an LCD or OLED is a contradiction: the display burns milliamps continuously to show a number that changes every five minutes. **E-paper** breaks the contradiction because the electrophoretic pigment particles are *bistable* — once driven black or white they stay put with the power completely removed. The panel draws energy only while changing the image. For a node that wakes, measures, renders, and sleeps, the display costs literally zero between updates, and the reading stays legible the whole time. That is the perfect companion to the deep-sleep duty cycling from the [months-on-a-LiPo article](/articles/iot-embedded/2026-07-26-esp32-deep-sleep-power/).

## Picking a panel

The hobbyist sweet spot is the SPI modules from **Waveshare** and **Good Display** (Waveshare's panels are largely Good Display glass on a breakout with level shifting and the boost circuit). Two sizes cover most dashboards:

| Panel | Resolution | Controller | Full refresh | Partial refresh | Typical price |
|---|---|---|---|---|---|
| 2.9" B/W (e.g. GDEY029T94 / Waveshare 2.9" V2) | 296×128 | SSD1680 | ~1.5–2 s | ~0.3 s | ~$10 |
| 4.2" B/W (GDEW042T2, EOL → GDEQ042T81) | 400×300 | UC8176 → SSD1683 | ~4 s | supported on newer glass | ~$25 |
| 7.5" B/W | 800×480 | UC8179-class | ~5 s | limited | ~$50 |

Buy black/white. The three-colour red/yellow panels look great on product photos and take 15–20 seconds per refresh with no partial mode — wrong tool for a dashboard. Note the churn in the 4.2" line: the venerable UC8176-based GDEW042T2 is end-of-life and its successors moved to the SSD1683, so check which revision you're holding before picking a driver class.

## GxEPD2 and wiring

**GxEPD2** (v1.6.9 as of April 2026, still actively gaining panels) is the standard Arduino library: one driver class per panel, `Adafruit_GFX` for drawing, paged rendering so a 400×300 framebuffer doesn't have to live in RAM. Wiring is SPI plus three control lines:

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

The BUSY line matters: refreshes take seconds and the library polls it, so don't share the pin.

## Full vs partial refresh

A **full refresh** drives every pixel through an inversion waveform — the black/white flashing you see — which clears **ghosting** but takes seconds. A **partial refresh** rewrites only a window without the flash in ~0.3 s, but each one leaves faint residue of the old image, and the controller's partial waveform stresses the pigment less thoroughly. The working rule: partial refreshes for routine value updates, a full refresh every ~10 partials (or once an hour) to wipe ghosts, and never leave the panel powered mid-waveform. For a node that wakes every five minutes anyway, the simplest robust policy is one full refresh per wake — the flash happens while nobody's watching.

## The wake-render-hibernate loop

The whole firmware is a straight line — no `loop()`, because the ESP32 reboots fresh from deep sleep each cycle:

```cpp
#include <GxEPD2_BW.h>
#include <Fonts/FreeSansBold18pt7b.h>

GxEPD2_BW<GxEPD2_290_GDEY029T94, GxEPD2_290_GDEY029T94::HEIGHT>
  display(GxEPD2_290_GDEY029T94(/*CS=*/5, /*DC=*/17, /*RST=*/16, /*BUSY=*/4));

RTC_DATA_ATTR uint32_t wakes = 0;

void setup() {
  float pm25, co2;
  fetchReadings(&pm25, &co2);          // MQTT or HTTP GET, ~2 s on Wi-Fi

  display.init(0, wakes == 0);         // full init only on first boot
  display.setRotation(1);
  display.setFont(&FreeSansBold18pt7b);
  display.setFullWindow();
  display.firstPage();
  do {
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

`display.hibernate()` is the line people forget: it puts the SSD1680 into its own deep-sleep mode at ~1 µA instead of leaving it in standby at tens of µA, and GxEPD2 wakes it with the RST pulse on the next `init()`.

## The power math

Per five-minute cycle on a 2.9" panel: deep sleep at ~10 µA (ESP32 plus hibernated panel and a decent LDO) for 295 s ≈ 0.8 µAh; Wi-Fi fetch at ~80 mA average for 3 s ≈ 67 µAh; refresh at ~8 mA for 2 s ≈ 4 µAh. Call it ~72 µAh per cycle, ~0.9 mAh/h — a 2000 mAh LiPo runs roughly three months, and the display is *rounding error* next to the radio. That's the real lesson: once the image persists for free, your battery budget is a Wi-Fi problem. Cutting the fetch (ESP-NOW, or batching via [store-and-forward](/articles/iot-embedded/2026-07-31-littlefs-store-and-forward/)) pays off far more than optimising the panel, and the data source can be the same [SEN5x node](/articles/iot-embedded/2026-07-24-esp32-sen5x-air-quality-mqtt/) already publishing to MQTT.

## When you outgrow GxEPD2

For big panels and rich layouts, the **epdiy** project drives bare parallel e-paper glass (salvaged Kindle/e-reader panels included) from ESP-IDF with proper greyscale waveforms, and **LVGL** can target an e-paper flush callback for widget-style dashboards — accepting that a widget toolkit and a 2-second refresh make odd companions. For a numbers-and-icons air-quality display, GxEPD2 plus stock `Adafruit_GFX` fonts is the right amount of machinery.

**Try next:** flash the loop above, then split the layout into a static frame drawn once with a full refresh and a value region updated with `setPartialWindow()` — count how many partial cycles your panel tolerates before ghosting makes you schedule the full-refresh wipe.
