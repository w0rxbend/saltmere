---
title: "A dB(A) Noise Node: INMP441 I2S Microphone on the ESP32"
date: 2026-08-15
track: iot-embedded
summary: "An I2S MEMS mic like the INMP441 turns an ESP32 into a continuous sound-level meter for a few dollars: read 24-bit samples out of 32-bit slots, run an A-weighting IIR cascade, integrate Leq over 125 ms windows, and anchor absolute dB SPL on the mic's -26 dBFS sensitivity spec."
reading_time: 6
tags: [inmp441, i2s, esp32, sound-level, dba, esp-idf, noise]
sources:
  - title: "INMP441 Datasheet — Omnidirectional MEMS Microphone with I2S Output (InvenSense/TDK)"
    url: "https://www.farnell.com/datasheets/1824785.pdf"
  - title: "ikostoski/esp32-i2s-slm — Sound Level Meter with ESP32 and I2S MEMS microphone"
    url: "https://github.com/ikostoski/esp32-i2s-slm"
  - title: "ESP32-I2S-SLM project log — Hackaday.io"
    url: "https://hackaday.io/project/166867-esp32-i2s-slm"
  - title: "I2S Driver (i2s_std) — ESP-IDF Programming Guide"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/peripherals/i2s.html"
  - title: "stas-sl/esphome-sound-level-meter — Leq/A-weighting as an ESPHome component"
    url: "https://github.com/stas-sl/esphome-sound-level-meter"
---

An air-quality station tells you the room is stuffy; a noise channel tells you *why you can't sleep in it*. Traffic, HVAC, the neighbour's heat pump — LAeq trends correlate with windows-open decisions just like CO2 does, and the sensor is a three-dollar board. The INMP441 (and its better-calibrated sibling, the ICS-43434) is a MEMS microphone with the ADC already inside: it speaks I2S, so the ESP32 clocks in finished 24-bit samples over DMA and no analog front end exists to get wrong.

## The mic, honestly specced

From the INMP441 datasheet: sensitivity **-26 dBFS** at 94 dB SPL / 1 kHz, SNR **61 dBA**, equivalent input noise **33 dBA SPL**, flat response **60 Hz–15 kHz**. Two consequences. First, the noise floor means readings below ~35 dB(A) are the mic measuring itself — fine for "how loud is the street", useless for a recording studio. Second, sensitivity tolerance is **±3 dB** on the INMP441 versus **±1 dB** on the ICS-43434, which is why Ivan Kostoski's reference project recommends the ICS-43434 when you care about absolute accuracy. Wiring is five lines: VDD (3.3 V), GND, SCK, WS, SD — plus L/R tied to GND to put mono data in the left slot.

## I2S std mode and the 24-in-32 gotcha

On ESP-IDF 5+ the old `i2s_driver_install` API is replaced by channel-based drivers; standard Philips mode lives in `driver/i2s_std.h`. The one thing everyone trips over: the INMP441 ships **24-bit data MSB-aligned inside a 32-bit slot** (the datasheet specifies 64 SCK cycles per stereo frame — 32 per word). So you configure 32-bit slots, receive `int32_t`, and arithmetic-shift right by 8 to recover a signed 24-bit sample. Skip the shift and your levels are off by 48 dB and full of garbage in the low byte.

```c
#include "driver/i2s_std.h"
#include <math.h>

#define SAMPLE_RATE 48000
#define BLOCK 1024   // ~21 ms per read at 48 kHz

static i2s_chan_handle_t rx;

void mic_init(void) {
    i2s_chan_config_t ch = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    i2s_new_channel(&ch, NULL, &rx);

    i2s_std_config_t std = {
        .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
                        I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = { .bclk = GPIO_NUM_23, .ws = GPIO_NUM_18,
                      .din = GPIO_NUM_19, .mclk = I2S_GPIO_UNUSED },
    };
    std.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;   // L/R pin -> GND
    i2s_channel_init_std_mode(rx, &std);
    i2s_channel_enable(rx);
}

// RMS of one block -> dB SPL (Z-weighted; A-weighting goes before this)
float read_block_db(void) {
    static int32_t raw[BLOCK];
    size_t n = 0;
    i2s_channel_read(rx, raw, sizeof(raw), &n, portMAX_DELAY);

    double sum = 0;
    static double dc = 0;
    for (size_t i = 0; i < n / 4; i++) {
        double s = (double)(raw[i] >> 8);         // 24-bit sample in 32-bit slot
        dc += (s - dc) * 1e-4;                    // one-pole DC removal
        s -= dc;
        sum += s * s;
    }
    double rms = sqrt(sum / (n / 4));

    // -26 dBFS @ 94 dB SPL: full scale 2^23-1 -> 94 dB tone has
    // peak amplitude 8388607 * 10^(-26/20) ~= 420426 counts.
    const double ref_rms = 420426.0 / M_SQRT2;
    return 94.0 + 20.0 * log10(rms / ref_rms);
}
```

The calibration constant is the whole trick: a 94 dB SPL sine should read `-26 dBFS`, i.e. a peak amplitude of about **420,426** counts in 24-bit data. Anchor on that and the dB numbers become absolute SPL, not arbitrary dBFS — within the mic's ±3 dB part tolerance, anyway. A $20 calibrator (or a phone SLM app held next to the board, for the unfussy) tightens that to ~1 dB.

## From dB(Z) to dB(A)

The code above is unweighted ("Z"). Regulations and every published noise figure use **A-weighting** — a standardized curve that discounts the low frequencies human ears discount. Digitally it's an IIR filter, and the clean implementation is a cascade of **second-order sections (biquads)** run in single precision per sample block. The esp32-i2s-slm project ships exactly this: one biquad cascade equalizing the microphone's own response, then a three-stage A-weighting cascade with coefficients designed for 48 kHz (the MATLAB used to derive them is in the repo — half the repo by volume, usefully). Filtering 1024 samples between DMA reads costs single-digit milliseconds on a 240 MHz ESP32; the FPU makes FFT-based weighting unnecessary.

Then integrate: **Leq** is just RMS-over-a-window expressed in dB. The standard fast meter response is a **125 ms** window (LAeq,125ms) — that's your "live" bar. Accumulate the same sum-of-squares for 1 s (LAeq,1s) for publishing, or 15 min for the neighborhood-noise number that actually means something. Because you accumulate energy (squares), longer windows are free: keep one running sum, snapshot at each horizon.

How good can this get? Kostoski compared the ICS-43434 build against a Brüel & Kjær 2250 class-1 meter and got agreement across roughly 35–116 dB SPL, with a theoretical **±1 dB(A)** over 20 Hz–20 kHz for the factory-calibrated mic. Not a legal instrument — microphone aging, enclosure acoustics and wind all go unhandled — but easily good enough for trend data. If you'd rather not write the firmware, stas-sl's ESPHome external component packages the same DSP; this article is for the node you build yourself.

## Fitting it into the station

Publish `laeq_1s` (or 1-min aggregates) next to PM2.5 and CO2 — MQTT discovery from the [previous article](/articles/iot-embedded/2026-08-15-home-assistant-mqtt-discovery-esp32/) makes it one more `cmps` entry with `unit_of_meas: "dB"`. Two integration notes: keep the mic port outside any sealed enclosure (a foam-plugged hole works), and don't share the I2S pins with anything — the 3.072 MHz bit clock radiates into sloppy wiring. Privacy is a design choice, not an accident: compute Leq on-device and publish only the number, never raw audio, and the node is a noise meter rather than a bug.

**Try next:** port the A-weighting biquads from esp32-i2s-slm into `read_block_db()`, play a 1 kHz tone at a known level, and check that A- and Z-weighted readings agree at 1 kHz (A-weighting is 0 dB there) but diverge hard on a 100 Hz tone.
