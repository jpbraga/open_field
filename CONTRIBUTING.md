# Contributing to openField

Thanks for helping. Bug reports, new services, fixes and documentation are all welcome.

## Before you start

- **Questions and ideas:** open a [discussion](https://github.com/jpbraga/open_field/discussions).
- **Bugs:** open an [issue](https://github.com/jpbraga/open_field/issues/new/choose) with the version, the
  machine and the steps. Remove access keys and private paths from logs before pasting them.
- **Security problems:** do not open an issue; follow [SECURITY.md](SECURITY.md).
- **Larger changes:** open an issue first, so we can agree on the approach before you write the code.

## Development setup

openField uses [uv](https://docs.astral.sh/uv/) and Python 3.12 or later.

```sh
git clone https://github.com/jpbraga/open_field.git
cd open_field
uv sync
uv run pytest -q
```

Run a development daemon next to an installed one with its own config and port:

```sh
uv run openfield --config ./config serve --port 8799
```

The web UI is plain HTML, CSS and JavaScript in `src/openfield/web/`, with no build step: reload the page after a
change.

## Adding a service

Most services need no Python: a YAML file in `config/services/` wraps a command line, an asynchronous HTTP API, an
OpenAI-compatible endpoint or a built-in function. [Adding a service](README.md#adding-a-service) in the README
describes every field. A good service:

- states its model, its license and where the weights come from;
- sets `memory_gb` and `estimate_s` from measurements, with the machine and numbers in a comment;
- parses the tool's progress, so the web UI and agents see more than "running";
- lists in `requires` the files it needs, so it shows as unavailable instead of failing;
- has `agent_notes` that tell an agent what the service is good at and how to call it.

Put the commands that install a service's tools in `scripts/<name>/setup.sh` and keep downloaded tools and model
weights out of the repository (`tools/` is ignored).

## Pull requests

- Keep each pull request to one change, with tests when it touches Python.
- `uv run pytest -q` must pass.
- Everything in the platform is in English: UI text, messages, docs, configs and comments.
- Update the README and `CHANGELOG.md` (under *Unreleased*) when behaviour changes.
- Never commit access keys, `.env` files, model weights or outputs.

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
