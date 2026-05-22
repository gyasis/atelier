# Research Notes — Mac Studio inference hub (2026-05-22)

**Source:** Gemini Deep Research (`deep-research-pro-preview-12-2025`), task id `58fe246a-a935-4d6f-9639-96ab27c8d1f9`.
**Run:** 2026-05-22, ~154 min wall, ~4.5 min API mode, 29 cited sources.
**Subject:** Running AI image + video generation locally on Mac Studio (Apple M1 Max, 32-core GPU, 64 GB unified memory, macOS 15.5 Sequoia, Metal 3, arm64) as part of a LAN inference hub alongside Ollama + a Kokoro TTS sidecar. Late 2025 / early 2026 context.

> Raw report below. The spec at `docs/ARCHITECTURE.md` is the canonical decision record — this file is the underlying evidence.

---

The deployment of a local AI inference hub on a Mac Studio (M1 Max, 64 GB unified memory, macOS 15.5 Sequoia) presents a highly capable but memory-constrained engineering environment. While the 64 GB of unified memory offers a distinct advantage over standard consumer desktop GPUs for large model execution, the memory bandwidth (~400 GB/s) and the lack of native CUDA support necessitate rigorous architectural choices.

**Key Points:**
* **Memory Management is the Primary Bottleneck:** 64 GB of unified memory is ample, but running a 30 GB Ollama context alongside a 14B parameter video generation model will cause catastrophic swap death.
* **The Framework Shift:** As of late 2025/early 2026, Apple's native MLX framework natively outperforms PyTorch's MPS (Metal Performance Shaders) backend due to zero-copy memory advantages and quantized attention optimizations.
* **GGUF is Mandatory:** 14B parameter models like Wan2.1 and HunyuanVideo are entirely non-viable on 64 GB hardware without 4-bit or 5-bit GGUF quantization.
* **Headless Reliability:** ComfyUI can operate effectively as a headless microservice, but robust job tracking requires WebSocket integration.
* **ASR Dominance:** `mlx-whisper` and CoreML-based `FluidAudio` implementations have completely superseded CPU-bound `whisper.cpp` executions on Apple Silicon.

## 1. ComfyUI on Apple Silicon (Install + Run)

Deploying ComfyUI on macOS arm64 demands deliberate environment isolation and PyTorch configuration to ensure stability under extended inference workloads.

### Best Install Path and Python Version

In late 2025, the canonical package manager is **`uv`**. It provides near-instantaneous dependency resolution and pristine isolation, circumventing the bloat of Conda and the brittleness of Homebrew-managed Python packages. The recommended initialization is via a `uv venv`.

**Python 3.12** is the undisputed sweet spot. Python 3.11 is trailing into obsolescence for cutting-edge ML libraries, while Python 3.14 introduces experimental ABI changes that routinely break C-extensions compiled for arm64. Python 3.12 guarantees the highest compatibility with pre-built wheels for PyTorch and custom node dependencies. Avoid portable bundles (which obscure debugging) and rely on a clean `uv`-managed virtual environment.

### PyTorch MPS Maturity and Configuration

The PyTorch MPS backend has matured significantly but still lacks coverage for several critical tensor operations (e.g., specific `float8_e4m3fn` casts). Therefore, declaring the environment variable `PYTORCH_ENABLE_MPS_FALLBACK=1` is an absolute necessity. Without this flag, ComfyUI will instantly crash when encountering an unimplemented Metal op; with it, PyTorch silently routes the operation to the CPU.

The recommended target is **PyTorch 2.5.1** or the latest nightly build, paired with `torchvision` matched to the release. Additionally, defining `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0` prevents aggressive memory pre-allocation crashes.

### Headless and API-Only Operation

ComfyUI is frequently misunderstood as merely a browser-based GUI. In production, it functions as a lightweight HTTP server managing a robust execution graph. To run ComfyUI as a long-running, launchd-managed service without the browser, execute `main.py` directly (bypassing any `--auto-launch` flags).

The primary API endpoints are:
* `GET /object_info`: Maps available nodes.
* `POST /prompt`: Submits the JSON-formatted workflow graph.
* `GET /queue`: Returns pending and running jobs.
* `GET /history`: Returns completed job data.
* `GET /view?filename=...`: Retrieves output artifacts.

**The WebSocket Gotcha:** Polling `/history` is inefficient and prone to race conditions for long-running video jobs. You must connect to `/ws?clientId=<UUID>` to receive real-time execution steps (`execution_start`, `executing`, `progress`, `executed`). If you do not maintain a WebSocket connection, you will not receive step-level progress. There is no "must keep a browser tab open" constraint if your custom LAN application correctly maintains this WebSocket listener and submits workflows strictly in ComfyUI's "API format" (which differs slightly from the standard UI JSON).

### Custom Node Management on Apple Silicon

**Nodes to AVOID:**
1. Any nodes utilizing **Triton** or **SageAttention**. Triton does not compile natively on macOS arm64 without extensive, brittle hacking.
2. **xformers**-dependent nodes. Apple Silicon utilizes native `sdpa` (Scaled Dot Product Attention); xformers is tightly coupled to CUDA.
3. Memory-offloading nodes designed for low-VRAM Windows GPUs. These interfere with Apple's unified memory architecture and cause extreme performance degradation.

**Nodes that WORK GREAT:**
1. **ComfyUI-GGUF** (by city96): Indispensable for loading 4-bit quantized DiT models.
2. **ComfyUI-Manager**: Essential for dependency tracking and clean node updates.
3. **ComfyUI-LTXVideo**: Well-maintained, though it occasionally requires fallback flags for autocast issues.

## 2. Video Generation Models: Feasibility on 64 GB M1 Max

Video generation via Diffusion Transformers (DiTs) is the most computationally hostile workload for an M1 Max. Memory bandwidth (400 GB/s) and total capacity (64 GB) dictate strict limitations. A model must fit into the RAM *alongside* Ollama and the OS. If the total allocated memory exceeds ~55 GB, the M1 Max will page to disk (swap), converting a 5-minute render into a 2-hour failure.

| Model | Works on Apple Silicon? | RAM Footprint (Quantization) | Approx. 4s Render (480p) | Quality Tradeoffs |
| :--- | :--- | :--- | :--- | :--- |
| **LTX-Video (2B)** | Yes (Native & MLX) | ~4-6 GB (FP16/BF16) | ~5 mins | Excellent temporal consistency; strict prompt adherence required. |
| **Wan2.1 1.3B** | Yes (Native) | ~6-8 GB (FP16) | ~4-6 mins | Less coherent motion than 14B; slightly "cartoonish" at edges. |
| **Wan2.1 14B** | Yes (GGUF Only) | ~10 GB (Q4_K_M) | ~11-15 mins | Q4 causes minor artifacting in complex textures, but motion is highly realistic. |
| **Mochi-1 (10B)** | Yes (GGUF) | ~12 GB (Q4) | ~15+ mins | High prompt adherence, but generally slower and softer output than Wan2.2. |
| **HunyuanVideo** | Yes (GGUF) | ~9-12 GB (Q4) | ~15-20 mins | Q4 works well, but VAE decoding is exceptionally slow on MPS. |
| **CogVideoX-5B** | Yes | ~8 GB (FP8) | ~8 mins | Good, but largely superseded by Wan and LTX in prompt comprehension. |
| **AnimateDiff / SVD** | Yes | ~4 GB | ~1 min | Fully superseded by DiT architectures. Highly robotic motion. |

### Model Specifics and Recommendations

**Wan2.1 1.3B:** Fits comfortably in FP16. Ideal for rapid prototyping. It operates fast enough on the M1 Max to be interactive but lacks the cinematic depth of the 14B variant.

**Wan2.1 14B:** Do not attempt FP16. The native model will trigger system swap and generate corrupted frames. You must use the `Q4_K_M` or `Q5` GGUF variant via the `ComfyUI-GGUF` node. The maximum practical limit on an M1 Max without swap death is roughly 48 frames (2 seconds at 24fps) at 480p, scaling to roughly 4-8 seconds if resolution is strictly managed.

**LTX-Video (LTX-2):** A highly optimized 2B model that runs beautifully on M-series chips. The ComfyUI implementation occasionally throws MPS autocast errors, but it generates realistic, coherent motion in a fraction of the time required by Wan 14B.

**Mochi-1:** Historically important but falling behind in 2026. Still viable via GGUF, but Wan 2.2 and LTX-Video yield superior generation times.

**Stable Video Diffusion & AnimateDiff:** Consider these deprecated for a 2026 deployment. Their U-Net architectures cannot compete with modern DiTs.

**Opinionated Ranked Recommendation for M1 Max 64GB:**
1. **LTX-Video** (Best balance of speed and VRAM efficiency for 4-second clips).
2. **Wan2.1 14B Q4_K_M GGUF** (Best cinematic quality, but requires extreme patience — treat as asynchronous batch jobs only).
3. **Wan2.1 1.3B** (Best for rapid storyboarding).

## 3. Image Generation Baseline

Image generation on the M1 Max 64GB is highly performant. The objective is to select quantization levels that allow the image model to load without displacing Ollama's active LLM from memory.

| Model | Setup | Typical Steps | Approx. Time per Image (1024x1024) | Notes |
| :--- | :--- | :--- | :--- | :--- |
| **Flux.1-schnell** | FP8 / GGUF Q8 | 4 steps | ~110-120 seconds (PyTorch) / ~15s (MLX) | Massive speedup via MLX framework. |
| **Flux.1-dev** | GGUF Q4_K_M | 20 steps | ~240-300 seconds (PyTorch) | Q4 maintains 92% of FP16 visual fidelity. |
| **SDXL 1.0** | FP16 | 25 steps | ~12-15 seconds | Highly optimized for MPS. |
| **SD 3.5 Large** | FP8 / GGUF | 30 steps | ~45-60 seconds | Competent, but Flux offers superior typography and prompt adherence. |

**Quantization Strategy:** For Flux on a 64 GB machine running alongside a ~30 GB Ollama instance, the **GGUF Q4_K_M** or **Q5_K_M** format is the strategic imperative. The Q8 variant consumes nearly 12 GB, which flirts dangerously with the swap threshold when transformer activation buffers (KV caches) expand during generation. Flux.1-schnell via MLX is the definitive choice for near-real-time generation.

## 4. MLX-Native Alternatives

The most profound shift in Apple Silicon AI engineering in late 2025 is the maturation of Apple's MLX framework. PyTorch MPS involves translation overhead and occasional redundant data copying. MLX is written strictly for Apple's unified memory, meaning tensors remain in place, and CPU/GPU zero-copy operations are native.

* **`mflux`:** A direct MLX port of Flux. It achieves performance 25-40% faster than ComfyUI PyTorch. On an M1 Max, it bypasses MPS bottlenecks, generating 1024x1024 images in under 15 seconds for Flux Schnell.
* **`mlx-video`:** Developed by Prince Canuma, this supports LTX-2 and Wan2.1 natively. It is dramatically faster than the ComfyUI equivalents and exposes simple CLI/API generation tools.
* **`mlx-vlm`:** Superior for vision-language models, though Ollama currently handles text routing well.

**Architectural Decision:** **Maintain a dual-stack.**

Relying strictly on MLX sacrifices the vast ecosystem of ComfyUI's visual workflow builder, ControlNets, and LoRA chaining. The pragmatic engineering choice is to host ComfyUI as the primary orchestration engine for complex, multi-stage pipelines (e.g., Image-to-Video with IPAdapter constraints), while standing up `mflux` and `mlx-video` as lightweight, parallel Python HTTP services for sheer "prompt-to-output" brute speed tasks.

## 5. Whisper / ASR on M1 Max

For LAN sidecar transcription tasks (e.g., voice commands to the LLM or subtitle generation), the landscape has solidified.

* **`whisper.cpp` (Metal):** The standard in 2024. Excellent memory footprint, but effectively dethroned on Apple Silicon.
* **`mlx-whisper`:** A direct MLX implementation of OpenAI's architecture. It is roughly 30-50% faster than `whisper.cpp` on an M1 Max. The `whisper-large-v3-turbo` model transcribes 12 minutes of audio in ~14-18 seconds, achieving a Real-Time Factor (RTF) of ~40-50x.
* **`FluidAudio` (CoreML/Parakeet):** Compiles models directly to the Apple Neural Engine. It achieves 0.19 seconds per inference compared to `mlx-whisper`'s 1.02 seconds.

**Recommendation:** For a Python-based LAN sidecar, **`mlx-whisper`** is the definitive choice. It requires no complex C++ build chains (`pip install mlx-whisper`), seamlessly integrates with standard Python FastAPI servers, and fully saturates the M1 Max GPU.

## 6. Disk + Model Management

With 2.9 TB of available storage, disk capacity is not a hard constraint, but organizational entropy is a severe risk.

### Realistic Disk Budget

* **Video Models:** ~25 GB total. (Wan2.1 14B Q4 = ~10 GB; HunyuanVideo Q4 = ~9 GB; LTX-Video = ~5 GB).
* **Image Models:** ~25 GB total. (Flux Q8/Q4 = ~12 GB; SDXL = ~7 GB; SD 3.5 = ~6 GB).
* **Audio/TTS Models:** ~5 GB. (Whisper large-v3-turbo = ~1.6 GB; Kokoro ONNX = <1 GB; VAEs = ~2 GB).
* **LLMs (Ollama):** ~400 GB (gemma4:31b, qwen3:32b, deepseek-r1:70b).
* **Total Ballpark Estimate:** ~455-500 GB.

### Best Practices for Model Sharing

Do not duplicate model weights across the MLX stack and ComfyUI. The cleanest convention in late 2025 leverages the `HF_HOME` environment variable and physical symlinking.

1. Set `export HF_HOME="/Volumes/Data/AI/huggingface"` globally in the user's shell profile and launchd agents. This forces Python scripts, MLX pipelines, and HuggingFace Hub downloads to use a centralized cache.
2. For ComfyUI, edit `extra_model_paths.yaml`. Map the `checkpoints`, `unet`, `clip`, and `vae` directories directly to absolute paths within a curated `/Volumes/Data/AI/models/` directory.
3. Use APFS symlinks for models that refuse to obey the yaml routing. APFS handles symlinks flawlessly, incurring zero disk overhead.

## 7. Service Architecture for One-User LAN Hub

Hosting these tools as reliable background services for a single-user LAN requires pragmatism over enterprise-grade overkill.

### Authentication

**Bearer Token + IP Allowlist.** Do not implement OAuth or mTLS for a single-user home LAN. Assign static IPs via your router to trusted devices (e.g., `192.168.1.50-60`). Configure the FastAPI/Node gateways to reject any traffic outside this subnet, and pass a hardcoded HTTP Bearer token in the request headers. This provides sufficient friction against casual internal probing without the maintenance nightmare of certificate rotation.

### Job Lifecycle Pattern

Mixes of synchronous (Kokoro TTS at <2 seconds) and highly asynchronous (Wan2.1 video at 15 minutes) workloads demand **Server-Sent Events (SSE)** or **WebSockets**.

* *Fast requests* (TTS, LLM streaming) should use SSE. It natively supports standard HTTP routing and keeps clients effortlessly updated.
* *Heavy asynchronous tasks* (Video/Image gen) should follow a REST + WebSocket paradigm. Submit the job via a `POST` request (which returns a `job_id`). The client then establishes a WebSocket connection using that `job_id` to stream status updates. ComfyUI natively expects WebSocket connections for real-time progress bar reporting.

### Health and Readiness Signals

Every sidecar (ComfyUI, MLX API, Whisper, Kokoro) must expose two unauthenticated GET endpoints:

* `/healthz`: Returns `HTTP 200 OK`. Indicates the HTTP server process is running and responding.
* `/readyz`: Returns `HTTP 200 OK` only if models are loaded into memory and the GPU queue is ready to accept inference requests. (If a model is cold-loading or the system is swapping, return `HTTP 503`).

### Deployment Infrastructure

**`launchd` plists are the undisputed best choice** for macOS deployments.

* *Why not `brew services`?* It abstracts away critical error logging and environment variables (like `PYTORCH_ENABLE_MPS_FALLBACK`) that ML pipelines require.
* *Why not `supervisord`?* It requires a separate Python installation and fights with macOS native process lifecycle management.
* *Why not `tmux`?* Highly prone to user error and failure to auto-recover upon power loss.

Custom `~/Library/LaunchAgents/` plists allow you to inject precise environment variables (`PATH`, `HF_HOME`), define `StandardOutPath` for logging, and leverage `KeepAlive` for automatic restarts if a PyTorch Out-of-Memory (OOM) error crashes the daemon.

## 8. Top Sharp Edges

When deploying advanced AI pipelines on Apple Silicon, hardware elegance often masks brutal software edge cases. Below are the top five hard-won failure modes specific to M-Series Macs in this configuration:

1. **Unified Memory Swap Death:** The Mac Studio will happily attempt to load a 20 GB video model while Ollama is holding 30 GB of LLM context. Instead of throwing a hard CUDA `OutOfMemoryError` like an NVIDIA card, macOS silently pages memory to the NVMe SSD. Inference speed will instantly drop from 2 seconds/iteration to 400 seconds/iteration. *Mitigation:* You must strictly enforce VRAM offloading in Ollama (`ollama stop <model>`) before dispatching a ComfyUI video job, or utilize MLX memory limits to cap allocation.
2. **App Nap and Background Throttling:** macOS aggressively downclocks and pauses background processes that lack visible UI windows. A headless ComfyUI `launchd` service will be severely throttled by macOS "App Nap", causing a 5-minute render to stretch indefinitely. *Mitigation:* Wrap your launch daemon execution scripts in the macOS `caffeinate -i` command, or manually disable App Nap for the Python binary via `defaults write`.
3. **The `PYTORCH_ENABLE_MPS_FALLBACK=1` Crash:** As highlighted in Section 1, assuming PyTorch MPS has full feature parity with CUDA will result in catastrophic failure. Specific data types required by modern DiTs (e.g., `bfloat16` or `float8` scaling) will cause the application to abruptly abort with a fatal exception if the fallback flag is absent. *Mitigation:* Hardcode `export PYTORCH_ENABLE_MPS_FALLBACK=1` inside the shell script executed by the `launchd` agent.
4. **TCC Sandbox Permissions for Central Repositories:** If you store your centralized `HF_HOME` models on an external Thunderbolt drive or a secondary internal Volume, macOS's Transparency, Consent, and Control (TCC) framework will block terminal-spawned Python processes from reading the files, resulting in obscure `FileNotFound` errors. *Mitigation:* You must explicitly grant "Full Disk Access" to the Terminal application, your IDE, and specifically the `python3` binary located inside your `uv venv` via System Settings.
5. **C-Extension Compiler Mismatches:** When installing custom ComfyUI nodes, Python often attempts to compile C/C++ dependencies natively. If Xcode Command Line Tools are out of date, or if the system defaults to an older Clang compiler, wheels like `tokenizers` or specific audio processing libraries will fail to build or segment-fault at runtime. *Mitigation:* Run `xcode-select --install` upon any major macOS update. Ensure your `uv` environments are entirely recreated if you upgrade the base macOS version, as dynamic library links can break silently during system updates.
