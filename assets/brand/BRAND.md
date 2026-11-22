# openField — identidade visual

![openField](social-preview.png)

## Conceito

**Um campo aberto ao amanhecer.** O símbolo é um sol nascendo no horizonte sobre fileiras de pontos em
perspectiva. Os pontos são o campo: os serviços, os jobs, as possibilidades. Um único ponto aceso na cor do
sol é o job rodando agora, a ideia central do openField: a GPU usada com critério, cada job admitido só
quando há vaga e recursos.

- **open:** local, aberto, sem nuvem; peso leve no nome.
- **Field:** o campo de trabalho; peso forte no nome.

## Logo

| Arquivo | Uso |
|---|---|
| `openfield-logo.svg` | Logo horizontal sobre fundos claros |
| `openfield-logo-dark.svg` | Logo horizontal sobre fundos escuros |
| `openfield-mark.svg` / `openfield-mark-dark.svg` | Só o símbolo (claro / escuro) |
| `openfield-icon.svg`, `openfield-icon-512.png` | Ícone de app (quadrado arredondado tinta) |
| `openfield-favicon.svg` | Favicon simplificado, legível em 16 px |
| `social-preview.png` | Prévia social do repositório (1280×640) |

O texto do logo está convertido em curvas, então os arquivos não dependem da fonte instalada. Para gerar
tudo de novo a partir da mesma geometria:

```sh
uv run --no-project --with uharfbuzz --with fonttools python scripts/build_brand.py
```

Regras: mantenha uma área livre ao redor do logo igual à altura do sol; não mude as cores do sol nem o
ponto aceso; o nome se escreve **openField** (o minúsculo, F maiúsculo).

## Cores

| Nome | Claro | Escuro | Uso |
|---|---|---|---|
| Tinta | `#15181D` | `#ECEAE3` (texto) | Texto, símbolo, ícone |
| Papel | `#F6F4EE` | `#101317` (fundo) | Fundo |
| Painel | `#FFFFFF` | `#171A20` | Cartões |
| Sol (gradiente) | `#FFB341` → `#FF5E1A` | igual | Sol, botões principais, progresso |
| Ponto aceso | `#FF7A1A` | igual | O job em execução, foco |
| Brasa | `#C2410C` | `#FF8A3D` | Links e textos de destaque (contraste AA) |
| Campo | `#2F8F5B` | `#5BC48A` | GPU livre, sucesso |
| Erro | `#C53030` | `#F07171` | Erros, cancelar |

Texto sobre o gradiente do sol é sempre **Tinta** (contraste acima de 7:1).

## Tipografia

**Outfit** (SIL Open Font License, arquivo variável 100–900, em `src/openfield/web/fonts/`): 400 para
texto, 500–600 para títulos e botões. Números tabulares na interface. Código em `ui-monospace`.

## Elementos

- **Campo de pontos:** fundo da tela de login (pontos a cada 22 px) com o brilho do sol nascendo na base.
- **Ponto pulsando:** a GPU ocupada e o job em execução.
- **Barra de progresso:** o gradiente do sol, com um brilho que corre enquanto o job roda.
- **Vagas:** círculos que acendem na cor do sol, um por job em paralelo.
