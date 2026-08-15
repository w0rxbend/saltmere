---
title: "Edge Inference on the ESP32-S3 with ESP-DL: ONNX to .espdl, and What the Vector ISA Buys"
date: 2026-08-13
track: iot-embedded
summary: "Running quantized neural networks on the ESP32-S3 with Espressif's ESP-DL v3 and the esp-ppq quantization toolchain: the role of the S3's SIMD instructions, the ONNX-to-.espdl workflow through espdl_quantize_onnx, model loading and execution from C++, and how to size an anomaly-detection workload against the part."
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

**Gist.** Distinguishing a genuine carbon-dioxide (CO2) event from sensor drift is a classification problem that threshold rules solve only partially, and solving it in the cloud requires uploading every raw sensor window — the traffic a fleet design exists to avoid. Espressif's **ESP-DL** inference runtime plus the **esp-ppq** quantization toolchain move the model onto the node, executing an int8-quantized network directly on the ESP32-S3. The cost is a lossy conversion: the model must be post-training quantized against real calibration data, the resulting artefact is bound to one target chip, and the operator set the network may use is restricted to what ESP-DL implements.

The ESP-DL v3 line iterates quickly; the ESP Component Registry lists a **3.3.x** release at the time of writing, and the registry entry records a **minimum ESP-IDF version** that has moved upward across the v3 line, so the required IDF release should be read from the component manifest rather than assumed.

## Chip selection

The S3's Xtensa LX7 cores carry Espressif's processor instruction extensions: **128-bit-wide single-instruction-multiple-data (SIMD) operations** performing several int8 multiply-accumulate steps per cycle. That operation is the inner loop of a quantized convolution or general matrix multiply (GEMM), so the extension maps directly onto the dominant cost of inference. ESP-DL's operator kernels are hand-written in assembly for the S3 and for the RISC-V ESP32-P4.

The framework also targets parts without those extensions, including the original ESP32 and the C-series. On those parts the operators fall back to plain C implementations, and Espressif's documentation warns that execution is much slower without quantifying the gap. **Prototyping is portable; latency is not.** The P4 additionally receives a finer quantization scheme for Conv and GEMM (below), which is the relevant distinction for larger models.

## Quantization: ONNX in, .espdl out

ESP-DL executes its own container format, `.espdl` — FlatBuffers-based, deserialized without copying, holding int8 or int16 quantized weights. The producer is esp-ppq, Espressif's fork of the PPQ quantization framework:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install esp-ppq
```

The path is: train in PyTorch, export to Open Neural Network Exchange (ONNX) format — TensorFlow and TFLite models convert through `tf2onnx` first — then run post-training quantization against a calibration set drawn from real sensor windows.

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

Three files result: the deployable `.espdl` (inspectable in Netron), an `.info` text dump for debugging, and a `.json` carrying the quantization metadata.

Two properties of the conversion are load-bearing. First, the scheme differs by target: **the S3 uses per-tensor symmetric quantization with power-of-two scales, while the P4 raises Conv and GEMM to per-channel quantization**. Per-channel assigns each output channel its own scale, so a channel whose weights occupy a narrow range is no longer forced to share a scale with a wide-range channel; the same network therefore tends to retain more accuracy on the P4. Second, **`.espdl` artefacts are target-specific** — a file quantized for `esp32s3` produces incorrect results on another chip rather than failing to load, so quantization must be repeated per target.

The returned graph should be evaluated on the validation set through esp-ppq's `TorchExecutor` before any hardware is involved. The ESP-DL documentation states that PC-side simulation and on-device inference agree numerically, which makes the host-side result an admissible proxy for on-device accuracy.

## Execution on the node

The model is embedded either in flash read-only data (rodata) or in a dedicated partition. The partition arrangement is preferable during development because `idf.py app-flash` then rewrites only the application and leaves the model image in place.

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

The detector is an autoencoder: the network is trained to reconstruct normal windows, so a large squared reconstruction error indicates a window unlike the training distribution. Note that the tensors expose an `exponent` rather than a floating-point scale — a direct consequence of the power-of-two scale constraint, which reduces rescaling to a shift.

When the model was exported with `export_test_values=True`, **`model->test()` runs the embedded test vector and returns `ESP_OK` only if the on-device output matches the PC-side result**. Calling it once at bring-up converts a silent numerical mismatch — the failure mode of a mis-targeted or mis-quantized artefact — into an explicit error code.

## Sizing the workload

No published benchmark covers this class of model on the S3, so the sizing below is an ordering of workloads by cost rather than a set of measured figures; the only reliable number is the one obtained by timing the target network on the target part with `esp_timer_get_time()` around `model->run()`.

A 64-sample autoencoder of a few thousand parameters, as above, is dominated by a handful of small GEMMs and is inexpensive relative to a sampling cadence measured in seconds — and cheaper in energy than transmitting the raw window over Wi-Fi. Keyword spotting on audio features is heavier by roughly the ratio of multiply-accumulates involved, but is demonstrated in shipping hardware: Espressif's esp-sr wake-word stack runs continuously on S3-based products. Image classification is heavier again, and the practical question is whether the per-inference time fits inside the duty cycle, not whether it runs at all.

Accuracy loss from 8-bit post-training quantization is model-specific and must be measured against a validation set rather than assumed. Where it degrades badly, esp-ppq supports raising sensitive layers to 16-bit rather than quantizing the whole graph to 8, which is a lower-effort step than retraining under quantization-aware training.

## Pitfalls

- **An unsupported operator fails at export, after the network is already trained.** ESP-DL implements a fixed operator set; an exotic activation function has no kernel. Consult `operator_support_state.md` in the repository during network design rather than after.
- **A model quantized for one target returns wrong numbers on another, not an error.** The S3 and P4 use different quantization schemes, and a mismatch surfaces as wrong output rather than a load failure. Re-run `espdl_quantize_onnx` per target.
- **Batching does not exist.** `input_shape` fixes the batch dimension at 1; a design that assumes amortization of per-inference overhead across a batch has no path to it.
- **Parameters are copied into RAM by default.** On a part without pseudo-static RAM (PSRAM) a model that fits in flash can still exhaust the heap at construction. The `param_copy=false` constructor flag leaves weights in flash and pays a per-access latency penalty instead.
- **Calibration consumes only `calib_steps` batches, not the whole loader.** Activation ranges are derived from that subset, so a loader whose first batches are unrepresentative of production data yields scales that clip real inputs. The accuracy loss surfaces only after deployment unless `TorchExecutor` is run on held-out data first.
