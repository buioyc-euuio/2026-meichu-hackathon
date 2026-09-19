# System Architecture on the AMD AI PC

Everything runs locally on one **AMD Ryzen AI 7 350** laptop. Nothing goes to the cloud.
The chip has three compute units, and each model runs on a different one so they don't compete:

| Unit | Model | Job | Runtime |
|---|---|---|---|
| **CPU** (Zen 5) | **YOLO11n** (COCO: `sports ball` + `person`) | Find tennis balls and people in the camera image | Ultralytics / PyTorch (`--device cpu`) |
| **NPU** (XDNA 2) | **Whisper** (Breeze-ASR-25) encoder | Speech-to-text for voice commands | Breeze NPU GEMM encoder, CPU decoder (`whisper-server`) |
| **iGPU** (Radeon 860M) | **Qwen3-4B-Instruct** (Q4_K_M GGUF) | Fix ASR errors and turn a sentence into one robot action | llama.cpp with Vulkan (`-ngl 99`) |

## Data flow

```
                    ┌──────────────── AMD Ryzen AI 7 350 ────────────────┐
 Mic / BT headset ─▶│ Silero VAD ─▶ Whisper encoder [NPU] ─▶ decoder     │
                    │                                   │                │
                    │                                   ▼                │
                    │                     Qwen3-4B [iGPU] ─▶ action JSON │
                    │                                   │                │
 USB camera ───────▶│ YOLO11n [CPU] ─▶ CV tracker ─▶ ball/person JSON    │
                    │                                   │                │
                    │                                   ▼                │
                    │               Controller (voice / ball-chase mode) │
                    └───────────────────────────────────┬────────────────┘
                                                        │ Bluetooth LE
                                                        ▼
                                              micro:bit ─▶ wheels, camera pan/tilt
```

## Services (all on loopback)

| Port | Service | Unit |
|---|---|---|
| `18082` | ASR WebSocket server (`robot_control/asr/server.py`): PCM in, text out | NPU + CPU |
| `18081` | `whisper-server` CPU decoder (started by the ASR server) | CPU |
| `18083` | `llama-server`, model alias `robot-igpu` (`robot_control/llm/run_server.sh`) | iGPU |
| `8000` | Tracking server (`tennis_tracking/server.py`): WebSocket / SSE JSON at ~30 fps | CPU |

## Why this split

- **LLM on the iGPU**: token generation is limited by memory bandwidth and does a lot of math. The GPU is the only unit
  that runs a 4B model fast enough. Short-label prompts take about **0.55 s** per decision after warm-up.
- **Whisper on the NPU**: the encoder is one large, fixed-shape batch of matrix multiplies, which suits the NPU.
  This keeps speech recognition off the GPU while the LLM runs. The decoder stays on the CPU.
- **YOLO on the CPU**: YOLO11n is small (about 18 ms per 640×480 frame, faster than the 30 fps camera),
  so it can run on the CPU and leave the iGPU to the LLM.
  (`--device gpu` runs YOLO through ncnn + Vulkan instead, which is faster but shares the iGPU with the LLM.)

## Safety boundaries

- The LLM can only output `forward / backward / left / right / stop / none`, plus a duration of 0.1 to 2 s.
  The program checks the output before anything reaches Bluetooth, and the LLM never gets direct access to the motors.
- A spoken "stop" skips the LLM entirely. Keyboard stop and mode switches never wait for the LLM.
- Only one program connects to the micro:bit at a time. `e2e/demo.py` runs the demo stages one after another.

## Known bottleneck

Speech-end to recognized text still takes about **4.8 s**. ASR is the slowest step, so voice is not an emergency stop.
