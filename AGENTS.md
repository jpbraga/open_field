# AGENTS.md

Guidance for AI agents working on openField: changing this repository, or installing and running openField on a
machine. An agent that only **uses** a running openField (submitting jobs over the API or MCP) needs
[`skills/openfield/SKILL.md`](skills/openfield/SKILL.md) instead, or the live version from `GET /api/v1/skill`.

openField is a local daemon that puts a machine's AI media services (image, video, sound, media tools, LLM
proxies) behind one port, with GPU slots and memory admission, a web UI, a REST API and an MCP server. Read the
[README](README.md) for the full picture; this file covers what an agent needs to work safely.

## Ground rules

- **Everything in the platform is in English:** UI text, API messages, docs, configs, comments, commit messages.
- **Never print, log, commit or copy access keys or tokens.** Configs reference secrets (`secrets:` in
  `daemon.yaml`, `{secret.NAME}` in templates); they never contain them. Remove keys and private paths from any
  log you quote.
- **Ask before anything that interrupts other people's work:** restarting the daemon while jobs run, stopping or
  unloading a model another tool serves, changing the listen address, ports or firewall.
- **Only stop processes you started.** A job's process belongs to the daemon; cancel it through the API.
- **Confirm before deleting** jobs, uploads, the data directory or model weights.
- **Downloaded tools and model code are untrusted:** review them before running, keep them under `tools/` (ignored
  by git), and pin versions or commits.

## Repository map

| Path | What it holds |
|---|---|
| `src/openfield/api.py` | FastAPI app: REST API, web UI routes, auth middleware |
| `src/openfield/mcp_server.py` | MCP server (streamable HTTP at `/mcp`) |
| `src/openfield/jobs.py` | Job lifecycle: admission, running, cancel, retry, retention |
| `src/openfield/resources.py` | Resource slots and conflicts (`409` / `503` answers) |
| `src/openfield/runner.py` | Child processes, output lines, progress parsing (`StepProgress`) |
| `src/openfield/adapters/` | `shell`, `http_job`, `http_proxy`, `builtin` |
| `src/openfield/config.py`, `params.py`, `expr.py` | YAML models, parameter validation, restricted expressions |
| `src/openfield/auth.py`, `links.py`, `uploads.py` | Keys and loopback access, signed links, staged uploads |
| `src/openfield/service_admin.py`, `service_templates/` | Service editor in the web UI and its YAML templates |
| `src/openfield/agent_docs.py` | The generated consumer skill (`/api/v1/skill`, `openfield skill`) |
| `src/openfield/web/` | Web UI: plain HTML, CSS and JavaScript, no build step |
| `config/daemon.yaml`, `config/services/*.yaml` | Default daemon config and one file per service |
| `scripts/<name>/` | Setup and runner scripts for services that need their own environment |
| `skills/openfield/SKILL.md` | Example of the generated consumer skill |
| `docs/` | The project website (GitHub Pages) |
| `tests/` | pytest suite |

## Developing

```sh
uv sync                 # Python 3.12+, dependencies from uv.lock
uv run pytest -q        # must pass before any commit
uv run openfield check  # validates the config and lists which services are available
```

To run a development daemon next to an installed one, give it **its own config copy with a different
`data_dir` and port**. The repository's `config/daemon.yaml` uses the same data directory as an installation, so
a second daemon on it would share the database and job files:

```sh
cp -R config /tmp/openfield-dev && sed -i.bak 's|^data_dir:.*|data_dir: /tmp/openfield-dev/data|' /tmp/openfield-dev/daemon.yaml
uv run openfield --config /tmp/openfield-dev serve --host 127.0.0.1 --port 8799
```

- The web UI needs no build: edit `src/openfield/web/` and reload the page. Check light and dark mode and a phone
  width; browser tests with Playwright work well against a development daemon.
- A behaviour change updates the README and `CHANGELOG.md` (under *Unreleased*).
- The version lives in `pyproject.toml` and `src/openfield/__init__.py`; run `uv lock` after changing it.
- After changing services or their `agent_notes`, regenerate the example skill:
  `uv run openfield --config config skill --url "http://<openfield-host>:8765" -o skills/openfield/SKILL.md`.
- Commit only what belongs in git: no `tools/`, model weights, job outputs, data directories or `.env` files.

## Adding or changing a service

A service is a YAML file in `config/services/`; [Adding a service](README.md#adding-a-service) documents every
field. What makes one work well:

1. **Measure, then declare.** Run the real tool a few times and set `memory_gb` (peak memory) and `estimate_s` as
   expressions over the parameters; write the machine and the measurements in a comment.
2. **Report progress.** Parse the tool's output with a `progress` regex or `phases`. Check that the totals it prints
   match `total_equals` (rounding can differ: mflux runs `steps - max(1, int(steps * (1 - strength)))` steps). For
   PyTorch on MPS or MLX, synchronize before printing a step, or every step prints at once.
3. **Fail as unavailable, not at run time.** List the binaries and weights in `requires` (globs).
4. **Hold the model in one stage** (`holds_model`) and name the stage, so the UI shows `loading` and `loaded`.
5. **Tell agents how to use it** in `agent_notes`; they end up in the generated skill.
6. **Install its tools reproducibly** with a `scripts/<name>/setup.sh` that pins versions, into `tools/`.

Then run `openfield check`, reload the services (web UI **Reload services** or `POST /api/v1/admin/reload`) and run
a real job. Reloading needs no restart and leaves running jobs alone. Services that run commands (`shell`,
`builtin`) or use secrets can only be created or edited from the machine itself.

## Operating an installation

### Install and update

```sh
curl -fsSL https://raw.githubusercontent.com/jpbraga/open_field/main/install.sh | sh     # macOS, Linux
irm https://raw.githubusercontent.com/jpbraga/open_field/main/install.ps1 | iex           # Windows PowerShell
```

The installer sets up uv and Python, installs openField (`~/.openfield`; Windows `%LOCALAPPDATA%\openfield\app`),
creates the user config and starts the daemon at login. Running it again updates the installation; restart the
daemon afterwards (below). `--with-tools` also downloads Real-ESRGAN and RIFE.

### Where things are

| What | Where |
|---|---|
| Config | `~/.config/openfield/` (Windows `%APPDATA%\openfield`); `OPENFIELD_CONFIG` overrides it, and a checkout without a user config uses its own `config/` |
| Data | `data_dir` in `daemon.yaml` (default `~/.local/share/openfield`): `openfield.db`, `jobs/`, `uploads/`, `keys.json`, `signing.key` |
| Log | `<data_dir>/daemon.log` |
| Tools | `tools/` in the installation (Real-ESRGAN, RIFE, per-service environments) |

### Checking on it

- `GET /api/v1/health`: version, number of services, config errors.
- `GET /api/v1/status`: slots in use, running jobs with progress and ETA, free memory.
- `GET /api/v1/services`: each service's `available` flag and `unavailable_reason`.
- `openfield check` and `openfield service status` from a shell on the machine.

Requests from the machine itself need no key; from other machines they need one in `X-Token` or
`Authorization: Bearer`.

### Reload or restart

| Change | What to do |
|---|---|
| A service file added, edited or removed | Reload: web UI **Reload services** or `POST /api/v1/admin/reload` |
| `daemon.yaml` network or data settings, a code update | Restart the daemon |

**Before restarting, check `GET /api/v1/status`.** A restart cancels running jobs: they end as `error`
("interrupted") and can be retried, but their callers lose the result. If jobs are running or other machines rely
on the daemon, wait for it to be idle or ask the user. To restart:

- macOS: `openfield service install` (reloads the launchd agent);
- Linux: `systemctl --user restart openfield.service`;
- Windows (PowerShell): `Stop-ScheduledTask -TaskName openField; Start-ScheduledTask -TaskName openField`.

### Keys and access

- `openfield key` lists keys; `openfield key --new <name>` creates an agent key and `--role admin` an admin key;
  `openfield key --revoke <id|name>` revokes one. A key is shown once and stored only as a hash.
- Give each agent or machine its own key, so it can be revoked alone. Hand the key over through the user, never in
  a file in a repository.
- Agent keys use the API, MCP, proxies and jobs; admin keys also manage keys, services and settings.

### GPU slots and memory

The **System** tab (or `PUT /api/v1/settings` with an admin key) sets how many jobs may use the GPU at once, from 1
to `max_slots`. Every job also needs free memory for its model (`memory_gb` plus `memory_margin_gb`), and GPU tools
started outside the daemon (`detect_external`) take a slot. More slots only pay off for light jobs next to heavy
ones: jobs that run together split the GPU.

### Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| A service shows as unavailable | Its `unavailable_reason` names the missing file: install the tool or weights, then reload |
| `409` on submit | No free slot or not enough memory next to running jobs: retry after `Retry-After` |
| `503` on submit | The job cannot fit even with the GPU idle: lower its size or free memory |
| A job stays at "loading" with no progress | The progress regex does not match the tool's output: read the job's `log_tail` |
| Times far from the estimate | `estimate_s` does not model a parameter (guidance, reference images...): measure and refit |
| Config changes have no effect | `openfield check` shows the errors; `daemon.yaml` network and data changes need a restart |
