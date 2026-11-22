---
name: openfield
description: "Run AI media jobs and local LLMs on the openField server at http://<openfield-host>:8765 (Qwen-Image-2.1, MiniMax-H3, RIFE, Real-ESRGAN, oMLX). Use it whenever a task needs image generation or editing, video generation, video upscaling or frame interpolation, local LLM calls, media inspection."
---

# openField

openField runs AI media services and local LLMs on a machine on the local network (Apple M5 Max), at **`http://<openfield-host>:8765`**. Jobs run on its GPU, which takes a limited number of jobs at once (slots, set by the owner; 1 now), and every job also needs enough free memory for its model. A request that does not fit is **rejected, not queued**: wait and retry.

## Access

- Send `X-Token: $OPENFIELD_KEY` (or `Authorization: Bearer $OPENFIELD_KEY`) on every call. The key is in the `OPENFIELD_KEY` environment variable: never print it, log it or write it into files.
- An agent running on the openField machine itself can use `http://localhost:8765` with no key.
- Two ways in: the REST API below, or the MCP server at `http://<openfield-host>:8765/mcp` (streamable HTTP, same header). If openField's MCP tools are available to you, prefer them: same operations.
- Live service list with full parameter schemas: `GET http://<openfield-host>:8765/api/v1/services`. OpenAPI: `http://<openfield-host>:8765/api/openapi.json`. Check it when a service below is missing or a parameter is rejected.

## Services

| id | name | what |
|---|---|---|
| `qwen-image` | Qwen Image 2.1 — gerar imagem | Text to image with Qwen-Image-2.1 Base in bf16 (mflux on the GPU). About 57 s at 1024x1024 with 40 steps. Native ~4 MP presets: 2048x2048, 2400x1792, 2528x1696, 2752x1536 and portrait versions. |
| `qwen-image-edit` | Qwen Image 2.1 — editar imagem | Edit by instruction from 1 to 10 input images (Qwen-Image-2.1 Base, mflux on the GPU). Refer to the inputs as "image 1", "image 2" in the order sent; the last one sets the aspect ratio when no size is given. About 78 s with one input at 40 steps. The output is an RGBA PNG. |
| `minimax-h3-video` | MiniMax-H3 — gerar vídeo com som | Text (or first/last frame) to video with a soundtrack, MiniMax-H3 Q8_0 on stable-diffusion.cpp (Metal). 24 fps, 124 to 345 frames (5.2-14.4 s), at most 1344x768 or 768x1344. Slow: about 6 min for 5 s at 512x288 and roughly 20+ min at 864x480. Describe the action, the camera and the sound. Keyframes are scaled to cover the clip and centre-cropped, so send them in the clip's aspect ratio. |
| `video-interpolate` | Vídeo — interpolação (RIFE) | RIFE v4.6 frame interpolation (ncnn). fps_multiplier doubles (or more) the frame rate at the same duration; cuts synthesise `insert` in-between frames after each listed source frame (0-based), which lengthens the video by those frames while the audio is copied untouched. Cuts apply first, on the source frame numbers. |
| `video-upscale` | Vídeo — upscale (Real-ESRGAN) | Upscale a video with Real-ESRGAN (ncnn, Metal through MoltenVK); realesr-animevideov3 suits comic/cel styles. The source audio is copied untouched and its colours are kept. About 2.9 s per second of 720p video for x2. |
| `video-upscale-interpolate` | Vídeo — upscale + interpolação | Real-ESRGAN upscaling, then RIFE interpolation at the upscaled size (see the two services alone). |
| `omlx` | oMLX — LLMs (proxy OpenAI-compatível) | Proxy to oMLX's OpenAI-compatible API (chat/completions, responses, models...). Point an OpenAI client at /proxy/omlx/v1 with an openField key. Calls share the GPU with each other but are refused (409) while an image or video job holds it, and vice versa. |
| `media-probe` | Mídia — inspecionar arquivo (ffprobe) | Codec, size, frame rate, frame count, duration, colour tags and audio of a file. No GPU, so it runs even while the GPU is busy. |

### Parameters

- **`qwen-image`** → outputs: `image` (image/png)
  - `prompt` (string, **required**): What the image shows
  - `negative_prompt` (string): What to avoid; needs guidance above 1 (2.5 when guidance is omitted)
  - `width` (int, 256–2752, multiple of 16, default 1024)
  - `height` (int, 256–2752, multiple of 16, default 1024)
  - `steps` (int, 1–150, default 40): 40 recommended; 20-28 for drafts
  - `guidance` (float, 0–30): Default 1.0, as the model was trained
  - `seed` (int, 0–2147483647, random if omitted): Same seed and prompt give the same image
  - `quantize` (one of `4`, `5`, `6`, `8`): Quantize to save memory (lower quality); omit for full quality
  - Note: Keep the returned seed to regenerate the same image with more steps or another size.
  - Note: negative_prompt needs guidance above 1 (2.5 is used when guidance is omitted).
- **`qwen-image-edit`** → outputs: `image` (image/png)
  - `prompt` (string, **required**): The change, e.g. 'Replace the background with a beach. Keep the face and pose.'
  - `images` (image file, up to 10, **required**): Input images (repeat the field)
  - `mask` (image file): Optional mask over image 1: white = repaint, black = keep
  - `auto_mask` (string): Name an object to repaint ('the red shirt'); ignored with mask
  - `width` (int, 256–2752, multiple of 32): Optional; give both width and height or neither
  - `height` (int, 256–2752, multiple of 32)
  - `steps` (int, 1–150, default 40)
  - `strength` (float, 0.01–1): 1.0 (default) redraws from noise; lower keeps more of the input
  - `negative_prompt` (string)
  - `guidance` (float, 0–30)
  - `seed` (int, 0–2147483647, random if omitted)
  - `quantize` (one of `4`, `5`, `6`, `8`)
  - Note: Send the images as repeated `images` multipart fields (or several `files` entries over MCP), in the order the prompt refers to them.
- **`minimax-h3-video`** → outputs: `video` (video/mp4)
  - `prompt` (string, **required**): Action, camera and sound of the clip
  - `width` (int, 256–1344, multiple of 32, default 1344): 1344x768 is the model's reference size; 864x480 for drafts
  - `height` (int, 256–1344, multiple of 32, default 768)
  - `duration_s` (float, 1–14.4): Length in seconds (rounded up to the frame grid)
  - `num_frames` (int, 1–345): Frames at 24 fps on the 17k+5 grid from 124 to 345 (overrides duration_s)
  - `steps` (int, 1–60, default 20)
  - `seed` (int, 0–9007199254740991, random if omitted)
  - `flow_shift` (float, 0.1–50, default 12)
  - `audio_flow_shift` (float, 0.1–50, default 3)
  - `first_frame` (image file): Image the clip starts from
  - `last_frame` (image file): Image the clip ends on
  - `crf` (int, 0–51, default 16): H.264 quality of the MP4
  - Note: Takes many minutes; submit, then poll every 30-60 s.
  - Note: For image-to-video, send keyframes in the clip's aspect ratio; they are scaled to cover and centre-cropped.
- **`video-interpolate`**
  - `video` (video file, **required**)
  - `fps_multiplier` (int, 2–8): 2: 24 fps becomes 48 fps (default when no cuts are given)
  - `cuts` (json): e.g. [{"frame": 312, "insert": 8}]
  - `crf` (int, 0–51, default 16)
  - `pix_fmt` (one of `yuv420p`, `yuv420p10le`, `yuv422p`, `yuv444p`, default "yuv420p")
  - `keep_audio` (bool, default true)
  - Note: With cuts, the video gets longer than its untouched audio by insert/fps seconds per cut.
- **`video-upscale`**
  - `video` (video file, **required**)
  - `scale` (one of `2`, `3`, `4`, default 2)
  - `model` (one of `realesr-animevideov3`, `realesrgan-x4plus`, `realesrgan-x4plus-anime`, default "realesr-animevideov3")
  - `out_height` (int, 144–4320, multiple of 2): Downscale (lanczos) to this height after upscaling
  - `crf` (int, 0–51, default 16)
  - `pix_fmt` (one of `yuv420p`, `yuv420p10le`, `yuv422p`, `yuv444p`, default "yuv420p")
  - `keep_audio` (bool, default true)
- **`video-upscale-interpolate`**
  - `video` (video file, **required**)
  - `scale` (one of `2`, `3`, `4`, default 2)
  - `model` (one of `realesr-animevideov3`, `realesrgan-x4plus`, `realesrgan-x4plus-anime`, default "realesr-animevideov3")
  - `out_height` (int, 144–4320, multiple of 2)
  - `fps_multiplier` (int, 2–8)
  - `cuts` (json)
  - `crf` (int, 0–51, default 16)
  - `pix_fmt` (one of `yuv420p`, `yuv420p10le`, `yuv422p`, `yuv444p`, default "yuv420p")
  - `keep_audio` (bool, default true)
- **`media-probe`**
  - `file` (any file, **required**)

## Proxies

- **`omlx`** (oMLX — LLMs (proxy OpenAI-compatível)): base URL `http://<openfield-host>:8765/proxy/omlx/v1`, API key `$OPENFIELD_KEY` (an OpenAI-style client sends it as `Authorization: Bearer`).
  - Note: List the models with GET <base URL>/models; use the model id exactly as listed.

Proxy calls take a GPU slot while they are in flight (calls of the same proxy share one). When every slot is held by a job, they answer 409 too.

## REST flow

```sh
B=http://<openfield-host>:8765
# 1. Is there a free slot? (busy = none free; resources.gpu.used/slots; running jobs with progress, eta_s)
curl -s -H "X-Token: $OPENFIELD_KEY" $B/api/v1/status
# 2. Submit. JSON when there are no files:
curl -s -H "X-Token: $OPENFIELD_KEY" -H 'Content-Type: application/json' \
  -d '{"service": "qwen-image", "params": {"prompt": "a lighthouse at dusk, photo"}}' $B/api/v1/jobs
#    multipart with files: a 'job' field with the same JSON, plus one field per file parameter (repeat it for several files)
curl -s -H "X-Token: $OPENFIELD_KEY" -F 'job={"service": "qwen-image-edit", "params": {"prompt": "make it night"}}' -F images=@input.png $B/api/v1/jobs
# 3. Poll until state is done / error / cancelled
curl -s -H "X-Token: $OPENFIELD_KEY" $B/api/v1/jobs/<id>
# 4. Download each output listed in "outputs" (full URLs in "output_urls")
curl -s -H "X-Token: $OPENFIELD_KEY" -o out.png $B/api/v1/jobs/<id>/outputs/<name>
```

Answers to `POST /api/v1/jobs`:
- **202**: `{"id", "state": "running", "estimate_s", "params"}`; `params` includes the seed used.
- **409 busy**: no free slot, or the job's memory would not fit next to the running jobs. `detail.reason`, `detail.held_by`, `detail.running` (progress, `eta_s`) and a `Retry-After` header: wait that long (or poll `/api/v1/status` until a slot is free), then resubmit. Do not hammer it.
- **503**: the job would not fit in memory even with the GPU idle (`detail.available_gb` / `needed_gb`), or the service is unavailable.
- **400**: invalid parameters (the message names the parameter); **401**: missing or wrong key; **404**: unknown service.

Other calls: `GET /api/v1/jobs?state=&service=&limit=` (history, newest first; files kept 7 days), `POST /api/v1/jobs/<id>/cancel`, `DELETE /api/v1/jobs/<id>`.

## MCP tools

`status`, `list_services`, `submit_job(service, params, files=[{param, filename, data_base64}])`, `get_job`, `wait_job(job_id, timeout_s≤120)` (call again while running), `list_jobs`, `cancel_job`, `get_output` (images come back as an image; videos as a URL to download with the key).

## Tips

- Slow jobs (video): submit, then poll every 30-60 s (or call `wait_job` repeatedly).
- Keep the returned `seed` to regenerate the same result with other settings.
- Several slots split the GPU: parallel jobs each run slower.
