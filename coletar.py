"""Coletor dos Diários Oficiais de Macapá (AP) e Fortaleza (CE).

Grava os resultados em um banco SQLite com índice FTS5 (`dados/diarios.db`),
decidindo nativo vs OCR página por página e suportando:
- Google Cloud Document AI (em lotes de 15 páginas, com ordenação de 2 colunas via boundingPoly)
- Resgate automático com Gemini 3.8 Flash nas páginas escaneadas com confiança < 0.85
- Fallback local para Tesseract OCR quando não há credenciais GCP configuradas

Uso:
    python coletar.py --inicio 2026-09-26 --fim 2026-11-25 --saida dados/diarios.db
"""

import argparse
import base64
import concurrent.futures
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
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

DOCAI_LOTE_PAGINAS = 5
DOCAI_WORKERS = 4
LIMIAR_CONFIANCA_RESGATE = 0.85
MIN_ALFANUM_PAGINA_NATIVA = 50

SIGLAS_HIFEN_PRESERVAR = {
    "AP", "CE", "BR", "PMM", "PMF", "SEMED", "SEMSA", "SEMOB", "SEMG",
    "SEMFI", "SEGOV", "SEMAS", "SEMZUR", "SEMPLA", "SEMHOU", "SEMCOM",
    "SEMFA", "SEMAG", "SEMAI", "SEMEL", "SEMMU", "SEMTC", "SEMVS",
    "PROGEM", "CORGEM", "GCMM", "EMDESUR", "CTMAC", "CIPEMAC", "FUMCULT",
    "DOM", "DOC", "PJE", "CPF", "CNPJ", "ME", "EPP", "LTDA", "SA",
}

PROMPT_RESGATE_GEMINI = (
    "Transcreva fielmente TODO o texto desta página de Diário Oficial brasileiro em português. "
    "Leia coluna por coluna (primeiro o cabeçalho, depois toda a coluna da esquerda de cima para baixo, "
    "depois toda a coluna da direita de cima para baixo). "
    "Preserve exatamente nomes próprios, siglas, números de decretos, portarias, CPFs, CNPJs, acentos e símbolos (§, º, ª). "
    "Ignore rabiscos de assinaturas manuscritas. "
    "Retorne APENAS o texto puro transcrito, sem blocos de código markdown e sem comentários."
)


# ---------------------------------------------------------------------------
# Utilitários HTTP, datas e limpeza determinística de texto
# ---------------------------------------------------------------------------

def criar_sessao() -> requests.Session:
    sessao = requests.Session()
    sessao.headers.update({"User-Agent": USER_AGENT})
    return sessao


def requisitar_com_retry(
    sessao: requests.Session,
    metodo: str,
    url: str,
    tentativas: int = 5,
    **kwargs,
) -> requests.Response:
    """Faz requisição HTTP com backoff exponencial para falhas transitórias (DNS, rede, 429, 5xx)."""
    timeout = kwargs.pop("timeout", 45)
    for tentativa in range(1, tentativas + 1):
        try:
            resp = sessao.request(metodo, url, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as err:
            status = err.response.status_code if err.response is not None else 0
            if (status != 429 and status < 500) or tentativa == tentativas:
                raise
            erro_atual: requests.RequestException = err
        except requests.RequestException as err:
            if tentativa == tentativas:
                raise
            erro_atual = err

        espera = min(30, 2 ** tentativa)
        print(
            f"  [retry {tentativa}/{tentativas}] erro em {url}: {erro_atual} (aguardando {espera}s)",
            file=sys.stderr,
        )
        time.sleep(espera)
    raise RuntimeError(f"Número inválido de tentativas ({tentativas}) para {url}")


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


def _juntar_hifen_quebra(match: re.Match) -> str:
    esquerda, direita = match.group(1), match.group(2)
    # Preserva compostos com hífen real: Macapá-AP, SEMED-PMM, Político-Pedagógico, Afro-Brasileira
    if direita.upper() in SIGLAS_HIFEN_PRESERVAR:
        return f"{esquerda}-{direita}"
    if direita[0].isupper() and not direita.isupper():
        return f"{esquerda}-{direita}"
    # Junta palavras partidas no fim da linha (ex.: CARVA-\nLHO -> CARVALHO, adminis-\ntração -> administração)
    if direita.islower() or (esquerda.isupper() and direita.isupper()):
        return f"{esquerda}{direita}"
    return f"{esquerda}-{direita}"


def limpar_texto_pagina(texto: str) -> str:
    """Limpeza determinística para busca exata: des-hifeniza quebras de linha e padroniza símbolos."""
    texto = texto.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    # Des-hifenização de palavras partidas no fim da linha (ex.: CARVA-\nLHO -> CARVALHO)
    texto = re.sub(r"([A-Za-zÀ-ÿ]{2,})-\s*\n\s*([A-Za-zÀ-ÿ]{2,})", _juntar_hifen_quebra, texto)
    # Normaliza símbolo de grau (°) usado no lugar do ordinal (º) em números de decreto/artigo
    texto = re.sub(r"\b([Nn])°", r"\1º", texto)
    texto = re.sub(r"\b(Art\.\s*\d+)°", r"\1º", texto)
    texto = re.sub(r"[ \t]+\n", "\n", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


def contar_alfanum(texto: str) -> int:
    return sum(ch.isalnum() for ch in texto)


# ---------------------------------------------------------------------------
# Credenciais GCP (Document AI + resgate Vertex AI Gemini 3.8 Flash)
# ---------------------------------------------------------------------------

def carregar_credenciais_gcp() -> tuple[object | None, str | None, str | None]:
    """Carrega Service Account GCP se GOOGLE_APPLICATION_CREDENTIALS e DOCAI_PROCESSOR estiverem definidos."""
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    processor = os.environ.get("DOCAI_PROCESSOR", "").strip()
    if not cred_path or not os.path.exists(cred_path) or not processor:
        return None, None, None

    try:
        from google.oauth2 import service_account
    except ImportError as err:
        raise RuntimeError(
            "google-auth não instalado. Rode `pip install -r requirements.txt` ou use --ocr tesseract."
        ) from err

    with open(cred_path, "r", encoding="utf-8") as f:
        project_id = json.load(f).get("project_id")

    creds = service_account.Credentials.from_service_account_file(
        cred_path,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    return creds, project_id, processor


def obter_token_gcp(creds) -> str:
    from google.auth.transport.requests import Request

    if not creds.valid or creds.expired:
        creds.refresh(Request())
    return creds.token


# ---------------------------------------------------------------------------
# OCR 1: Google Cloud Document AI (com reconstrução de 2 colunas via boundingPoly)
# ---------------------------------------------------------------------------

def _texto_ancora_docai(layout: dict, texto_completo: str) -> str:
    trechos = []
    for seg in layout.get("textAnchor", {}).get("textSegments", []):
        ini = int(seg.get("startIndex", 0))
        fim = int(seg.get("endIndex", 0))
        trechos.append(texto_completo[ini:fim])
    return "".join(trechos).replace("\n", " ").strip()


def _bbox_docai(layout: dict) -> tuple[float, float, float, float]:
    verts = layout.get("boundingPoly", {}).get("normalizedVertices", [])
    xs = [float(v.get("x", 0.0)) for v in verts] or [0.0]
    ys = [float(v.get("y", 0.0)) for v in verts] or [0.0]
    return min(xs), max(xs), min(ys), max(ys)


def _ordenar_linhas_leitura(itens: list[dict]) -> list[str]:
    """Ordena linhas de cima para baixo, juntando na mesma linha apenas fragmentos na mesma altura sem sobreposição X."""
    ordenados = sorted(itens, key=lambda it: (it["yc"], it["x0"]))
    grupos: list[list[dict]] = []
    for it in ordenados:
        if grupos:
            ult = grupos[-1]
            mesma_altura = abs(it["yc"] - ult[0]["yc"]) < 0.008
            sem_sobreposicao_x = all(it["x0"] >= prev["x1"] - 0.01 or prev["x0"] >= it["x1"] - 0.01 for prev in ult)
            if mesma_altura and sem_sobreposicao_x:
                ult.append(it)
                continue
        grupos.append([it])

    linhas = []
    for grp in grupos:
        grp.sort(key=lambda it: it["x0"])
        linhas.append(" ".join(x["text"] for x in grp))
    return linhas


def reconstruir_pagina_docai(pagina_json: dict, texto_completo: str) -> tuple[str, float]:
    """Reconstrói texto respeitando ordem de 2 colunas usando boundingPoly do Document AI."""
    itens = []
    confs = []
    for ln in pagina_json.get("lines", []):
        lay = ln.get("layout", {})
        txt = _texto_ancora_docai(lay, texto_completo)
        if not txt:
            continue
        x0, x1, y0, y1 = _bbox_docai(lay)
        conf = lay.get("confidence")
        if isinstance(conf, (int, float)):
            confs.append(conf)
        itens.append(
            {"text": txt, "x0": x0, "x1": x1, "y0": y0, "y1": y1, "xc": (x0 + x1) / 2.0, "yc": (y0 + y1) / 2.0}
        )

    # Confiança média a partir dos tokens (mais granular); fallback para linhas
    tok_confs = [
        tok.get("layout", {}).get("confidence")
        for tok in pagina_json.get("tokens", [])
        if isinstance(tok.get("layout", {}).get("confidence"), (int, float))
    ]
    conf_media = (
        sum(tok_confs) / len(tok_confs)
        if tok_confs
        else (sum(confs) / len(confs) if confs else 0.0)
    )
    if not itens:
        return "", conf_media

    # Cabeçalho superior fino (ex.: "Macapá, 25.11.2026 DIÁRIO DO MUNICÍPIO pág. 05")
    topo = [it for it in itens if it["yc"] < 0.055]
    corpo = [it for it in itens if it["yc"] >= 0.055]

    esq = [it for it in corpo if it["x1"] <= 0.53]
    dir_ = [it for it in corpo if it["x0"] >= 0.47]
    largas = [it for it in corpo if it["x0"] < 0.44 and it["x1"] > 0.56]

    eh_duas_colunas = (
        len(esq) >= 5
        and len(dir_) >= 5
        and len(largas) <= max(6, int(len(corpo) * 0.25))
    )
    if not eh_duas_colunas:
        return limpar_texto_pagina("\n".join(_ordenar_linhas_leitura(itens))), conf_media

    faixas: list[tuple[str, list[dict], list[dict]]] = []
    col_esq: list[dict] = []
    col_dir: list[dict] = []
    for it in sorted(corpo, key=lambda x: (x["yc"], x["x0"])):
        if it["x0"] < 0.44 and it["x1"] > 0.56:
            if col_esq or col_dir:
                faixas.append(("cols", col_esq, col_dir))
                col_esq, col_dir = [], []
            faixas.append(("span", [it], []))
        elif it["xc"] < 0.50:
            col_esq.append(it)
        else:
            col_dir.append(it)
    if col_esq or col_dir:
        faixas.append(("cols", col_esq, col_dir))

    linhas_finais: list[str] = []
    if topo:
        linhas_finais.extend(_ordenar_linhas_leitura(topo))
    for tipo_faixa, a, b in faixas:
        linhas_finais.extend(_ordenar_linhas_leitura(a))
        if tipo_faixa == "cols":
            linhas_finais.extend(_ordenar_linhas_leitura(b))

    return limpar_texto_pagina("\n".join(linhas_finais)), conf_media


def _preparar_lotes_pdf(doc: pymupdf.Document, indices_paginas: list[int], max_pags: int) -> list[tuple[list[int], bytes]]:
    """Agrupa índices em sub-PDFs de até max_pags respeitando o limite de 13 MB por chamada síncrona."""
    lotes: list[tuple[list[int], bytes]] = []
    fila = [indices_paginas[i : i + max_pags] for i in range(0, len(indices_paginas), max_pags)]
    while fila:
        lote = fila.pop(0)
        sub = pymupdf.open()
        for idx in lote:
            sub.insert_pdf(doc, from_page=idx, to_page=idx)
        pdf_lote = sub.tobytes()
        sub.close()
        if len(pdf_lote) > 13 * 1024 * 1024 and len(lote) > 1:
            meio = len(lote) // 2
            fila.insert(0, lote[meio:])
            fila.insert(0, lote[:meio])
        else:
            lotes.append((lote, pdf_lote))
    return lotes


def ocr_lote_docai(
    sessao: requests.Session,
    doc: pymupdf.Document,
    indices_paginas: list[int],
    creds,
    processor_name: str,
) -> dict[int, tuple[str, float]]:
    """Envia lotes de páginas escaneadas para o Document AI (até 4 lotes em paralelo na API do Google)."""
    m_loc = re.search(r"/locations/([^/]+)/", processor_name)
    loc = m_loc.group(1) if m_loc else "us"
    url = f"https://{loc}-documentai.googleapis.com/v1/{processor_name}:process"

    lotes = _preparar_lotes_pdf(doc, indices_paginas, DOCAI_LOTE_PAGINAS)
    # Garante token válido antes de abrir as threads
    token = obter_token_gcp(creds)

    def processar_um(item: tuple[list[int], bytes]) -> dict[int, tuple[str, float]]:
        lote, pdf_lote = item
        sess_local = criar_sessao()
        resp = requisitar_com_retry(
            sess_local,
            "POST",
            url,
            tentativas=5,
            timeout=150,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "rawDocument": {
                    "content": base64.b64encode(pdf_lote).decode("ascii"),
                    "mimeType": "application/pdf",
                }
            },
        )
        documento = resp.json().get("document", {})
        texto_full = documento.get("text", "")
        paginas_json = documento.get("pages", [])
        if len(paginas_json) != len(lote):
            raise RuntimeError(
                f"Document AI retornou {len(paginas_json)} páginas para lote de {len(lote)}."
            )
        return {idx_pdf: reconstruir_pagina_docai(pg_json, texto_full) for idx_pdf, pg_json in zip(lote, paginas_json)}

    resultados: dict[int, tuple[str, float]] = {}
    workers = max(1, min(DOCAI_WORKERS, len(lotes)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for parcial in pool.map(processar_um, lotes):
            resultados.update(parcial)
    return resultados


def resgate_pagina_gemini(
    sessao: requests.Session,
    pagina: pymupdf.Page,
    creds,
    project_id: str,
) -> str:
    """Reprocessa uma única página de baixa confiança usando Gemini 3.8 Flash (LOW thinking)."""
    png = pagina.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY).tobytes("png")
    token = obter_token_gcp(creds)
    url = (
        f"https://aiplatform.googleapis.com/v1/projects/{project_id}"
        "/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
    )
    resp = requisitar_com_retry(
        sessao,
        "POST",
        url,
        tentativas=3,
        timeout=120,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": PROMPT_RESGATE_GEMINI},
                        {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(png).decode("ascii")}},
                    ],
                }
            ],
            "generationConfig": {"temperature": 0.0, "thinkingConfig": {"thinkingLevel": "LOW"}},
        },
    )
    parts = resp.json()["candidates"][0]["content"].get("parts", [])
    txt = "\n".join(p.get("text", "") for p in parts if not p.get("thought") and "text" in p)
    return limpar_texto_pagina(txt)


# ---------------------------------------------------------------------------
# OCR 2 (Fallback local): Tesseract com confiança média via TSV
# ---------------------------------------------------------------------------

def encontrar_tesseract() -> str:
    caminho = shutil.which("tesseract")
    if caminho:
        return caminho
    padrao_win = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(padrao_win):
        return padrao_win
    raise RuntimeError(
        "Tesseract OCR não encontrado e credenciais GCP não configuradas. "
        "Configure GOOGLE_APPLICATION_CREDENTIALS + DOCAI_PROCESSOR ou instale o Tesseract ('por')."
    )


def _ocr_pagina_tesseract(args: tuple[str, bytes]) -> tuple[str, float]:
    tesseract_bin, png_bytes = args
    proc_txt = subprocess.run(
        [tesseract_bin, "stdin", "stdout", "-l", "por", "--oem", "1", "--psm", "3"],
        input=png_bytes,
        capture_output=True,
        check=True,
    )
    texto = limpar_texto_pagina(proc_txt.stdout.decode("utf-8", errors="replace"))

    proc_tsv = subprocess.run(
        [tesseract_bin, "stdin", "stdout", "-l", "por", "--oem", "1", "--psm", "3", "tsv"],
        input=png_bytes,
        capture_output=True,
        check=True,
    )
    confs = []
    for linha in proc_tsv.stdout.decode("utf-8", errors="replace").splitlines()[1:]:
        cols = linha.split("\t")
        if len(cols) >= 12 and cols[11].strip():
            try:
                c = float(cols[10])
                if c >= 0:
                    confs.append(c / 100.0)
            except ValueError:
                pass
    conf_media = sum(confs) / len(confs) if confs else 0.0
    return texto, conf_media


def extrair_paginas_pdf(
    sessao: requests.Session,
    pdf_bytes: bytes,
    modo_ocr: str,
    ocr_workers: int,
    resgate_gemini: bool,
    creds_gcp,
    project_id: str | None,
    processor_name: str | None,
) -> tuple[list[dict], str, float | None]:
    """Extrai texto página por página: usa camada nativa onde existe e OCR só nas páginas escaneadas."""
    paginas_saida: list[dict] = []
    indices_ocr: list[int] = []

    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        total_paginas = len(doc)
        for idx in range(total_paginas):
            texto_nativo = limpar_texto_pagina(doc[idx].get_text("text"))
            if contar_alfanum(texto_nativo) >= MIN_ALFANUM_PAGINA_NATIVA:
                paginas_saida.append(
                    {
                        "numero_pagina": idx + 1,
                        "metodo_extracao": "texto_nativo",
                        "confianca_ocr": None,
                        "texto": texto_nativo,
                    }
                )
            else:
                paginas_saida.append(
                    {
                        "numero_pagina": idx + 1,
                        "metodo_extracao": "pendente_ocr",
                        "confianca_ocr": None,
                        "texto": texto_nativo,
                    }
                )
                indices_ocr.append(idx)

        if indices_ocr:
            usar_docai = (
                modo_ocr == "document_ai"
                or (modo_ocr == "auto" and creds_gcp is not None and processor_name is not None)
            )
            if usar_docai:
                if creds_gcp is None or not processor_name:
                    raise RuntimeError(
                        "Modo document_ai exige GOOGLE_APPLICATION_CREDENTIALS e DOCAI_PROCESSOR configurados."
                    )
                lote_res = ocr_lote_docai(sessao, doc, indices_ocr, creds_gcp, processor_name)
                for idx in indices_ocr:
                    txt_ocr, conf = lote_res[idx]
                    metodo_pag = "document_ai"
                    # Resgate automático com Gemini 3.8 Flash se a confiança ficar baixa ou texto muito curto
                    if (
                        resgate_gemini
                        and project_id
                        and (conf < LIMIAR_CONFIANCA_RESGATE or contar_alfanum(txt_ocr) < 80)
                    ):
                        try:
                            txt_gem = resgate_pagina_gemini(sessao, doc[idx], creds_gcp, project_id)
                            if contar_alfanum(txt_gem) >= contar_alfanum(txt_ocr) * 0.85 and contar_alfanum(txt_gem) >= 40:
                                print(
                                    f"    [resgate gemini] pág {idx + 1}: conf DocAI={conf:.2f} "
                                    f"({len(txt_ocr)} -> {len(txt_gem)} chars)"
                                )
                                txt_ocr = txt_gem
                                metodo_pag = "gemini_flash"
                                conf = None
                        except Exception as err:
                            print(f"    [aviso resgate gemini] pág {idx + 1}: {err}", file=sys.stderr)

                    paginas_saida[idx]["texto"] = txt_ocr
                    paginas_saida[idx]["metodo_extracao"] = metodo_pag
                    paginas_saida[idx]["confianca_ocr"] = round(conf, 4) if conf is not None else None
            else:
                tesseract_bin = encontrar_tesseract()
                tarefas = [
                    (tesseract_bin, doc[idx].get_pixmap(dpi=150, colorspace=pymupdf.csGRAY).tobytes("png"))
                    for idx in indices_ocr
                ]
                workers = max(1, min(ocr_workers, len(tarefas)))
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                    res_tess = list(pool.map(_ocr_pagina_tesseract, tarefas))
                for idx, (txt_ocr, conf) in zip(indices_ocr, res_tess):
                    paginas_saida[idx]["texto"] = txt_ocr
                    paginas_saida[idx]["metodo_extracao"] = "tesseract"
                    paginas_saida[idx]["confianca_ocr"] = round(conf, 4)

    metodos = sorted({p["metodo_extracao"] for p in paginas_saida})
    if len(metodos) == 1:
        metodo_edicao = metodos[0]
    elif metodos == ["document_ai", "gemini_flash"]:
        metodo_edicao = "document_ai+gemini"
    else:
        metodo_edicao = "+".join(metodos)

    confs_validas = [p["confianca_ocr"] for p in paginas_saida if p["confianca_ocr"] is not None]
    confianca_media = round(sum(confs_validas) / len(confs_validas), 4) if confs_validas else None
    return paginas_saida, metodo_edicao, confianca_media


# ---------------------------------------------------------------------------
# Fonte 1: Macapá (AP) — WordPress + Laravel Livewire
# ---------------------------------------------------------------------------

def classificar_macapa(titulo: str, url: str, id_raw: str) -> tuple[str, str]:
    """Extrai número completo da edição e mapeia o tipo (normal, extra, suplemento)."""
    m = re.search(r"N[º°o]?\s*:?\s*(\d+(?:\s*-\s*[A-Za-z])?)", titulo, re.IGNORECASE)
    if m:
        numero = re.sub(r"\s+", "", m.group(1)).upper()
    else:
        numero = str(id_raw).strip()

    referencia = f"{titulo} {url}".lower()
    if re.search(r"\bsuplemento\b", referencia):
        tipo = "suplemento"
    elif re.search(r"\bextra\b", referencia) or re.search(r"-\s*[A-Za-z]$", numero):
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
            url_doc = item["link"].strip()
            if chave in edicoes_por_chave and edicoes_por_chave[chave]["url_documento"] != url_doc:
                print(
                    f"  [macapa] Aviso: URLs diferentes para {chave}: "
                    f"{edicoes_por_chave[chave]['url_documento']} vs {url_doc}",
                    file=sys.stderr,
                )
            edicoes_por_chave[chave] = {
                "fonte": "macapa",
                "data_publicacao": data_pub.isoformat(),
                "numero_edicao": numero,
                "tipo": tipo,
                "url_documento": url_doc,
            }

    edicoes = sorted(edicoes_por_chave.values(), key=lambda e: (e["data_publicacao"], e["numero_edicao"]))
    print(f"[macapa] {len(edicoes)} edições encontradas no intervalo.")
    return edicoes


# ---------------------------------------------------------------------------
# Fonte 2: Fortaleza (CE) — API REST (/api/search + /api/nodes/{id}/content)
# ---------------------------------------------------------------------------

def classificar_fortaleza(nr_raw: str, nome_arquivo: str, titulo: str) -> tuple[str, str]:
    """Normaliza número e mapeia o tipo de edição em Fortaleza."""
    numero = str(nr_raw or "").strip()
    referencia = f"{nome_arquivo} {titulo}".lower()
    if re.search(r"\bsuplemento\b", referencia) or re.search(r"s$", numero, re.IGNORECASE):
        tipo = "suplemento"
    elif re.search(r"\bextra\b", referencia):
        tipo = "extra"
    else:
        tipo = "normal"
    return numero, tipo


def eh_nome_canonico_fortaleza(nome: str) -> bool:
    """Prefere o arquivo principal sem sufixos de reupload como `(1)` ou `-t`."""
    return not bool(re.search(r"(\(\d+\)|-t)\.pdf$", nome, re.IGNORECASE))


def listar_fortaleza(sessao: requests.Session, inicio: date, fim: date) -> list[dict]:
    """Lista edições de Fortaleza no intervalo [inicio, fim], deduplicando reuploads apenas com segurança."""
    print(f"[fortaleza] Buscando listagem ({inicio} a {fim})...")

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
            lista = resp.json().get("list", {})
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

    edicoes = []
    duplicatas_ignoradas = 0
    for chave, grupo in sorted(candidatos_por_chave.items()):
        if len(grupo) > 1:
            tamanhos = {g["_size"] for g in grupo if g["_size"] is not None}
            nomes = [g["_nome"] for g in grupo]
            if len(tamanhos) == 1:
                print(
                    f"  [fortaleza] Reupload idêntico na fonte para {chave}: {nomes} (tamanho={next(iter(tamanhos))} B)"
                )
                grupo.sort(
                    key=lambda g: (eh_nome_canonico_fortaleza(g["_nome"]), g["_modified"]),
                    reverse=True,
                )
                duplicatas_ignoradas += len(grupo) - 1
                escolhidas = [grupo[0]]
            else:
                # Nunca descarta silenciosamente quando os tamanhos diferem!
                print(
                    f"  [fortaleza] ALERTA: múltiplos arquivos com tamanhos diferentes para {chave}: "
                    f"{list(zip(nomes, [g['_size'] for g in grupo]))} — mantendo o mais recente após log.",
                    file=sys.stderr,
                )
                grupo.sort(key=lambda g: g["_modified"], reverse=True)
                escolhidas = [grupo[0]]
        else:
            escolhidas = [grupo[0]]

        for escolhida in escolhidas:
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
        f"({duplicatas_ignoradas} reuploads idênticos ignorados)."
    )
    return edicoes


# ---------------------------------------------------------------------------
# Banco SQLite + FTS5 (armazenamento sem duplicação de texto)
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS edicoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fonte TEXT NOT NULL,
    data_publicacao TEXT NOT NULL,
    numero_edicao TEXT NOT NULL,
    tipo TEXT NOT NULL,
    url_documento TEXT NOT NULL,
    coletado_em TEXT NOT NULL,
    metodo_extracao TEXT NOT NULL,
    paginas INTEGER NOT NULL,
    confianca_media REAL,
    sha256_pdf TEXT NOT NULL,
    UNIQUE (fonte, data_publicacao, numero_edicao, tipo)
);

CREATE TABLE IF NOT EXISTS paginas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edicao_id INTEGER NOT NULL REFERENCES edicoes(id) ON DELETE CASCADE,
    numero_pagina INTEGER NOT NULL,
    metodo_extracao TEXT NOT NULL,
    confianca_ocr REAL,
    texto TEXT NOT NULL,
    UNIQUE (edicao_id, numero_pagina)
);

CREATE VIRTUAL TABLE IF NOT EXISTS paginas_fts USING fts5(
    texto,
    content='paginas',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS paginas_ai AFTER INSERT ON paginas BEGIN
    INSERT INTO paginas_fts(rowid, texto) VALUES (new.id, new.texto);
END;

CREATE TRIGGER IF NOT EXISTS paginas_ad AFTER DELETE ON paginas BEGIN
    INSERT INTO paginas_fts(paginas_fts, rowid, texto) VALUES('delete', old.id, old.texto);
END;

CREATE TRIGGER IF NOT EXISTS paginas_au AFTER UPDATE ON paginas BEGIN
    INSERT INTO paginas_fts(paginas_fts, rowid, texto) VALUES('delete', old.id, old.texto);
    INSERT INTO paginas_fts(rowid, texto) VALUES (new.id, new.texto);
END;

CREATE VIEW IF NOT EXISTS edicoes_completas AS
SELECT
    e.id,
    e.fonte,
    e.data_publicacao,
    e.numero_edicao,
    e.tipo,
    e.url_documento,
    (
        SELECT GROUP_CONCAT(texto, char(10) || char(10))
        FROM (
            SELECT p.texto
            FROM paginas p
            WHERE p.edicao_id = e.id
            ORDER BY p.numero_pagina
        )
    ) AS texto,
    e.coletado_em,
    e.metodo_extracao,
    e.paginas,
    e.confianca_media,
    e.sha256_pdf
FROM edicoes e;
"""


def inicializar_banco(caminho: Path, retomar: bool) -> sqlite3.Connection:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    if not retomar and caminho.exists():
        caminho.unlink()
        for suf in ("-wal", "-shm"):
            aux = Path(str(caminho) + suf)
            if aux.exists():
                aux.unlink()
    conn = sqlite3.connect(caminho)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def edicao_ja_coletada(conn: sqlite3.Connection, edicao: dict) -> bool:
    row = conn.execute(
        """
        SELECT e.paginas, COUNT(p.id)
        FROM edicoes e
        LEFT JOIN paginas p ON p.edicao_id = e.id
        WHERE e.fonte = ? AND e.data_publicacao = ? AND e.numero_edicao = ? AND e.tipo = ?
        GROUP BY e.id
        """,
        (edicao["fonte"], edicao["data_publicacao"], edicao["numero_edicao"], edicao["tipo"]),
    ).fetchone()
    return bool(row and row[0] > 0 and row[0] == row[1])


def gravar_edicao(conn: sqlite3.Connection, registro: dict, paginas: list[dict]) -> None:
    with conn:
        # Remove versão anterior caso exista (para reprocessamento limpo)
        conn.execute(
            "DELETE FROM edicoes WHERE fonte = ? AND data_publicacao = ? AND numero_edicao = ? AND tipo = ?",
            (registro["fonte"], registro["data_publicacao"], registro["numero_edicao"], registro["tipo"]),
        )
        cur = conn.execute(
            """
            INSERT INTO edicoes (
                fonte, data_publicacao, numero_edicao, tipo, url_documento,
                coletado_em, metodo_extracao, paginas, confianca_media, sha256_pdf
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                registro["fonte"],
                registro["data_publicacao"],
                registro["numero_edicao"],
                registro["tipo"],
                registro["url_documento"],
                registro["coletado_em"],
                registro["metodo_extracao"],
                registro["paginas"],
                registro["confianca_media"],
                registro["sha256_pdf"],
            ),
        )
        edicao_id = cur.lastrowid
        conn.executemany(
            """
            INSERT INTO paginas (edicao_id, numero_pagina, metodo_extracao, confianca_ocr, texto)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (edicao_id, p["numero_pagina"], p["metodo_extracao"], p["confianca_ocr"], p["texto"])
                for p in paginas
            ],
        )


def finalizar_banco(conn: sqlite3.Connection) -> None:
    """Compacta o banco e desativa WAL para gerar um único arquivo .db portátil."""
    try:
        conn.commit()
        conn.execute("INSERT INTO paginas_fts(paginas_fts) VALUES('optimize')")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pipeline principal de download, extração e gravação
# ---------------------------------------------------------------------------

def coletar_edicoes(
    sessao: requests.Session,
    conn: sqlite3.Connection,
    edicoes: list[dict],
    pausa_pdf: float,
    modo_ocr: str,
    ocr_workers: int,
    resgate_gemini: bool,
    retomar: bool,
    creds_gcp,
    project_id: str | None,
    processor_name: str | None,
) -> int:
    coletadas = 0
    total = len(edicoes)
    baixou_anterior = False

    for i, edicao in enumerate(edicoes, start=1):
        if retomar and edicao_ja_coletada(conn, edicao):
            print(
                f"  [{i}/{total}] {edicao['fonte']} nº {edicao['numero_edicao']} "
                f"({edicao['data_publicacao']}) — já coletada, pulando."
            )
            coletadas += 1
            continue

        if baixou_anterior:
            time.sleep(pausa_pdf)
        baixou_anterior = True

        t0 = time.time()
        resp = requisitar_com_retry(sessao, "GET", edicao["url_documento"], timeout=120)
        pdf_bytes = resp.content
        if not pdf_bytes.startswith(b"%PDF"):
            raise RuntimeError(
                f"Resposta inesperada (não é PDF) em {edicao['url_documento']} "
                f"(Content-Type={resp.headers.get('Content-Type')})"
            )

        sha256 = hashlib.sha256(pdf_bytes).hexdigest()
        paginas, metodo, conf_media = extrair_paginas_pdf(
            sessao=sessao,
            pdf_bytes=pdf_bytes,
            modo_ocr=modo_ocr,
            ocr_workers=ocr_workers,
            resgate_gemini=resgate_gemini,
            creds_gcp=creds_gcp,
            project_id=project_id,
            processor_name=processor_name,
        )
        coletado_em = datetime.now(FUSO_BRASILIA).isoformat(timespec="seconds")

        registro = {
            "fonte": edicao["fonte"],
            "data_publicacao": edicao["data_publicacao"],
            "numero_edicao": edicao["numero_edicao"],
            "tipo": edicao["tipo"],
            "url_documento": edicao["url_documento"],
            "coletado_em": coletado_em,
            "metodo_extracao": metodo,
            "paginas": len(paginas),
            "confianca_media": conf_media,
            "sha256_pdf": sha256,
        }
        gravar_edicao(conn, registro, paginas)
        coletadas += 1

        total_chars = sum(len(p["texto"]) for p in paginas)
        conf_str = f", conf={conf_media:.3f}" if conf_media is not None else ""
        dt = time.time() - t0
        print(
            f"  [{i}/{total}] {edicao['fonte']} nº {edicao['numero_edicao']} "
            f"({edicao['data_publicacao']}, {edicao['tipo']}) — "
            f"{len(paginas)} págs, {total_chars} chars ({metodo}{conf_str}) em {dt:.1f}s"
        )
    return coletadas


def parse_args() -> argparse.Namespace:
    hoje = datetime.now(FUSO_BRASILIA).date()
    padrao_inicio = (hoje - timedelta(days=60)).isoformat()
    padrao_fim = hoje.isoformat()

    parser = argparse.ArgumentParser(
        description="Coleta Diários Oficiais de Macapá (AP) e Fortaleza (CE) para SQLite + FTS5."
    )
    parser.add_argument("--inicio", default=padrao_inicio, help=f"Data inicial YYYY-MM-DD (padrão: {padrao_inicio})")
    parser.add_argument("--fim", default=padrao_fim, help=f"Data final YYYY-MM-DD (padrão: {padrao_fim})")
    parser.add_argument("--fonte", choices=["todas", "macapa", "fortaleza"], default="todas")
    parser.add_argument("--saida", default="dados/diarios.db", help="Arquivo SQLite de saída")
    parser.add_argument("--pausa", type=float, default=1.2, help="Intervalo em segundos entre requisições")
    parser.add_argument(
        "--ocr",
        choices=["auto", "document_ai", "tesseract"],
        default="auto",
        help="Motor de OCR para páginas escaneadas (padrão: auto — Document AI se configurado, senão Tesseract)",
    )
    parser.add_argument(
        "--sem-resgate-gemini",
        action="store_true",
        help="Desativa o resgate automático com Gemini 3.8 Flash em páginas com confiança < 0.85",
    )
    parser.add_argument("--ocr-workers", type=int, default=8, help="Threads locais no fallback Tesseract")
    parser.add_argument(
        "--retomar",
        action="store_true",
        help="Mantém o banco existente e pula edições que já foram coletadas integralmente",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    inicio = date.fromisoformat(args.inicio)
    fim = date.fromisoformat(args.fim)
    if inicio > fim:
        raise SystemExit("Erro: --inicio deve ser menor ou igual a --fim.")

    creds_gcp, project_id, processor_name = carregar_credenciais_gcp()
    motor_efetivo = (
        "document_ai"
        if (args.ocr == "document_ai" or (args.ocr == "auto" and creds_gcp and processor_name))
        else "tesseract"
    )
    resgate = (not args.sem_resgate_gemini) and (motor_efetivo == "document_ai")
    print(f"[config] Motor OCR para páginas escaneadas: {motor_efetivo} (resgate_gemini={resgate})")

    sessao = criar_sessao()
    saida = Path(args.saida)
    conn = inicializar_banco(saida, retomar=args.retomar)

    total_coletadas = 0
    try:
        if args.fonte in ("todas", "fortaleza"):
            lista_fort = listar_fortaleza(sessao, inicio, fim)
            total_coletadas += coletar_edicoes(
                sessao, conn, lista_fort,
                pausa_pdf=args.pausa, modo_ocr=args.ocr, ocr_workers=args.ocr_workers,
                resgate_gemini=resgate, retomar=args.retomar,
                creds_gcp=creds_gcp, project_id=project_id, processor_name=processor_name,
            )

        if args.fonte in ("todas", "macapa"):
            lista_mcp = listar_macapa(sessao, inicio, fim, pausa=args.pausa)
            total_coletadas += coletar_edicoes(
                sessao, conn, lista_mcp,
                pausa_pdf=args.pausa, modo_ocr=args.ocr, ocr_workers=args.ocr_workers,
                resgate_gemini=resgate, retomar=args.retomar,
                creds_gcp=creds_gcp, project_id=project_id, processor_name=processor_name,
            )
    finally:
        finalizar_banco(conn)

    tamanho_mb = saida.stat().st_size / (1024 * 1024)
    print(f"\nConcluído: {total_coletadas} edições gravadas em {saida} ({tamanho_mb:.2f} MB).")


if __name__ == "__main__":
    main()
