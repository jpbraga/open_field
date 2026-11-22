---
name: openfield
description: Generate and edit images (Qwen-Image-2.1), generate videos with sound (MiniMax-H3), upscale or interpolate videos (Real-ESRGAN, RIFE) and call local LLMs (oMLX) on the user's Mac through the openField daemon at http://192.168.0.205:8765. Use it whenever a task needs one of these media jobs or the Mac's local models.
---

# openField (the Mac's media services)

The user's MacBook Pro (M5 Max, 128 GB) runs a daemon on the home network at
**`http://192.168.0.205:8765`**. The GPU has a number of **slots** the user sets (1 by default, so usually
one job at a time). A job starts only when a slot is free **and** the Mac has enough free memory for its
model; otherwise the request is **rejected** (not queued) and you retry later.

- Auth: send the header `X-Token: $OMLX_LAN_KEY` (or `Authorization: Bearer $OMLX_LAN_KEY`) on every call.
  Never print the key. An agent running on the openField machine itself can use `http://localhost:8765`
  with no key at all.
- Web UI for the user: `http://192.168.0.205:8765/` (same key to log in).
- Two ways in: the REST API below, or the MCP server at `http://192.168.0.205:8765/mcp` (streamable HTTP,
  same header). Prefer MCP when it is configured; it has the same operations as tools.

## Services

`GET /api/v1/services` returns every service with its parameter schema. The current ones:

| id | What | Typical time |
|---|---|---|
| `qwen-image` | Text to image, Qwen-Image-2.1 Base bf16. Defaults 1024x1024, 40 steps. Up to ~4 MP (2048x2048, 2752x1536...) | ~1 min at 1 MP |
| `qwen-image-edit` | Edit from 1-10 images (`images` file field, repeat it) by instruction; optional `mask`, `auto_mask`, `strength` | ~1.3 min |
| `minimax-h3-video` | Video **with sound** from text, optionally `first_frame` / `last_frame` images. 24 fps, 124-345 frames (5.2-14.4 s), ≤1344x768 | **slow**: ~6 min at 512x288, ~20+ min at 864x480 for 5 s |
| `video-upscale` | Real-ESRGAN x2/x3/x4 (`realesr-animevideov3` for cel/comic), `out_height` to downscale after | ~3 s per s of 720p (x2) |
| `video-interpolate` | RIFE: `fps_multiplier` (24→48) or `cuts` `[{"frame": N, "insert": K}]` to add in-between frames | fast |
| `video-upscale-interpolate` | Both, in that order | |
| `media-probe` | ffprobe of a file (no GPU: runs even while busy) | instant |
| `omlx` | Proxy to the Mac's oMLX LLMs: OpenAI base URL `http://192.168.0.205:8765/proxy/omlx/v1` | |

The LLM proxy takes a GPU slot too while calls are in flight (its calls share that one slot). With one slot,
LLM calls get 409 while an image/video job runs, and jobs get 409 while LLM calls are in flight.

## REST flow

```sh
B=http://192.168.0.205:8765
# 1. Is there a free slot? (busy = none free; resources.gpu.used/slots; running jobs with stage/progress/eta_s)
curl -s -H "X-Token: $OMLX_LAN_KEY" $B/api/v1/status
# 2. Submit. JSON when there are no files:
curl -s -H "X-Token: $OMLX_LAN_KEY" -H 'Content-Type: application/json' \
  -d '{"service":"qwen-image","params":{"prompt":"a lighthouse at dusk, photo","seed":7}}' $B/api/v1/jobs
#    multipart with files: a 'job' field with the same JSON, plus one field per file parameter
curl -s -H "X-Token: $OMLX_LAN_KEY" -F 'job={"service":"qwen-image-edit","params":{"prompt":"make it night"}}' \
  -F images=@photo.png $B/api/v1/jobs
curl -s -H "X-Token: $OMLX_LAN_KEY" -F 'job={"service":"minimax-h3-video","params":{"prompt":"...","width":864,"height":480,"duration_s":5}}' \
  -F first_frame=@frame.png $B/api/v1/jobs
# 3. Poll until state is done / error / cancelled
curl -s -H "X-Token: $OMLX_LAN_KEY" $B/api/v1/jobs/<id>
# 4. Download each output listed in "outputs" (also in "output_urls")
curl -s -H "X-Token: $OMLX_LAN_KEY" -o out.png $B/api/v1/jobs/<id>/outputs/image
```

Answers to `POST /api/v1/jobs`:
- **202**: `{"id", "state": "running", "estimate_s", "params"}`; `params` includes the seed used.
- **409 busy**: no free slot, or the job's memory would not fit next to the running jobs. `detail.reason`,
  `detail.held_by`, `detail.running` (its progress and `eta_s`) and a `Retry-After` header. Wait that long
  (or poll `/api/v1/status` until `busy` is false), then resubmit. Do not hammer it.
- **503**: the job would not fit in memory even with the GPU idle (`detail.available_gb` / `needed_gb`: another
  app holds memory), or the service is unavailable.
- **400**: invalid parameters (the message names the parameter); **404**: unknown service.

Other calls: `GET /api/v1/jobs?state=&service=&limit=` (history, newest first; files kept 7 days),
`POST /api/v1/jobs/<id>/cancel`, `DELETE /api/v1/jobs/<id>`.

## MCP tools

`status`, `list_services`, `submit_job(service, params, files=[{param, filename, data_base64}])`,
`get_job`, `wait_job(job_id, timeout_s≤120)` (call again while running), `list_jobs`, `cancel_job`,
`get_output` (images come back as an image; videos as a URL to download with the key).

Claude Code on the other PC:

```sh
claude mcp add --transport http openfield http://192.168.0.205:8765/mcp --header "X-Token: $OMLX_LAN_KEY"
```

## Tips

- Video jobs take many minutes: submit, then poll every 30-60 s (or `wait_job` repeatedly).
- For image-to-video, send keyframes in the clip's aspect ratio; they are scaled to cover and centre-cropped.
- Keep the returned `seed` to regenerate the same image with more steps or another size.
- `qwen-image` `negative_prompt` needs guidance above 1 (2.5 is used when guidance is omitted).
- With `cuts`, the video gets longer than its untouched audio by insert/fps seconds per cut.
