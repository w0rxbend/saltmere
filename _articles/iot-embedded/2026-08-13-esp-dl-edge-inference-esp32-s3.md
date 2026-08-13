---
title: "Edge Inference on the ESP32-S3 with ESP-DL: ONNX to .espdl, and What the Vector ISA Buys"
date: 2026-08-13
track: iot-embedded
summary: "Running quantized neural nets on the ESP32-S3 with Espressif's ESP-DL v3.3 and the esp-ppq quantization toolchain: why the S3's SIMD instructions matter, the ONNX-to-.espdl workflow with espdl_quantize_onnx, loading and running a model from the C++ side, and realistic expectations for anomaly detection on sensor data."
reading_time: 6
tags: [esp32-s3, esp-dl, edge-ai, quantization, tinyml]
sources:
  - title: "espressif/esp-dl — GitHub"
    url: "https://github.com/espressif/esp-dl"
  - title: "espressif/esp-dl — ESP Component Registry"
    url: "https://components.espressif.com/components/espressif/esp-dl"
  - title: "How to quantize model — ESP-DL User Guide"
    url: "https://docs.espressif.com/projects/esp-dl/en/latest/tutorials/how_to_quantize_model.html"
  - title: "How to run model — ESP-DL User Guide"
    url: "https://docs.espressif.com/projects/esp-dl/en/latest/tutorials/how_to_run_model.html"
  - title: "espressif/esp-ppq — GitHub"
    url: "https://github.com/espressif/esp-ppq"
---

My air-quality fleet has a classification problem: which "CO2 spike" is a meeting room filling up, and which is a sensor drifting into nonsense? Threshold rules got me 80% of the way; the last 20% wants a model. Cloud inference means shipping every raw window upstream — exactly the traffic a fleet design tries to avoid — so the interesting question is what actually runs *on* the node. On the ESP32-S3, the answer in 2026 is Espressif's own stack: **ESP-DL** for inference, **esp-ppq** for quantization. The ESP-DL v3 line has been iterating fast — the component registry is at v3.3.9 as of this month — and it requires ESP-IDF v5.3 or newer.

## Why the S3 specifically

The S3's Xtensa LX7 cores carry Espressif's processor instruction extensions: 128-bit-wide SIMD operations that do multiple int8 multiply-accumulates per cycle — exactly the inner loop of a quantized convolution or matrix multiply. ESP-DL's operator kernels are hand-optimized in assembly for the S3 (and for the RISC-V ESP32-P4). The framework technically supports nine chips including the classic ESP32 and the C-series, but on those the operators fall back to plain C implementations, and Espressif's own docs say execution is "significantly slower." Practical translation: prototype wherever you like, but if inference latency matters, the S3 or P4 is the target. The P4 also gets a better quantization scheme (more below), which makes it the pick for bigger models.

## The toolchain: ONNX in, .espdl out

ESP-DL runs its own format, `.espdl` — FlatBuffers-based, zero-copy deserialization, int8 or int16 quantized. You get there with esp-ppq, Espressif's fork of the PPQ quantization framework:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install esp-ppq
```

Train in PyTorch, export to ONNX (TensorFlow/TFLite users convert with tf2onnx first), then run post-training quantization with a calibration set of real sensor windows:

```python
from esp_ppq.api import espdl_quantize_onnx

quantized_graph = espdl_quantize_onnx(
    onnx_import_file="anomaly_ae.onnx",
    espdl_export_file="anomaly_ae.espdl",
    calib_dataloader=dataloader,      # real fleet data, shuffle=False
    calib_steps=32,
    input_shape=[1, 64],              # batch size 1 only
    target="esp32s3",                 # or "esp32p4", or "c" for plain ESP32
    num_of_bits=8,
    export_test_values=True,          # embeds a test vector in the .espdl
    device="cpu",
)
```

You get three files: the deployable `.espdl` (viewable in Netron), an `.info` text dump for debugging, and a `.json` with the quantization metadata. Details that matter: the S3 uses per-tensor symmetric quantization with power-of-two scales; the P4 upgrades Conv and GEMM to per-channel, which is why the same model often keeps more accuracy there. And `.espdl` files are **target-specific** — an `esp32s3` model produces wrong results on other chips, so quantize per target. Before touching hardware, run the returned graph through esp-ppq's `TorchExecutor` on your validation set; the docs commit to bit-exact alignment between PC simulation and on-device inference, and that promise held in my testing.

## Running it on the node

The C++ side is compact. Embed the model in flash rodata (or a dedicated partition — better, since `idf.py app-flash` then skips re-flashing the model during development):

```cpp
#include "dl_model_base.hpp"

extern const uint8_t model_espdl[] asm("_binary_anomaly_ae_espdl_start");

dl::Model *model = new dl::Model((const char *)model_espdl,
                                 fbs::MODEL_LOCATION_IN_FLASH_RODATA);

auto inputs  = model->get_inputs();
auto outputs = model->get_outputs();
dl::TensorBase *in  = inputs.begin()->second;
dl::TensorBase *out = outputs.begin()->second;

int8_t *in_data = (int8_t *)in->data;
for (int i = 0; i < 64; i++)   // quantize a window of normalized sensor values
    in_data[i] = dl::quantize<int8_t>(window[i], DL_RESCALE(in->exponent));

model->run();

float err = 0;
int8_t *out_data = (int8_t *)out->data;
for (int i = 0; i < 64; i++) {
    float rec = dl::dequantize(out_data[i], DL_SCALE(out->exponent));
    err += (rec - window[i]) * (rec - window[i]);
}
// err above threshold => reconstruction failed => anomalous window
```

Because you exported `export_test_values`, call `model->test()` once at bring-up: it runs the embedded test vector and returns `ESP_OK` only if on-device output matches the PC-side result. That one call has caught every misquantized deployment I've attempted.

## Realistic expectations

Keep the problem small and the S3 is genuinely fast. A 64-sample autoencoder like the one above — a few thousand parameters — is low single-digit milliseconds per window; effectively free next to a 10-second sample cadence, and cheaper than radioing raw windows upstream. Keyword spotting on audio features lands in the tens of milliseconds; Espressif's esp-sr wake-word stack runs continuously on S3-based products, so that class of workload is proven. Small-image classification runs at hundreds of milliseconds — fine for "photo per minute," not for video.

Three sharp edges. First, check `operator_support_state.md` in the repo **before** designing your network; an exotic activation or unsupported op stops the export. Second, batch size is fixed at 1 — no batching tricks. Third, memory: by default parameters are copied to RAM for speed, and PSRAM makes life much easier; the `param_copy=false` constructor flag keeps weights in flash at a real latency cost. Quantization accuracy loss on 8-bit was under a percent for my autoencoder, but always validate with `TorchExecutor` on held-out data — and if accuracy craters, esp-ppq's newer AutoQuant tooling searches mixed strategies before you resort to quantization-aware training.

**Try next:** clone esp-dl and run the `quantize_sin_model` tutorial end-to-end — train, quantize with `espdl_quantize_onnx`, flash to an S3, and confirm `model->test()` returns `ESP_OK` — before trying it with your own sensor data.
