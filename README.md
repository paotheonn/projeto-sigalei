# Coleta de Diários Oficiais — Macapá (AP) e Fortaleza (CE)

Coletor e verificador dos últimos 60 dias dos diários oficiais municipais de **Macapá (AP)** e **Fortaleza (CE)**.

* **Intervalo coletado:** `2026-09-26` a `2026-11-25` (60 dias contados a partir de 25/11/2026; 26–27/09 foram fim de semana).
* **Resultado:** 78 edições únicas em `dados/diarios.jsonl` (16,7 MB) — 39 de Fortaleza (`18406`..`18444`, 28/09 a 25/11) e 39 de Macapá (`5383`..`5421`, 28/09 a 25/11).
* **Formato (JSONL):** uma edição por linha usando só a stdlib `json`, sem banco externo, sem problemas de escape com quebras de linha no texto completo e fácil de inspecionar em streaming.

## Como rodar

Pré-requisitos: Python 3.12+ e **Tesseract OCR** com idioma `por` (para PDFs escaneados de Macapá).

```bash
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

python coletar.py --inicio 2026-09-26 --fim 2026-11-25 --saida dados/diarios.jsonl
python verificar.py dados/diarios.jsonl --inicio 2026-09-26 --fim 2026-11-25 --checar-fonte
```

## Como cada fonte funciona e descobertas

**Fortaleza (`diariooficial.fortaleza.ce.gov.br`):** SPA React/Vite sobre API REST (`/api/captcha-required`, `POST /api/search`, `GET /api/nodes/{id}/content`) com PostgreSQL e Alfresco. Um `User-Agent` falsificado de Chrome aciona CAPTCHA do F5 BIG-IP; com `User-Agent` honesto (alinhado ao `robots.txt: Allow: /`) e pausa de `>= 5,5s` entre buscas (`intervalSeconds: 5.0` da própria API), funciona sem CAPTCHA. Encontramos **2 duplicatas idênticas na fonte** (mesmo SHA-256: `18434-t.pdf` e `18438 (1).pdf`), deduplicadas pelo coletor, e um erro da prefeitura na capa da edição `18435` (11/11/2026), impressa como `Nº 18.434`.

**Macapá (`macapa.ap.gov.br/diarios-oficiais/`):** WordPress + Laravel Livewire (`POST /livewire/update`, ações `setMonth`/`changeYear`). Três achados: (1) 35 das 39 edições são PDFs escaneados (ABBYY FineReader) sem camada de texto e 4 têm texto nativo, exigindo fallback automático para Tesseract OCR; (2) as abas mensais filtram por mês de upload no WordPress, não pela data da edição (por isso varremos também o mês seguinte); (3) o campo `id` omite sufixos como `-A`, presentes apenas no `title`.

**Mapeamento de tipos:** em Fortaleza, sufixo `s` no número (`18379s`) ou prefixo `Suplemento` → `suplemento`; `extra` → `extra`; demais → `normal`. Em Macapá, sufixo alfabético (`5234-A`, `4820-B` no histórico 2024–2026) → `extra`; `suplemento` → `suplemento`; demais → `normal`. No período de 60 dias não houve extras nem suplementos.

## Como sei que não faltou nenhuma edição

1. **Numeração sequencial sem furos:** Fortaleza `18406`..`18444` e Macapá `5383`..`5421` (sequencialidade declarada no expediente do DOM).
2. **Reconciliação cruzada (`--checar-fonte`):** o verificador cruza o JSONL com o sitemap XML de Macapá (descoberto via `sitemap_index.xml`) e com a busca paginada de Fortaleza — 100% batem.
3. **Calendário:** todas as pausas (incluindo bordas do período) coincidem com fins de semana e feriados (12/10, 30/10, 02/11, 20/11).

## Limites, riscos e uso de IA

* **Ficou de fora / com mais tempo:** checkpoint incremental para retomada, limpeza de duas colunas no OCR de Macapá e testes unitários dos parsers.
* **O que quebraria primeiro:** mudança no protocolo/nomes do Livewire em Macapá (`setMonth`, `tabDiaries`) ou alteração em `/api/search` / CAPTCHA obrigatório em Fortaleza.
* **Parte mais frágil do código:** a interação direta com `wire:snapshot` do Livewire (interno, sem contrato público) e o OCR a 150 DPI, que ocasionalmente pula a faixa estreita do número na capa de Macapá (sinalizado como aviso no verificador).
* **Uso de IA:** apoiou a inspeção de rede/JS das fontes e gerou o rascunho de `coletar.py` e `verificar.py`. Cortei classes e dependências extras (como BeautifulSoup), troquei o `User-Agent` frente ao F5, adicionei OCR automático para Macapá, deduplicação de reuploads em Fortaleza e aviso dedicado para divergência de número na capa após identificar o erro da edição `18435`.
