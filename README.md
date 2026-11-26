# Coleta de Diários Oficiais — Macapá (AP) e Fortaleza (CE)

> Coleta completa, legível e auditável dos últimos 60 dias de diários oficiais municipais (`2026-09-26` a `2026-11-25`) — sem classes desnecessárias e com prova cruzada na fonte.

![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue)
![Edições: 78](https://img.shields.io/badge/Edi%C3%A7%C3%B5es-78%20%C3%BAnicas-brightgreen)
![Completude: 0 furos](https://img.shields.io/badge/Completude-0%20furos%20%7C%200%20duplicatas-success)
![Saída: JSONL](https://img.shields.io/badge/Sa%C3%ADda-JSONL%20(16.7%20MB)-informational)

| Métrica | Fortaleza (CE) | Macapá (AP) | Total |
| --- | --- | --- | --- |
| **Período coletado** | `2026-09-28` a `2026-11-25` | `2026-09-28` a `2026-11-25` | 60 dias (`26/09`–`25/11`) |
| **Edições únicas** | 39 (`18406`..`18444`) | 39 (`5383`..`5421`) | **78** |
| **Extração** | 39 texto nativo | 4 texto nativo + 35 OCR | 100% legível |

* **Por que JSONL (`dados/diarios.jsonl`)?** Usa só a stdlib `json`, dispensa banco externo, não quebra com quebras de linha/aspas em textos longos (como CSV) e permite leitura em streaming.

## Quick Start

Pré-requisitos: **Python 3.12+** e **Tesseract OCR** com idioma `por` (para os PDFs escaneados de Macapá).

```bash
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

# 1. Reproduzir a coleta (HTTP 100% sequencial e gentil)
python coletar.py --inicio 2026-09-26 --fim 2026-11-25 --saida dados/diarios.jsonl

# 2. Verificar completude, duplicatas e legibilidade (retorna exit code 1 se falhar)
python verificar.py dados/diarios.jsonl --inicio 2026-09-26 --fim 2026-11-25 --checar-fonte
```

Saída esperada da verificação:

```text
- fortaleza: 39 edições | período 2026-09-28 a 2026-11-25 | numeração 18406..18444 | extração={'texto_nativo': 39}
- macapa:    39 edições | período 2026-09-28 a 2026-11-25 | numeração 5383..5421   | extração={'texto_nativo': 4, 'ocr': 35}
  [macapa]    OK — todas as 39 edições do sitemap batem 100% com o JSONL.
  [fortaleza] OK — todas as 39 edições únicas da API batem 100% com o JSONL.
VERIFICAÇÃO OK: coleta completa, sem duplicatas e com todos os textos legíveis.
```

## Como cada fonte funciona e o que descobri

* **Fortaleza (`diariooficial.fortaleza.ce.gov.br`):** SPA React sobre API REST (`/api/captcha-required`, `POST /api/search`, `GET /api/nodes/{id}/content`) com PostgreSQL + Alfresco. Um `User-Agent` falsificado de Chrome acionava CAPTCHA do F5 BIG-IP; com `User-Agent` honesto (`robots.txt: Allow: /`) e pausa `>= 5,5s` entre buscas (`intervalSeconds: 5.0` da própria API), roda sem CAPTCHA. Encontrei **2 reuploads duplicados idênticos** na fonte (mesmo SHA-256: `18434-t.pdf` e `18438 (1).pdf`), deduplicados pelo nome canônico, e um erro da prefeitura na capa da `18435` (`11/11`), impressa como `Nº 18.434`.
* **Macapá (`macapa.ap.gov.br/diarios-oficiais/`):** WordPress + Laravel Livewire (`POST /livewire/update`, ações `setMonth`/`changeYear`). Três achados: (1) 35 das 39 edições são PDFs escaneados (`ABBYY FineReader`, 0 chars nativos) e 4 têm texto nativo → fallback automático para Tesseract OCR local; (2) abas mensais filtram por mês de *upload*, não pela data da edição → varro também o mês seguinte; (3) `id` do Livewire corta sufixos como `-A`, preservados apenas no `title`.

| Fonte | Regra original (histórico 2024–2026) | Mapeamento |
| --- | --- | --- |
| **Fortaleza** | Sufixo `s` (`18379s`) ou prefixo `Suplemento` / termo `extra` / demais | `suplemento` / `extra` / `normal` |
| **Macapá** | Sufixo alfabético (`5234-A`, `4820-B`) / `suplemento` / demais | `extra` / `suplemento` / `normal` |

*(Nos últimos 60 dias não houve extras nem suplementos publicados em nenhuma das duas fontes.)*

## Como provo que não faltou nenhuma edição

* **Sequência sem furos:** Fortaleza `18406`..`18444` e Macapá `5383`..`5421` (sequencialidade confirmada no expediente do DOM).
* **Prova cruzada (`--checar-fonte`):** reconciliação simétrica contra o sitemap XML independente de Macapá (`sitemap_index.xml`) e a busca paginada de Fortaleza — 100% batem.
* **Calendário e testes negativos:** lacunas (inclusive nas bordas início/fim) coincidem com fins de semana e feriados (`12/10`, `30/10`, `02/11`, `20/11`); validado contra 6 defeitos sintéticos (todos falham com `exit code 1`).

## Limites, fragilidades e uso de IA

* **Com mais tempo:** checkpoint incremental (`--retomar`), decisão de OCR página a página (hoje é média por documento) e testes unitários dos parsers.
* **O que quebraria primeiro:** mudança nos métodos/payload do Livewire em Macapá (`setMonth`, `tabDiaries`) ou alteração em `/api/search` / CAPTCHA obrigatório em Fortaleza.
* **Parte mais frágil:** protocolo interno `wire:snapshot` do Livewire e OCR a 150 DPI, que às vezes pula a faixa estreita do número na capa de Macapá (sinalizado como aviso no verificador).
* **Uso de IA:** apoiou a inspeção de rede/JS e gerou o rascunho de `coletar.py` e `verificar.py`. Cortei classes e dependências extras (BeautifulSoup), troquei o `User-Agent` frente ao F5, adicionei OCR automático, deduplicação por nome canônico/SHA-256, corrigi `kwargs.pop("timeout")` no retry e incluí checagem de bordas e aviso para o erro da capa `18435`.
