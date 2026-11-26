# Coleta de Diários Oficiais — Macapá (AP) e Fortaleza (CE)

Coletor e verificador dos últimos 60 dias dos diários oficiais de **Macapá (AP)** e **Fortaleza (CE)**, construído priorizando simplicidade, rastreabilidade e prova de completude.

| Métrica | Fortaleza (CE) | Macapá (AP) | Total |
| --- | --- | --- | --- |
| **Período solicitado** | `2026-09-26` a `2026-11-25` | `2026-09-26` a `2026-11-25` | 60 dias |
| **Edições coletadas** | 39 (`28/09` a `25/11`) | 39 (`28/09` a `25/11`) | **78** |
| **Numeração** | `18406`..`18444` (sem furos) | `5383`..`5421` (sem furos) | Contínua |
| **Extração de texto** | 39 texto nativo | 4 texto nativo + 35 OCR | 100% legível |

* **Saída (`dados/diarios.jsonl`, 16,7 MB):** escolhi **JSONL** por usar só a stdlib `json`, não exigir banco externo, preservar textos longos com quebras de linha sem problemas de escape (ao contrário de CSV) e permitir leitura em streaming.

## Como rodar

Pré-requisitos: **Python 3.12+** e **Tesseract OCR** com idioma português (`por`) instalado no sistema (para os PDFs escaneados de Macapá).

```bash
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

# Reproduzir a coleta (requisições HTTP sequenciais com pausa)
python coletar.py --inicio 2026-09-26 --fim 2026-11-25 --saida dados/diarios.jsonl

# Verificar completude, duplicatas e legibilidade (exit code 1 em caso de falha)
python verificar.py dados/diarios.jsonl --inicio 2026-09-26 --fim 2026-11-25 --checar-fonte
```

## Como cada fonte funciona e o que descobri

### Fortaleza (`diariooficial.fortaleza.ce.gov.br`)
SPA React/Vite sobre API REST (`/api/captcha-required`, `POST /api/search`, `GET /api/nodes/{id}/content`) com PostgreSQL e Alfresco.
* **WAF e gentileza:** um `User-Agent` falsificado de Chrome disparou CAPTCHA do F5 BIG-IP; usando um `User-Agent` honesto (alinhado ao `robots.txt: Allow: /`) e pausa `>= 5,5s` entre buscas (`intervalSeconds: 5.0` da própria API), a coleta roda sem CAPTCHA.
* **Duplicatas na fonte:** encontramos 2 reuploads idênticos byte a byte (mesmo SHA-256: `18434-t.pdf` e `18438 (1).pdf`), deduplicados preferindo o nome canônico. Também detectamos um erro da prefeitura na capa da edição `18435` (`11/11/2026`), impressa como `Nº 18.434`.

### Macapá (`macapa.ap.gov.br/diarios-oficiais/`)
WordPress com Laravel Livewire (`POST /livewire/update`, ações `setMonth` e `changeYear`).
* **PDFs escaneados:** 35 das 39 edições são scans (ABBYY FineReader) sem camada de texto e 4 têm texto nativo — o coletor aplica fallback automático para Tesseract OCR local.
* **Abas e sufixos:** as abas mensais filtram pelo mês de *upload* no WordPress, não pela data da edição (por isso consultamos também o mês seguinte); e o campo `id` omite sufixos como `-A`, preservados apenas no `title`.

**Mapeamento de tipos:** Fortaleza usa sufixo `s` (`18379s`) ou prefixo `Suplemento` → `suplemento`; `extra` → `extra`; demais → `normal`. Macapá usa sufixo alfabético (`5234-A`, `4820-B` no histórico 2024–2026) → `extra`; `suplemento` → `suplemento`; demais → `normal`. No recorte de 60 dias não houve extras nem suplementos.

## Como sei que não faltou nenhuma edição

1. **Sequência sem furos:** Fortaleza `18406`..`18444` e Macapá `5383`..`5421` (numeração sequencial declarada no expediente do DOM).
2. **Prova cruzada (`--checar-fonte`):** reconciliação simétrica com o sitemap XML de Macapá (descoberto via `sitemap_index.xml`) e com a busca paginada de Fortaleza — 100% de correspondência.
3. **Calendário:** todas as lacunas (incluindo bordas do período) coincidem com fins de semana e feriados (`12/10`, `30/10`, `02/11`, `20/11`).

## Limites, fragilidades e uso de IA

* **Com mais tempo:** retomada incremental por checkpoint, decisão de OCR página a página (hoje é por documento) e testes unitários dos parsers.
* **Quebraria primeiro:** alteração nos métodos/payload do Livewire em Macapá (`setMonth`, `tabDiaries`) ou mudança em `/api/search` / CAPTCHA fixo em Fortaleza.
* **Parte mais frágil:** acoplamento ao `wire:snapshot` do Livewire (protocolo interno sem contrato público) e OCR a 150 DPI, que às vezes pula a faixa estreita do número na capa de Macapá (sinalizado como aviso no verificador).
* **Uso de IA:** apoiou a inspeção das fontes e gerou o rascunho inicial dos scripts. Cortei classes e dependências extras (como BeautifulSoup), troquei o `User-Agent` frente ao F5, adicionei OCR automático, deduplicação por nome canônico/SHA-256, corrigi `kwargs.pop("timeout")` no retry e incluí checagem de bordas e aviso para o erro da capa `18435`.
