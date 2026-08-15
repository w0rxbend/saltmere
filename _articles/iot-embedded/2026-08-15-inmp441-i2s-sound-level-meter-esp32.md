---
title: "A dB(A) Noise Node: INMP441 I2S Microphone on the ESP32"
date: 2026-08-15
track: iot-embedded
summary: "An inter-IC sound (I2S) micro-electro-mechanical (MEMS) microphone such as the INMP441 turns an ESP32 into a continuous sound-level meter: 24-bit samples arrive MSB-aligned in 32-bit slots, an A-weighting IIR cascade shapes them, Leq integrates over 125 ms windows, and the mic's -26 dBFS sensitivity anchors absolute dB SPL."
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

**Gist.** A noise channel answers a question a CO2 channel cannot: which of traffic, heating-ventilation-and-air-conditioning (HVAC) plant or a neighbour's heat pump is driving the room's sound level, and when. The mechanism is a micro-electro-mechanical-systems (MEMS) microphone with the analogue-to-digital converter (ADC) already on die, emitting inter-IC sound (I2S) frames that the ESP32 clocks in over direct memory access (DMA); firmware then applies an A-weighting infinite-impulse-response (IIR) cascade and integrates equivalent continuous level (Leq) over fixed windows. The cost is accuracy that is bounded by the part's sensitivity tolerance and its own noise floor, not by the arithmetic: on the INMP441 that tolerance is **±3 dB**, and levels below roughly 35 dB(A) are the microphone measuring itself.

## The part, as specified

The INMP441 datasheet gives sensitivity **-26 dBFS** at 94 dB sound pressure level (SPL) and 1 kHz, signal-to-noise ratio **61 dBA**, equivalent input noise **33 dBA SPL**, and a flat response over **60 Hz–15 kHz**. Two consequences follow directly. The equivalent input noise sets a floor: readings near or below the mid-30s dB(A) are dominated by the sensor, which suits street-level trend data and does not suit a quiet room. The sensitivity tolerance sets absolute accuracy: **±3 dB** part to part, which no amount of arithmetic recovers. Ivan Kostoski's reference project recommends the ICS-43434 instead where absolute accuracy matters, its sensitivity tolerance being the tighter of the two.

Wiring is five lines — VDD (3.3 V), GND, SCK, WS, SD — with the L/R pin tied to GND so the mono word lands in the left slot. Every subsequent decision in firmware depends on that slot choice.

## I2S standard mode and the 24-in-32 alignment

On ESP-IDF 5 and later the older `i2s_driver_install` interface is superseded by channel-based drivers, and standard Philips mode lives in `driver/i2s_std.h`. The alignment detail is the one that silently corrupts levels: the INMP441 emits **24-bit data MSB-aligned inside a 32-bit slot**, the datasheet specifying **64 SCK cycles per stereo frame**, thirty-two per word. The slot width is therefore configured as 32 bits, samples are received as `int32_t`, and each is arithmetic-shifted right by 8 to recover a signed 24-bit value. Omitting the shift scales every sample by 2^8, an offset of **48 dB**, and leaves the undefined low byte in the data.

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

Three details in that block carry the result. `n` is a byte count, so the sample count is `n / 4` for 32-bit slots. The one-pole recursive filter tracks and subtracts the direct-current (DC) offset, which would otherwise be squared into the sum and inflate every reading at low levels. The calibration constant converts relative full-scale units into absolute SPL: a 94 dB SPL sine reads **-26 dBFS**, so its peak amplitude is 8388607 × 10^(-26/20) ≈ **420,426** counts in 24-bit data, and the root-mean-square (RMS) reference is that peak divided by √2. Anchored on this, the output is dB SPL rather than arbitrary dBFS — within the part's ±3 dB tolerance. Comparison against a reference meter narrows the residual, but no published figure fixes how far.

## From dB(Z) to dB(A), and on to Leq

The listing above is unweighted, conventionally labelled Z. Published noise figures and regulatory limits use **A-weighting**, a standardized curve that attenuates low frequencies in the manner human hearing does. It is realized as an IIR filter, implemented as a cascade of **second-order sections (biquads)** applied per sample block. The esp32-i2s-slm project ships this arrangement: one biquad cascade equalizing the microphone's own response, followed by an A-weighting cascade with coefficients designed for 48 kHz. Each sample costs a fixed handful of multiply-accumulates per section, independent of block size, so a fast Fourier transform is not required to obtain the weighting.

Integration follows. **Leq** is RMS over a window expressed in dB. A **125 ms** window matches the time scale of the conventional Fast meter response and gives LAeq,125ms as the live indication; a 1 s window gives LAeq,1s for publishing, and a 15 min window gives the aggregate used for neighbourhood noise. Because the accumulated quantity is energy — a sum of squares — the longer horizons cost nothing extra: one running sum is maintained and snapshotted at each horizon boundary.

The achievable agreement is reported rather than derived. Kostoski gives a usable range of roughly **35–116 dB(A)** for an ICS-43434 build; the lower end is the microphone's own noise and the upper end its acoustic overload point. Microphone aging, enclosure acoustics and wind noise remain unhandled, so the node is a trend instrument, not a legal one. Where writing the firmware is not the objective, stas-sl's ESPHome external component packages equivalent processing.

## Fitting the node into the station

The published channel is `laeq_1s`, or one-minute aggregates, alongside particulate matter and CO2; the MQTT discovery scheme from the [previous article](/articles/iot-embedded/2026-08-15-home-assistant-mqtt-discovery-esp32/) admits it as one further `cmps` entry with `unit_of_meas: "dB"`. Two physical constraints apply. The microphone port must sit outside any sealed enclosure — a foam-plugged hole suffices — because a sealed port measures the enclosure. And the I2S lines should not be shared with other signals: the bit clock runs at **3.072 MHz** for 48 kHz, 32-bit stereo frames, and couples into adjacent wiring.

Privacy is determined by where the arithmetic happens. Computing Leq on-device and publishing only the scalar keeps raw audio off the network; publishing samples instead makes the same hardware a microphone in the other sense.

A useful verification step is to port the A-weighting biquads into `read_block_db()`, play a 1 kHz tone at a known level, and confirm that A- and Z-weighted readings coincide at 1 kHz, where A-weighting is 0 dB, while diverging on a 100 Hz tone.

## Pitfalls

- **Levels 48 dB high and jittery at low amplitude**: the `>> 8` shift was omitted, so 24-bit data is read at 32-bit scale with an undefined low byte.
- **Sample count off by four**: `i2s_channel_read` returns bytes in `n`, not samples; dividing the sum of squares by `n` rather than `n / 4` understates RMS.
- **A floor near 40 dB(A) that never drops**: DC offset is being squared into the sum, or the reading is at the INMP441's 33 dBA SPL equivalent input noise, which no filtering removes.
- **Silence or one channel of zeros**: the L/R pin is floating or the slot mask selects the right slot while the part transmits in the left.
- **Absolute readings wrong by several dB between two identical boards**: the INMP441's sensitivity tolerance is ±3 dB per part; the calibration constant assumes the nominal -26 dBFS.
- **Weighted readings wrong at every frequency but 1 kHz**: A-weighting biquad coefficients are sample-rate specific, and the esp32-i2s-slm set is designed for 48 kHz.
- **Readings track wind rather than sources**: wind noise at the port is unhandled by A-weighting and appears as low-frequency energy.
