# Changelog

All notable changes to openField. Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

- `openfield --version`.
- Project website on GitHub Pages, screenshots, community files (contributing, security policy, issue and pull
  request templates), continuous integration and package metadata.

## [0.2.1] - 2026-11-23

- Jobs list and grid: a running job's cover animates and follows the job's progress, with "Loading" while its
  model loads; audio outputs get a waveform cover with their duration.
- `qwen-image-edit`: progress now shows with `strength` below 1 (mflux runs
  `steps - max(1, int(steps * (1 - strength)))` steps), and the time estimate was refitted on 76 jobs.

## [0.2.0] - 2026-11-23

- `flux2-image`: FLUX.2 [dev] generates or edits images with up to 10 references, in two processes (text encoder,
  then transformer) with a cache of prompt embeddings and an optional prompt enhancer.
- Sound effects: `stable-audio-sfx` (Stable Audio 3 Small SFX) and `moss-sfx` (MOSS-SoundEffect 2.0); audio
  outputs report duration, sample rate and channels.
- Web UI: list or grid view of the jobs, 1 to 8 per row.
- Retry a failed or cancelled job from the web UI, `POST /api/v1/jobs/{id}/retry` or the `retry_job` MCP tool.
- Restarts no longer leave job processes behind.

## [0.1.0] - 2026-11-22

- First public version: one daemon for image, video and media services and LLM proxies, with GPU slots, memory
  admission, a web UI, a REST API and an MCP server.
- Access without a key on the machine itself; access keys per agent from other machines, stored as hashes.
- Agent onboarding in the web UI: MCP commands, REST examples and a generated `SKILL.md`.
- Service editor in the web UI, with templates for the `shell`, `http_job` and `http_proxy` adapters.
- Large files: staged, resumable uploads and signed download links that need no key.
- One-line installers for macOS, Linux and Windows.

[Unreleased]: https://github.com/jpbraga/open_field/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/jpbraga/open_field/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jpbraga/open_field/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jpbraga/open_field/releases/tag/v0.1.0
