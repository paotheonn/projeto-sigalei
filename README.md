# Coleta de Diários Oficiais — Macapá (AP) e Fortaleza (CE)

> Coleta completa, legível e indexada para **busca exata** dos últimos 60 dias (`2026-09-28` a `2026-11-27`), com validação página a página e prova cruzada independente na fonte.

| Métrica | Fortaleza (CE) | Macapá (AP) | Total |
| --- | --- | --- | --- |
| **Período** | `2026-09-28` a `2026-11-26` | `2026-09-28` a `2026-11-26` | 60 dias (`28/09`–`27/11`) |
| **Edições / páginas** | 40 (`18406`..`18445`) / 2.282 págs | 40 (`5383`..`5422`) / 1.126 págs | **80 edições / 3.408 págs** |
| **Extração (por pág.)** | 2.282 texto nativo | 68 nativo + 1.056 Document AI (`96,1%`) + 2 Gemini Flash | 100% legível |

* **Por que SQLite + FTS5 (`dados/diarios.db`, 25,4 MB)?** Usa só `sqlite3` nativo, guarda `edicoes`, `paginas` (texto limpo, método e confiança por página), uma `VIEW edicoes_completas` com o texto integral e o índice `paginas_fts` (`unicode61 remove_diacritics 2`, sem duplicar texto) para **busca exata** em milissegundos de nomes, CNPJs e números de processo.

## Quick Start

```bash
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

# 1. Coletar (usa Document AI se GOOGLE_APPLICATION_CREDENTIALS + DOCAI_PROCESSOR estiverem definidos; senão Tesseract)
python coletar.py --inicio 2026-09-28 --fim 2026-11-27 --saida dados/diarios.db --retomar

# 2. Verificar completude, FTS5 e qualidade página a página (exit code 1 se falhar)
python verificar.py dados/diarios.db --inicio 2026-09-28 --fim 2026-11-27 --checar-fonte

# 3. Buscar menções exatas (com ou sem acento/pontuação)
python buscar.py "VITÓRIA CRISTINA GIBSON"
python buscar.py --processo "6046681-54.2026.8.03.0001"
python buscar.py --cnpj "60.419.461/0001-25"
```

## Benchmark de OCR nas páginas escaneadas de Macapá

Em Macapá, 36 das 40 edições são PDFs escaneados em **2 colunas** (`ABBYY FineReader`, 0 chars nativos). Comparei **6 motores** nas mesmas 4 páginas difíceis da edição `5421` (págs 1, 5, 15 e 20 — 57 entidades reais: nomes, matrículas, processos CNJ, siglas e 5 decretos sublinhados):

| Motor | Entidades (57) | 2 colunas | Falha crítica observada | Custo (~1k págs) |
| --- | ---: | --- | --- | ---: |
| **Tesseract `fast` 150 / `best` 250** | 47/57 (82,5%) | OK | Errou 4/5 decretos (`B.243`, `5.244`), apagou 4/4 assinaturas na pág. 20, `EMDE:` | US\$ 0 |
| **RapidOCR (`PP-OCRv4`)** | Reprovado | Misturou | Tirou acentos e grudou nomes sem espaço | US\$ 0 |
| **Gemma 4 26B A4B IT** | Reprovado | Errou artigo | **Cortou metade da pág. 15** (`2.613` vs `4.738` chars) e trocou matrícula/decreto | ~US\$ 0,76 |
| **Mistral OCR (`25.05`)** | 43/57 | Nativo | Recortou 3/4 assinaturas como `![img]`, pôs LaTeX (`$8.243 / 2026$`), `Gisele→Gisela` | ~US\$ 1–4 |
| **Google Document AI + `boundingPoly`** | **56/57 (98,2%)** | **100% (coords)** | Zero omissão/alucinação, confiança por página (`96–98%`), 5/5 decretos e 100% CNJs | **~US\$ 0,06** *(1k grátis; US\$ 1,50/1k)* |
| **Gemini 3.8 Flash / 4 Hydrogen (`LOW`)** | **57/57 (100%)** | Nativo | Qualidade máxima, mas 5–16x mais caro que Document AI | ~US\$ 7 / US\$ 25 |

**Decisão:** **Google Document AI** com reconstrução determinística das 2 colunas via `boundingPoly` + **resgate automático com Gemini 3.8 Flash** nas páginas com confiança `< 0,85` ou texto curto (resgatou a pág. 40 da edição `5390` de 132 para 1.518 chars) + **fallback local Tesseract** sem conta GCP. Eliminou os 7 erros de leitura de capa que o Tesseract tinha em Macapá.

## Fontes, limpeza e prova de completude

* **Fortaleza (`diariooficial.fortaleza.ce.gov.br`):** API REST (`/api/captcha-required`, `POST /api/search`, `GET /api/nodes/{id}/content`). `User-Agent` honesto (`robots.txt: Allow: /`) e pausa `>= 5,5s` rodam sem CAPTCHA. Deduplica reuploads idênticos (`18434-t.pdf` e `18438 (1).pdf`) só quando o tamanho bate; registra aviso para o erro real da prefeitura na capa da `18435` (`Nº 18.434`).
* **Macapá (`macapa.ap.gov.br/diarios-oficiais/`):** Laravel Livewire (`POST /livewire/update`). Abas filtram por mês de upload (varro o mês seguinte) e `id` corta sufixos `-A` (extraídos do `title`).
* **Tipos:** Fortaleza (`s`/`Suplemento` → `suplemento`, `\bextra\b` → `extra`); Macapá (sufixo `-A`/`\bextra\b` → `extra`, `\bsuplemento\b` → `suplemento`); demais → `normal` (todas as 80 edições dos últimos 60 dias são `normal`).
* **Limpeza para busca exata:** decisão e auditoria **página por página**, des-hifenização (`CARVA-\nLHO` → `CARVALHO`, preservando `Político-Pedagógico` e `Macapá-AP`) e normalização de `N°`/`Art. 8°` → `Nº`/`Art. 8º`.
* **Completude:** numeração sem furos (`18406`..`18445` e `5383`..`5422`), reconciliação **independente** no `verificar.py` (sem importar `coletar.py`) contra `sitemap_index.xml` de Macapá e `/api/search` de Fortaleza, e 6 testes negativos sintéticos falhando com `exit code 1`.

## Limites e uso de IA

* **Fragilidades:** mudança no `wire:snapshot` do Livewire em Macapá ou CAPTCHA obrigatório em Fortaleza. **Com mais tempo:** quebra por ato individual (decreto/portaria) e suíte `pytest`.
* **Uso de IA:** apoiou a exploração das APIs, rascunho dos scripts e benchmark de OCR. As decisões principais (troca de `User-Agent`, benchmark de 6 motores nas páginas reais, descarte de Gemma 4 e Mistral OCR, ordenação por `boundingPoly`, migração para SQLite FTS5 e verificador independente) foram tomadas e validadas em cima dos dados reais.
