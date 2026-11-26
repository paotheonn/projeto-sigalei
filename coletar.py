"""Coletor dos Diários Oficiais de Macapá (AP) e Fortaleza (CE).

Uso:
    python coletar.py --inicio 2026-09-26 --fim 2026-11-25 --saida dados/diarios.jsonl
"""

import argparse
import concurrent.futures
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pymupdf
import requests

USER_AGENT = "SigaleiChallengeBot/1.0 (+contato-desafio-tecnico)"
FUSO_BRASILIA = timezone(timedelta(hours=-3))

MACAPA_URL = "https://macapa.ap.gov.br/diarios-oficiais/"
MACAPA_LIVEWIRE_URL = "https://macapa.ap.gov.br/livewire/update"

FORTALEZA_BASE = "https://diariooficial.fortaleza.ce.gov.br"


def encontrar_tesseract() -> str:
    """Localiza o binário do Tesseract no PATH ou no caminho padrão do Windows."""
    caminho = shutil.which("tesseract")
    if caminho:
        return caminho
    padrao_win = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(padrao_win):
        return padrao_win
    raise RuntimeError(
        "Tesseract OCR não encontrado. Instale o Tesseract com o pacote de idioma 'por'."
    )


def criar_sessao() -> requests.Session:
    sessao = requests.Session()
    sessao.headers.update({"User-Agent": USER_AGENT})
    return sessao


def requisitar_com_retry(sessao: requests.Session, metodo: str, url: str, tentativas: int = 3, **kwargs) -> requests.Response:
    """Faz requisição HTTP com backoff simples para falhas transitórias (rede, 429, 5xx)."""
    timeout = kwargs.pop("timeout", 45)
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            resp = sessao.request(metodo, url, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as err:
            status = err.response.status_code if err.response is not None else 0
            if status != 429 and status < 500:
                raise
            ultimo_erro = err
        except requests.RequestException as err:
            ultimo_erro = err

        if tentativa < tentativas:
            espera = 2 * tentativa
            print(f"  [retry {tentativa}/{tentativas}] erro em {url}: {ultimo_erro} (aguardando {espera}s)", file=sys.stderr)
            time.sleep(espera)
    raise ultimo_erro


def meses_no_intervalo(inicio: date, fim: date, incluir_mes_seguinte: bool = True) -> list[tuple[int, int]]:
    """Retorna lista ordenada de pares (ano, mes) que cobrem [inicio, fim].

    Quando incluir_mes_seguinte=True, adiciona o mês posterior a `fim` (até o mês atual),
    pois Macapá agrupa abas pela data de upload no WordPress (ex.: edição de 30/01 na aba 02).
    """
    hoje = datetime.now(FUSO_BRASILIA).date()
    limite = fim
    if incluir_mes_seguinte:
        prox_ano = fim.year + (1 if fim.month == 12 else 0)
        prox_mes = 1 if fim.month == 12 else fim.month + 1
        candidato = date(prox_ano, prox_mes, 1)
        if candidato <= date(hoje.year, hoje.month, 1):
            limite = candidato

    meses = []
    ano, mes = inicio.year, inicio.month
    while (ano, mes) <= (limite.year, limite.month):
        meses.append((ano, mes))
        if mes == 12:
            ano, mes = ano + 1, 1
        else:
            mes += 1
    return meses


def normalizar_texto(texto: str) -> str:
    """Remove caracteres nulos, normaliza quebras de linha e espaços excessivos."""
    texto = texto.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


def ocr_pagina_png(args: tuple[str, bytes]) -> str:
    tesseract_bin, png_bytes = args
    proc = subprocess.run(
        [tesseract_bin, "stdin", "stdout", "-l", "por", "--oem", "1", "--psm", "3"],
        input=png_bytes,
        capture_output=True,
        check=True,
    )
    return proc.stdout.decode("utf-8", errors="replace")


def extrair_texto_pdf(pdf_bytes: bytes, ocr_workers: int = 8) -> tuple[str, str, int]:
    """Extrai texto de um PDF. Usa camada nativa; se escaneado, aplica OCR com Tesseract.

    Retorna: (texto, metodo_extracao, total_paginas)
    """
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        total_paginas = len(doc)
        textos_nativos = [pagina.get_text("text") for pagina in doc]
        texto_nativo = normalizar_texto("\n\n".join(textos_nativos))

        # Se há camada de texto real (>= 30 caracteres médios por página), usa direto
        if total_paginas > 0 and len(texto_nativo) / total_paginas >= 30:
            return texto_nativo, "texto_nativo", total_paginas

        # PDF escaneado (caso de Macapá): renderiza páginas em tons de cinza e roda OCR local
        tesseract_bin = encontrar_tesseract()
        imagens = [
            (tesseract_bin, pagina.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY).tobytes("png"))
            for pagina in doc
        ]

    workers = max(1, min(ocr_workers, total_paginas))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        textos_ocr = list(pool.map(ocr_pagina_png, imagens))

    return normalizar_texto("\n\n".join(textos_ocr)), "ocr", total_paginas


# ---------------------------------------------------------------------------
# Fonte 1: Macapá (AP) — WordPress + Laravel Livewire
# ---------------------------------------------------------------------------

def classificar_macapa(titulo: str, url: str, id_raw: str) -> tuple[str, str]:
    """Extrai número completo da edição e mapeia o tipo (normal, extra, suplemento).

    Em Macapá, o campo `id` do Livewire descarta sufixos como `-A` (ex.: `5234-A` vem como `5234`),
    mas o título preserva (`DIÁRIO OFICIAL Nº: 5234-A-12/02/2026`).
    Edições com sufixo de letra (`-A`, `-B`) correspondem a edições extras no mesmo dia.
    """
    m = re.search(r"N[º°o]?\s*:?\s*(\d+(?:\s*-\s*[A-Za-z])?)", titulo, re.IGNORECASE)
    if m:
        numero = re.sub(r"\s+", "", m.group(1)).upper()
    else:
        numero = str(id_raw).strip()

    referencia = f"{titulo} {url}".lower()
    if "suplemento" in referencia:
        tipo = "suplemento"
    elif "extra" in referencia or re.search(r"-\s*[A-Za-z]$", numero):
        tipo = "extra"
    else:
        tipo = "normal"

    return numero, tipo


def listar_macapa(sessao: requests.Session, inicio: date, fim: date, pausa: float) -> list[dict]:
    """Lista edições de Macapá no intervalo [inicio, fim] via componente Livewire."""
    print(f"[macapa] Buscando listagem ({inicio} a {fim})...")
    resp = requisitar_com_retry(sessao, "GET", MACAPA_URL)
    csrf_match = re.search(r'data-csrf="([^"]+)"', resp.text)
    snap_match = re.search(r'wire:snapshot="([^"]+)"', resp.text)
    if not csrf_match or not snap_match:
        raise RuntimeError("Não foi possível localizar token CSRF ou wire:snapshot em Macapá.")

    csrf = csrf_match.group(1)
    snapshot_str = html.unescape(snap_match.group(1))
    snapshot_obj = json.loads(snapshot_str)
    ano_atual = int(snapshot_obj["data"]["year"])
    mes_atual = int(snapshot_obj["data"]["month"])

    def chamar_livewire(metodo: str, params: list[str]) -> dict:
        nonlocal snapshot_str
        time.sleep(pausa)
        r = requisitar_com_retry(
            sessao,
            "POST",
            MACAPA_LIVEWIRE_URL,
            headers={"X-Livewire": "true", "X-CSRF-TOKEN": csrf},
            json={
                "_token": csrf,
                "components": [
                    {
                        "snapshot": snapshot_str,
                        "updates": {},
                        "calls": [{"path": "", "method": metodo, "params": params}],
                    }
                ],
            },
        )
        dados = r.json()
        snapshot_str = dados["components"][0]["snapshot"]
        return json.loads(snapshot_str)

    edicoes_por_chave: dict[tuple[str, str, str], dict] = {}
    for ano, mes in meses_no_intervalo(inicio, fim, incluir_mes_seguinte=True):
        if ano != ano_atual:
            snapshot_obj = chamar_livewire("changeYear", [str(ano)])
            ano_atual = ano
            mes_atual = int(snapshot_obj["data"]["month"] or 0)
        if mes != mes_atual:
            snapshot_obj = chamar_livewire("setMonth", [f"{mes:02d}"])
            mes_atual = mes

        itens = [item[0] for item in snapshot_obj["data"]["tabDiaries"][0]]
        for item in itens:
            data_pub = datetime.strptime(item["date"].strip(), "%d/%m/%Y").date()
            if not (inicio <= data_pub <= fim):
                continue
            numero, tipo = classificar_macapa(item["title"], item["link"], item["id"])
            chave = (data_pub.isoformat(), numero, tipo)
            edicoes_por_chave[chave] = {
                "fonte": "macapa",
                "data_publicacao": data_pub.isoformat(),
                "numero_edicao": numero,
                "tipo": tipo,
                "url_documento": item["link"].strip(),
            }

    edicoes = sorted(edicoes_por_chave.values(), key=lambda e: (e["data_publicacao"], e["numero_edicao"]))
    print(f"[macapa] {len(edicoes)} edições encontradas no intervalo.")
    return edicoes


# ---------------------------------------------------------------------------
# Fonte 2: Fortaleza (CE) — API REST (/api/search + /api/nodes/{id}/content)
# ---------------------------------------------------------------------------

def classificar_fortaleza(nr_raw: str, nome_arquivo: str, titulo: str) -> tuple[str, str]:
    """Normaliza número e mapeia o tipo de edição em Fortaleza.

    Fortaleza usa sufixo `s` no número (ex.: `18379s`, `18272-02s`) e prefixo `Suplemento`
    no nome do arquivo para suplementos.
    """
    numero = str(nr_raw or "").strip()
    referencia = f"{nome_arquivo} {titulo}".lower()
    if "suplemento" in referencia or re.search(r"s$", numero, re.IGNORECASE):
        tipo = "suplemento"
    elif "extra" in referencia:
        tipo = "extra"
    else:
        tipo = "normal"
    return numero, tipo


def eh_nome_canonico_fortaleza(nome: str) -> bool:
    """Prefere o arquivo principal sem sufixos de reupload como `(1)` ou `-t`."""
    return not bool(re.search(r"(\(\d+\)|-t)\.pdf$", nome, re.IGNORECASE))


def listar_fortaleza(sessao: requests.Session, inicio: date, fim: date) -> list[dict]:
    """Lista edições de Fortaleza no intervalo [inicio, fim], deduplicando reuploads na fonte."""
    print(f"[fortaleza] Buscando listagem ({inicio} a {fim})...")

    # Verifica o intervalo exigido pela própria API para não acionar CAPTCHA
    resp_cfg = requisitar_com_retry(sessao, "GET", f"{FORTALEZA_BASE}/api/captcha-required")
    cfg = resp_cfg.json()
    if cfg.get("required"):
        raise RuntimeError(
            "API de Fortaleza exigiu CAPTCHA em /api/captcha-required. "
            "Não tentamos burlar proteções; aguarde alguns minutos e tente novamente."
        )
    intervalo_busca = max(5.5, float(cfg.get("intervalSeconds", 5.0)) + 0.5)

    candidatos_por_chave: dict[tuple[str, str, str], list[dict]] = {}
    primeira_busca = True

    for ano, mes in meses_no_intervalo(inicio, fim, incluir_mes_seguinte=False):
        pagina = 1
        while True:
            if not primeira_busca:
                time.sleep(intervalo_busca)
            primeira_busca = False

            resp = requisitar_com_retry(
                sessao,
                "POST",
                f"{FORTALEZA_BASE}/api/search",
                json={
                    "ano": str(ano),
                    "mes": f"{mes:02d}",
                    "nrDom": None,
                    "conteudo": None,
                    "captcha": "",
                    "page": pagina,
                    "pageSize": 100,
                },
            )
            corpo = resp.json()
            lista = corpo.get("list", {})
            entradas = lista.get("entries", [])
            paginacao = lista.get("pagination", {})

            for bruto in entradas:
                item = bruto.get("entry", bruto)
                data_str = str(item.get("dataPublicacao") or item.get("data_publicacao") or "")[:10]
                if not data_str:
                    continue
                data_pub = date.fromisoformat(data_str)
                if not (inicio <= data_pub <= fim):
                    continue

                nr = item.get("nrDiarioOficial") or item.get("nr_diario_oficial") or ""
                nome = str(item.get("name") or "")
                titulo = str(item.get("tituloDescricao") or item.get("titulo_descricao") or "")
                node_id = str(item.get("id") or "")
                numero, tipo = classificar_fortaleza(nr, nome, titulo)

                chave = (data_pub.isoformat(), numero, tipo)
                candidatos_por_chave.setdefault(chave, []).append(
                    {
                        "fonte": "fortaleza",
                        "data_publicacao": data_pub.isoformat(),
                        "numero_edicao": numero,
                        "tipo": tipo,
                        "url_documento": f"{FORTALEZA_BASE}/api/nodes/{node_id}/content?name={quote(nome)}",
                        "_nome": nome,
                        "_size": item.get("sizeInBytes"),
                        "_modified": item.get("modifiedAt") or "",
                    }
                )

            if not paginacao.get("hasMoreItems"):
                break
            pagina += 1

    # Deduplica entradas repetidas na própria fonte (ex.: 18434-t.pdf e 18438 (1).pdf)
    edicoes = []
    duplicatas_ignoradas = 0
    for chave, grupo in sorted(candidatos_por_chave.items()):
        if len(grupo) > 1:
            tamanhos = {g["_size"] for g in grupo}
            nomes = [g["_nome"] for g in grupo]
            print(
                f"  [fortaleza] Duplicata na fonte para {chave}: {nomes} (tamanhos={tamanhos})"
            )
            # Prefere o nome canônico (sem `(1)` ou `-t`) e, em empate, o mais recente
            grupo.sort(
                key=lambda g: (eh_nome_canonico_fortaleza(g["_nome"]), g["_modified"]),
                reverse=True,
            )
            duplicatas_ignoradas += len(grupo) - 1

        escolhida = grupo[0]
        edicoes.append(
            {
                "fonte": escolhida["fonte"],
                "data_publicacao": escolhida["data_publicacao"],
                "numero_edicao": escolhida["numero_edicao"],
                "tipo": escolhida["tipo"],
                "url_documento": escolhida["url_documento"],
            }
        )

    print(
        f"[fortaleza] {len(edicoes)} edições únicas encontradas "
        f"({duplicatas_ignoradas} duplicatas da fonte ignoradas)."
    )
    return edicoes


# ---------------------------------------------------------------------------
# Download, extração e gravação
# ---------------------------------------------------------------------------

def coletar_edicoes(
    sessao: requests.Session,
    edicoes: list[dict],
    pausa_pdf: float,
    ocr_workers: int,
) -> list[dict]:
    """Baixa cada PDF sequencialmente, extrai o texto completo e monta o registro final."""
    resultados = []
    total = len(edicoes)
    for i, edicao in enumerate(edicoes, start=1):
        if i > 1:
            time.sleep(pausa_pdf)

        t0 = time.time()
        resp = requisitar_com_retry(sessao, "GET", edicao["url_documento"], timeout=90)
        pdf_bytes = resp.content
        if not pdf_bytes.startswith(b"%PDF"):
            raise RuntimeError(
                f"Resposta inesperada (não é PDF) em {edicao['url_documento']} "
                f"(Content-Type={resp.headers.get('Content-Type')})"
            )

        sha256 = hashlib.sha256(pdf_bytes).hexdigest()
        texto, metodo, paginas = extrair_texto_pdf(pdf_bytes, ocr_workers=ocr_workers)
        coletado_em = datetime.now(FUSO_BRASILIA).isoformat(timespec="seconds")

        registro = {
            "fonte": edicao["fonte"],
            "data_publicacao": edicao["data_publicacao"],
            "numero_edicao": edicao["numero_edicao"],
            "tipo": edicao["tipo"],
            "url_documento": edicao["url_documento"],
            "texto": texto,
            "coletado_em": coletado_em,
            "metodo_extracao": metodo,
            "paginas": paginas,
            "sha256_pdf": sha256,
        }
        resultados.append(registro)

        dt = time.time() - t0
        print(
            f"  [{i}/{total}] {edicao['fonte']} nº {edicao['numero_edicao']} "
            f"({edicao['data_publicacao']}, {edicao['tipo']}) — "
            f"{paginas} págs, {len(texto)} chars ({metodo}) em {dt:.1f}s"
        )
    return resultados


def salvar_jsonl(registros: list[dict], caminho: Path) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_suffix(caminho.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for reg in registros:
            f.write(json.dumps(reg, ensure_ascii=False) + "\n")
    tmp.replace(caminho)


def parse_args() -> argparse.Namespace:
    hoje = datetime.now(FUSO_BRASILIA).date()
    padrao_inicio = (hoje - timedelta(days=60)).isoformat()
    padrao_fim = hoje.isoformat()

    parser = argparse.ArgumentParser(
        description="Coleta Diários Oficiais de Macapá (AP) e Fortaleza (CE) em um intervalo de datas."
    )
    parser.add_argument("--inicio", default=padrao_inicio, help=f"Data inicial YYYY-MM-DD (padrão: {padrao_inicio})")
    parser.add_argument("--fim", default=padrao_fim, help=f"Data final YYYY-MM-DD (padrão: {padrao_fim})")
    parser.add_argument("--fonte", choices=["todas", "macapa", "fortaleza"], default="todas")
    parser.add_argument("--saida", default="dados/diarios.jsonl", help="Arquivo JSONL de saída")
    parser.add_argument("--pausa", type=float, default=1.2, help="Intervalo em segundos entre requisições")
    parser.add_argument("--ocr-workers", type=int, default=8, help="Threads locais para OCR de páginas escaneadas")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    inicio = date.fromisoformat(args.inicio)
    fim = date.fromisoformat(args.fim)
    if inicio > fim:
        raise SystemExit("Erro: --inicio deve ser menor ou igual a --fim.")

    sessao = criar_sessao()
    todos: list[dict] = []

    if args.fonte in ("todas", "fortaleza"):
        lista_fort = listar_fortaleza(sessao, inicio, fim)
        todos.extend(coletar_edicoes(sessao, lista_fort, pausa_pdf=args.pausa, ocr_workers=args.ocr_workers))

    if args.fonte in ("todas", "macapa"):
        lista_mcp = listar_macapa(sessao, inicio, fim, pausa=args.pausa)
        todos.extend(coletar_edicoes(sessao, lista_mcp, pausa_pdf=args.pausa, ocr_workers=args.ocr_workers))

    saida = Path(args.saida)
    salvar_jsonl(todos, saida)
    tamanho_mb = saida.stat().st_size / (1024 * 1024)
    print(f"\nConcluído: {len(todos)} edições salvas em {saida} ({tamanho_mb:.2f} MB).")


if __name__ == "__main__":
    main()
