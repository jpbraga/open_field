<p align="center"><img src="assets/brand/openfield-logo.svg" alt="openField" height="64"></p>

<p align="center"><b>Serviços de IA locais, um campo aberto.</b><br>
Geração e edição de imagem, vídeo com som, pós-processamento de vídeo e LLMs locais atrás de um daemon só,<br>
com controle de recursos da GPU, interface web, API REST e servidor MCP.</p>

---

O openField reúne os serviços de IA de um Mac (feito num M5 Max de 128 GB) atrás de uma porta. Ele decide
quantos jobs a GPU roda ao mesmo tempo e só aceita um job novo quando há vaga **e** memória para o modelo
dele. Tudo aparece numa interface web, e agentes de outros computadores usam a API REST ou o MCP.

- Interface web: `http://<mac>:8765/` (login com uma chave aceita pelo oMLX).
- API REST: `http://<mac>:8765/api/v1/...` (documentação interativa em `/api/docs`).
- MCP (streamable HTTP): `http://<mac>:8765/mcp`.
- Skill para agentes consumidores: [`skills/openfield/SKILL.md`](skills/openfield/SKILL.md).
- Identidade visual: [`assets/brand/BRAND.md`](assets/brand/BRAND.md).

## Como funciona

| Peça | Onde | O que faz |
|---|---|---|
| Configuração | `config/daemon.yaml`, `config/services/*.yaml` | Porta, recursos, retenção; um arquivo por serviço |
| Adaptadores | `src/openfield/adapters/` | `shell`, `http_job`, `http_proxy`, `builtin` |
| Execução | `jobs.py`, `resources.py`, `runner.py` | Admissão, vagas, processos, progresso, cancelamento |
| Dados | `~/.local/share/openfield/` | `openfield.db` (SQLite) e `jobs/<id>/` (entradas e saídas) |
| Interface | `src/openfield/web/` | HTML/JS sem build, servido pelo próprio daemon |

### Vagas e checagem de recursos

Cada serviço declara os recursos que usa (`resources: [gpu]`). Um recurso tem **vagas**: quantos jobs podem
usá-lo ao mesmo tempo. O número é um ajuste do sistema, de 1 a `max_slots`, escolhido na aba **Sistema** da
interface (ou `PUT /api/v1/settings`); o padrão vem do `daemon.yaml` e é 1.

Um job novo só começa se:

1. **há uma vaga livre** no recurso. Ferramentas de GPU rodando fora do daemon (`mflux-generate*`, `sd-cli`...)
   ocupam uma vaga cada;
2. **a memória livre cobre o modelo dele:** memória livre agora, menos o que os jobs que ainda estão
   carregando o modelo vão ocupar, precisa ser maior que o `memory_gb` do serviço mais `memory_margin_gb`;
3. há espaço em disco.

A política é `reject`: nada fica em fila. Sem vaga, ou sem memória enquanto outros jobs rodam, a resposta é
**409** com o que está rodando e o ETA no `Retry-After`. Se o job não caberia nem com a GPU vazia, a resposta
é **503**. Serviços `shared` (o proxy de LLM) ocupam uma vaga juntos, até `max_concurrency` chamadas.

Mais de um job ao mesmo tempo divide a GPU, então cada um fica mais lento. Vale a pena para jobs leves (um
upscale ao lado de uma imagem), não para dois vídeos.

### Modelos

Cada job roda o modelo num processo próprio: carrega no início e libera quando o processo termina. A interface
e a API mostram o estado do modelo (`loading`, `loaded`, `released`).

### Reinício e retenção

Um job interrompido por um reinício do daemon fica como `error` ("interrupted"). Os arquivos de jobs
concluídos ficam 7 dias e os registros 90 (ajustável em `retention`).

## Rodar

```sh
uv sync
.venv/bin/openfield check                 # valida a configuração e mostra quais serviços estão disponíveis
.venv/bin/openfield serve                 # em primeiro plano (porta do daemon.yaml)
.venv/bin/openfield serve --port 8766     # outra porta, para testes
```

Como serviço do macOS (inicia no login e reinicia se cair): `./scripts/install-launchd.sh`. Logs em
`~/.local/share/openfield/daemon.log`. Para reiniciar:
`launchctl kickstart -k gui/$(id -u)/com.openfield.daemon`.

Os binários do Real-ESRGAN e do RIFE (ncnn-vulkan, releases oficiais de macOS) ficam em `tools/`, fora do git:
`tools/realesrgan/` (Real-ESRGAN v0.2.5.0) e `tools/rife/` (rife-ncnn-vulkan 20221029).

## Adicionar um serviço

Crie `config/services/<id>.yaml` e clique em **Recarregar serviços** na aba Serviços (ou
`POST /api/v1/admin/reload`). `openfield check` mostra erros de configuração.

Campos comuns:

```yaml
id: meu-servico               # letras, números, - e _
name: Nome na interface
description: O que faz, para pessoas e agentes
category: image               # image, video, video-post, llm, utility...
kind: shell                   # shell | http_job | http_proxy | builtin
resources: [gpu]              # vagas que o serviço ocupa ([] = roda mesmo com a GPU cheia)
lock: exclusive               # ou shared (com max_concurrency)
model: Nome do modelo (mostrado no status)
requires: [~/caminho/para/binario]   # globs; o serviço fica indisponível se faltar
memory_gb: "40 + 4*steps/10"  # expressão sobre os parâmetros; usada na admissão
estimate_s: "30 + 2*steps"    # para o ETA antes do primeiro progresso
params:
  prompt: {type: string, required: true, max_length: 4000, description: ...}
  steps: {type: int, default: 30, min: 1, max: 100}
  seed: {type: int, default: random}        # "random" sorteia uma seed
  size: {type: enum, values: [512, 1024], default: 1024}
  refs: {type: files, max_files: 4, accept: [image], max_mb: 40}   # file / files chegam por multipart
  extra: {type: json}                       # lista ou objeto
derive:                       # parâmetros calculados (podem sobrescrever ou criar nomes)
  steps: "steps if steps else 30"
validate:                     # regras com mensagem de erro
  - {expr: "size*size <= 1048576", message: no máximo 1 MP}
```

**Expressões** (`memory_gb`, `estimate_s`, `derive`, `validate`, `when`) são Python restrito: aritmética,
comparações, `and/or/not`, `x if c else y`, listas, `r["campo"]`/`r.campo` em dicionários e as funções `min`,
`max`, `round`, `int`, `float`, `len`, `ceil`, `floor`, `sqrt`, `abs`, `snap_up(valor, início, passo, máximo)`.
**Templates** trocam `{nome}` pelo valor; `{work}` é a pasta do job.

### `shell`: comandos

```yaml
kind: shell
env: {HF_HUB_OFFLINE: "1"}
steps:
  - name: gerar
    stage: carregando o modelo       # texto mostrado enquanto roda
    holds_model: true                # este processo segura o modelo (loading -> loaded -> released)
    weight: "steps"                  # peso da etapa no progresso total (expressão)
    when: "refs"                     # opcional: só roda se a expressão for verdadeira
    command: [~/bin/ferramenta, --prompt, "{prompt}", --steps, "{steps}", --refs, "{refs}", --out, "{work}/out.png"]
    optional_args:
      - {when: "seed is not None", args: [--seed, "{seed}"]}
    progress:
      kind: regex                    # regex | ffmpeg | files | none
      regex: '(?P<done>\d+)/(?P<total>\d+) \['
      total_equals: steps            # ignora barras com outro total (ex.: carregamento de pesos)
      start_after: 'sampling start'  # opcional
      label: "passo {done}/{total}"
      scale_from: 0.05
      scale_to: 0.95
      phases: [{regex: 'decoding', progress: 0.96, label: decodificando}]
outputs:
  - {name: image, path: "{work}/out.png", media_type: image/png}
```

Um `{param}` sozinho num argumento vai literal (uma lista vira vários argumentos). Caminhos do próprio YAML
começando com `~` ou `/` com `*` resolvem para o arquivo mais recente (ex.:
`~/.unsloth/stable-diffusion.cpp/sd-*/sd-cli`); valores do usuário nunca são expandidos.

### `http_proxy`: repasse de uma API

```yaml
kind: http_proxy
resources: [gpu]
lock: shared
max_concurrency: 8
proxy:
  upstream: http://127.0.0.1:8010
  upstream_auth: passthrough     # passthrough | headers | none
  # headers: {Authorization: "Bearer {secret.omlx_api_key}"}   # com upstream_auth: headers
```

Clientes chamam `/proxy/<id>/<caminho>`. O streaming (SSE) passa direto, e cada chamada, aceita ou recusada,
fica registrada na aba Requisições.

### `http_job`: API assíncrona (enviar, acompanhar, baixar)

```yaml
kind: http_job
http:
  submit: {method: POST, url: "http://127.0.0.1:9000/generate", headers: {Authorization: "Bearer {secret.minha_chave}"},
           json: {prompt: "{prompt}", image: "{b64.ref}"}}
  poll: {url: "http://127.0.0.1:9000/jobs/{submit.id}", interval_s: 2, done: "r.status == 'done'",
         error: "r.error", progress: "r.progress", stage: "r.phase"}
  fetch: [{name: image, url: "http://127.0.0.1:9000/files/{r.file_id}", media_type: image/png, filename: out.png}]
  cancel: {method: POST, url: "http://127.0.0.1:9000/jobs/{submit.id}/cancel"}
```

`submit` e `r` são as respostas JSON; `b64.<param>` é um arquivo enviado como data URL; `secret.<nome>` vem
de `secrets` no `daemon.yaml` (lido na hora, nunca gravado nem registrado).

### `builtin`: código Python

Para fluxos que não cabem numa lista de comandos (`handler: video_post`, `handler: probe`). Novos handlers
entram em `adapters/builtin.py` (`HANDLERS`).

## API

| Método e caminho | |
|---|---|
| `GET /api/v1/health` | Aberto: versão, GPU, número de serviços |
| `GET /api/v1/status` | Vagas por recurso (usadas, livres, quem usa), jobs rodando (etapa, progresso, ETA, modelo), memória, último concluído |
| `GET /api/v1/settings`, `PUT /api/v1/settings` | Vagas por recurso: `{"resources": {"gpu": {"slots": 2}}}` |
| `GET /api/v1/services` | Serviços com esquema de parâmetros e disponibilidade |
| `POST /api/v1/jobs` | JSON `{"service", "params"}` ou multipart (`job` + arquivos). 202 / 409 / 503 / 400 |
| `GET /api/v1/jobs` | Histórico (`state`, `service`, `limit`, `offset`) |
| `GET /api/v1/jobs/{id}` | Detalhes, log, resultado, `output_urls` |
| `GET /api/v1/jobs/{id}/outputs/{nome}` | Arquivo (`?download=true` para baixar) |
| `POST /api/v1/jobs/{id}/cancel`, `DELETE /api/v1/jobs/{id}` | Cancelar, excluir |
| `GET /api/v1/requests` | Registro das chamadas de proxy |
| `/proxy/{id}/...` | Proxies |
| `/mcp` | MCP: `status`, `list_services`, `submit_job`, `get_job`, `wait_job`, `list_jobs`, `cancel_job`, `get_output` |

Autenticação: `X-Token` ou `Authorization: Bearer` com uma chave aceita pelo oMLX (a chave admin aparece como
"admin"; as sub-chaves, pelo nome). A interface web usa um cookie de sessão de 7 dias.

## Testes

```sh
.venv/bin/pytest -q
```

## Licenças de terceiros

A fonte Outfit (`src/openfield/web/fonts/`) é distribuída sob a SIL Open Font License 1.1 (`OFL.txt`).
