"""Script de verificação de completude, unicidade e qualidade dos dados coletados.

Termina com código de saída 1 caso encontre qualquer problema e 0 se tudo estiver íntegro.

Uso:
    python verificar.py dados/diarios.jsonl --inicio 2026-09-26 --fim 2026-11-25
    python verificar.py dados/diarios.jsonl --inicio 2026-09-26 --fim 2026-11-25 --checar-fonte
"""

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import requests

from coletar import USER_AGENT, criar_sessao, listar_fortaleza

TIPOS_VALIDOS = {"normal", "extra", "suplemento"}
FONTES_ESPERADAS = {"macapa", "fortaleza"}
CAMPOS_OBRIGATORIOS = (
    "fonte",
    "data_publicacao",
    "numero_edicao",
    "tipo",
    "url_documento",
    "texto",
    "coletado_em",
)
PALAVRAS_DIARIO = (
    "diário",
    "diario",
    "oficial",
    "município",
    "municipio",
    "prefeitura",
    "decreto",
    "portaria",
    "lei",
    "secretaria",
    "publicação",
    "publicacao",
)
MACAPA_SITEMAP_URL = "https://macapa.ap.gov.br/official_diaries-sitemap3.xml"


def carregar_jsonl(caminho: Path) -> tuple[list[dict], list[str]]:
    erros: list[str] = []
    registros: list[dict] = []
    if not caminho.exists():
        return [], [f"Arquivo não encontrado: {caminho}"]

    with caminho.open("r", encoding="utf-8") as f:
        for num_linha, linha in enumerate(f, start=1):
            linha = linha.strip()
            if not linha:
                continue
            try:
                registros.append(json.loads(linha))
            except json.JSONDecodeError as err:
                erros.append(f"Linha {num_linha}: JSON inválido ({err})")

    if not registros:
        erros.append("Arquivo JSONL está vazio (0 registros).")
    return registros, erros


def verificar_esquema_e_qualidade(
    registros: list[dict],
    inicio: date | None,
    fim: date | None,
) -> tuple[list[str], list[str]]:
    erros: list[str] = []
    avisos: list[str] = []

    for idx, reg in enumerate(registros, start=1):
        etiqueta = f"Registro #{idx} ({reg.get('fonte', '?')} {reg.get('numero_edicao', '?')})"

        for campo in CAMPOS_OBRIGATORIOS:
            val = reg.get(campo)
            if val is None or (isinstance(val, str) and not val.strip()):
                erros.append(f"{etiqueta}: campo obrigatório ausente ou vazio '{campo}'")

        fonte = reg.get("fonte")
        if fonte not in FONTES_ESPERADAS:
            erros.append(f"{etiqueta}: fonte desconhecida '{fonte}'")

        tipo = reg.get("tipo")
        if tipo not in TIPOS_VALIDOS:
            erros.append(f"{etiqueta}: tipo inválido '{tipo}' (esperado: {sorted(TIPOS_VALIDOS)})")

        url = str(reg.get("url_documento") or "")
        if not url.startswith("https://"):
            erros.append(f"{etiqueta}: URL inválida '{url}'")

        # Data de publicação
        try:
            data_pub = date.fromisoformat(str(reg.get("data_publicacao") or ""))
            if inicio and data_pub < inicio:
                erros.append(f"{etiqueta}: data {data_pub} anterior ao início {inicio}")
            if fim and data_pub > fim:
                erros.append(f"{etiqueta}: data {data_pub} posterior ao fim {fim}")
        except ValueError:
            erros.append(f"{etiqueta}: data_publicacao inválida '{reg.get('data_publicacao')}'")

        # Timestamp ISO 8601 com fuso horário
        coletado = str(reg.get("coletado_em") or "")
        try:
            dt = datetime.fromisoformat(coletado)
            if dt.tzinfo is None:
                erros.append(f"{etiqueta}: coletado_em sem fuso horário '{coletado}'")
        except ValueError:
            erros.append(f"{etiqueta}: coletado_em não é ISO 8601 válido '{coletado}'")

        # Qualidade e legibilidade do texto extraído
        texto = str(reg.get("texto") or "")
        texto_limpo = texto.strip()
        if len(texto_limpo) < 500:
            erros.append(f"{etiqueta}: texto muito curto ou vazio ({len(texto_limpo)} chars)")
            continue

        paginas = reg.get("paginas")
        if isinstance(paginas, int) and paginas > 0:
            media_por_pagina = len(texto_limpo) / paginas
            if media_por_pagina < 150:
                erros.append(
                    f"{etiqueta}: média baixa de texto por página "
                    f"({media_por_pagina:.0f} chars/pág em {paginas} págs — possível falha de OCR)"
                )

        # Caracteres corrompidos (mojibake / replacement char)
        qtd_invalidos = texto_limpo.count("\ufffd")
        if qtd_invalidos / len(texto_limpo) > 0.005:
            erros.append(f"{etiqueta}: excesso de caracteres ilegíveis U+FFFD ({qtd_invalidos})")

        # Proporção de caracteres legíveis (letras/dígitos entre não-espaços)
        nao_espaco = re.sub(r"\s+", "", texto_limpo)
        if nao_espaco:
            alfanum = sum(ch.isalnum() for ch in nao_espaco)
            if alfanum / len(nao_espaco) < 0.50:
                erros.append(
                    f"{etiqueta}: proporção muito baixa de caracteres alfanuméricos "
                    f"({alfanum / len(nao_espaco):.1%} — texto possivelmente ilegível)"
                )

        # Vocabulário mínimo esperado de um diário oficial em português
        texto_lower = texto_limpo.lower()
        hits = sum(1 for p in PALAVRAS_DIARIO if p in texto_lower)
        if hits < 3:
            erros.append(f"{etiqueta}: texto não parece um diário oficial em português (apenas {hits} termos-chave)")

        # Aviso se o número da edição não aparece no texto (ignora espaços e pontos de milhar)
        num_digitos = re.sub(r"\D", "", str(reg.get("numero_edicao") or ""))
        if num_digitos:
            digitos_texto = re.sub(r"[\s.]+", "", texto_limpo)
            if num_digitos not in digitos_texto:
                avisos.append(
                    f"{etiqueta}: número '{num_digitos}' não encontrado literalmente no texto "
                    f"(pode ser erro de capa na fonte ou faixa ignorada no OCR)"
                )

    return erros, avisos


def verificar_duplicatas(registros: list[dict]) -> list[str]:
    erros: list[str] = []
    vistos_chave: set[tuple[str, str, str, str]] = set()
    vistos_url: set[str] = set()
    vistos_hash: dict[tuple[str, str], str] = {}

    for reg in registros:
        fonte = str(reg.get("fonte") or "")
        chave = (
            fonte,
            str(reg.get("data_publicacao") or ""),
            str(reg.get("numero_edicao") or ""),
            str(reg.get("tipo") or ""),
        )
        if chave in vistos_chave:
            erros.append(f"Duplicata de edição: {chave}")
        vistos_chave.add(chave)

        url = str(reg.get("url_documento") or "")
        if url in vistos_url:
            erros.append(f"Duplicata de URL: {url}")
        vistos_url.add(url)

        sha = reg.get("sha256_pdf")
        if sha:
            chave_hash = (fonte, str(sha))
            if chave_hash in vistos_hash:
                erros.append(
                    f"PDF idêntico duplicado em {fonte}: edição {reg.get('numero_edicao')} "
                    f"tem mesmo SHA-256 que edição {vistos_hash[chave_hash]}"
                )
            vistos_hash[chave_hash] = str(reg.get("numero_edicao") or "")

    return erros


def verificar_completude_local(registros: list[dict]) -> list[str]:
    erros: list[str] = []
    por_fonte: dict[str, list[dict]] = defaultdict(list)
    for reg in registros:
        por_fonte[str(reg.get("fonte") or "")].append(reg)

    for fonte in sorted(FONTES_ESPERADAS):
        itens = por_fonte.get(fonte, [])
        if not itens:
            erros.append(f"Fonte ausente nos dados coletados: {fonte}")
            continue

        # 1. Continuidade da numeração sequencial das edições normais
        numeros_normais = []
        for reg in itens:
            if reg.get("tipo") == "normal" and str(reg.get("numero_edicao", "")).isdigit():
                numeros_normais.append(int(reg["numero_edicao"]))

        if numeros_normais:
            menor, maior = min(numeros_normais), max(numeros_normais)
            faltando = sorted(set(range(menor, maior + 1)) - set(numeros_normais))
            if faltando:
                erros.append(
                    f"[{fonte}] Furo na numeração sequencial ({menor}..{maior}): "
                    f"edições ausentes = {faltando}"
                )

        # 2. Intervalos de dias corridos entre publicações consecutivas (> 6 dias indica possível lacuna)
        datas = sorted({date.fromisoformat(r["data_publicacao"]) for r in itens if r.get("data_publicacao")})
        for d1, d2 in zip(datas, datas[1:]):
            gap = (d2 - d1).days
            if gap > 6:
                erros.append(f"[{fonte}] Lacuna incomum de {gap} dias sem edição entre {d1} e {d2}")

    return erros


def verificar_contra_fontes(registros: list[dict], inicio: date, fim: date) -> list[str]:
    """Reconciliação ao vivo: cruza o JSONL com o sitemap de Macapá e a API de Fortaleza."""
    erros: list[str] = []
    chaves_locais = {
        (r["fonte"], r["data_publicacao"], r["numero_edicao"], r["tipo"]) for r in registros
    }

    # 1. Macapá — prova independente via Yoast XML Sitemap
    print("[checar-fonte] Consultando sitemap independente de Macapá...")
    resp = requests.get(MACAPA_SITEMAP_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    sitemap_macapa = set()
    for loc in root.findall(".//sm:loc", ns):
        url = (loc.text or "").strip()
        m = re.search(r"/diarios-oficiais/(\d+(?:-[a-z])?)-(\d{2})-(\d{2})-(\d{4})/?$", url, re.IGNORECASE)
        if not m:
            continue
        num_raw, dd, mm, yyyy = m.groups()
        data_pub = date(int(yyyy), int(mm), int(dd))
        if inicio <= data_pub <= fim:
            num = num_raw.upper()
            tipo = "extra" if re.search(r"-[A-Z]$", num) else "normal"
            sitemap_macapa.add(("macapa", data_pub.isoformat(), num, tipo))

    faltando_mcp = sorted(sitemap_macapa - chaves_locais)
    if faltando_mcp:
        erros.append(f"[macapa] Edições no sitemap ausentes no JSONL: {faltando_mcp}")
    else:
        print(f"  [macapa] OK — todas as {len(sitemap_macapa)} edições do sitemap estão no JSONL.")

    # 2. Fortaleza — reconciliação com /api/search
    print("[checar-fonte] Consultando API oficial de Fortaleza...")
    sessao = criar_sessao()
    fort_api = listar_fortaleza(sessao, inicio, fim)
    esperadas_fort = {
        ("fortaleza", e["data_publicacao"], e["numero_edicao"], e["tipo"]) for e in fort_api
    }
    faltando_fort = sorted(esperadas_fort - chaves_locais)
    if faltando_fort:
        erros.append(f"[fortaleza] Edições na API ausentes no JSONL: {faltando_fort}")
    else:
        print(f"  [fortaleza] OK — todas as {len(esperadas_fort)} edições únicas da API estão no JSONL.")

    return erros


def imprimir_resumo(registros: list[dict]) -> None:
    por_fonte: dict[str, list[dict]] = defaultdict(list)
    for reg in registros:
        por_fonte[str(reg.get("fonte") or "?")].append(reg)

    print("\n=== Resumo da Coleta ===")
    for fonte in sorted(por_fonte):
        itens = por_fonte[fonte]
        datas = sorted(r["data_publicacao"] for r in itens if r.get("data_publicacao"))
        tipos: dict[str, int] = defaultdict(int)
        metodos: dict[str, int] = defaultdict(int)
        for r in itens:
            tipos[str(r.get("tipo"))] += 1
            metodos[str(r.get("metodo_extracao", "n/d"))] += 1
        nums = [int(r["numero_edicao"]) for r in itens if str(r.get("numero_edicao", "")).isdigit()]
        faixa_num = f"{min(nums)}..{max(nums)}" if nums else "n/d"
        print(
            f"- {fonte}: {len(itens)} edições | período {datas[0]} a {datas[-1]} | "
            f"numeração {faixa_num} | tipos={dict(tipos)} | extração={dict(metodos)}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verifica completude, duplicatas e legibilidade dos diários coletados.")
    parser.add_argument("arquivo", nargs="?", default="dados/diarios.jsonl", help="Caminho do arquivo JSONL")
    parser.add_argument("--inicio", help="Data inicial esperada (YYYY-MM-DD)")
    parser.add_argument("--fim", help="Data final esperada (YYYY-MM-DD)")
    parser.add_argument(
        "--checar-fonte",
        action="store_true",
        help="Faz reconciliação ao vivo com o sitemap de Macapá e a API de Fortaleza",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    inicio = date.fromisoformat(args.inicio) if args.inicio else None
    fim = date.fromisoformat(args.fim) if args.fim else None

    registros, erros = carregar_jsonl(Path(args.arquivo))
    avisos: list[str] = []
    if registros:
        imprimir_resumo(registros)
        erros_q, avisos = verificar_esquema_e_qualidade(registros, inicio, fim)
        erros.extend(erros_q)
        erros.extend(verificar_duplicatas(registros))
        erros.extend(verificar_completude_local(registros))
        if args.checar_fonte:
            if not (inicio and fim):
                datas = [date.fromisoformat(r["data_publicacao"]) for r in registros if r.get("data_publicacao")]
                inicio = inicio or min(datas)
                fim = fim or max(datas)
            erros.extend(verificar_contra_fontes(registros, inicio, fim))

    if avisos:
        print(f"\nAVISOS ({len(avisos)}):")
        for aviso in avisos:
            print(f"  - {aviso}")

    if erros:
        print(f"\nFALHA NA VERIFICAÇÃO ({len(erros)} problema(s)):", file=sys.stderr)
        for err in erros:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    print("\nVERIFICAÇÃO OK: coleta completa, sem duplicatas e com todos os textos legíveis.")


if __name__ == "__main__":
    main()
