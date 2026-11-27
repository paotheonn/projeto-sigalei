"""Script independente de verificação de completude, unicidade e qualidade página por página.

Termina com código de saída 1 caso encontre qualquer problema e 0 se tudo estiver íntegro.
Não importa funções de `coletar.py`, evitando validação circular.

Uso:
    python verificar.py dados/diarios.db --inicio 2026-09-26 --fim 2026-11-25
    python verificar.py dados/diarios.db --inicio 2026-09-26 --fim 2026-11-25 --checar-fonte
"""

import argparse
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import requests

USER_AGENT = "SigaleiChallengeBot/1.0 (+verificador-independente)"
TIPOS_VALIDOS = {"normal", "extra", "suplemento"}
FONTES_ESPERADAS = {"macapa", "fortaleza"}
CAMPOS_OBRIGATORIOS = (
    "fonte",
    "data_publicacao",
    "numero_edicao",
    "tipo",
    "url_documento",
    "coletado_em",
    "metodo_extracao",
    "paginas",
    "sha256_pdf",
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
MACAPA_SITEMAP_INDEX_URL = "https://macapa.ap.gov.br/sitemap_index.xml"
FORTALEZA_BASE = "https://diariooficial.fortaleza.ce.gov.br"


def carregar_sqlite(caminho: Path) -> tuple[list[dict], dict[int, list[dict]], list[str]]:
    erros: list[str] = []
    if not caminho.exists():
        return [], {}, [f"Banco SQLite não encontrado: {caminho}"]

    conn = sqlite3.connect(caminho)
    conn.row_factory = sqlite3.Row
    try:
        tabelas = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()
        }
        esperadas = {"edicoes", "paginas", "paginas_fts", "edicoes_completas"}
        faltando = sorted(esperadas - tabelas)
        if faltando:
            return [], {}, [f"Tabelas/views obrigatórias ausentes no SQLite: {faltando}"]

        edicoes = [dict(r) for r in conn.execute("SELECT * FROM edicoes ORDER BY fonte, data_publicacao, numero_edicao")]
        if not edicoes:
            erros.append("Tabela 'edicoes' está vazia (0 registros).")

        paginas_por_edicao: dict[int, list[dict]] = defaultdict(list)
        for row in conn.execute("SELECT * FROM paginas ORDER BY edicao_id, numero_pagina"):
            paginas_por_edicao[row["edicao_id"]].append(dict(row))

        # Integridade do índice FTS5
        n_pag = conn.execute("SELECT COUNT(*) FROM paginas").fetchone()[0]
        n_fts = conn.execute("SELECT COUNT(*) FROM paginas_fts").fetchone()[0]
        if n_pag != n_fts:
            erros.append(f"Índice FTS5 dessincronizado: paginas={n_pag}, paginas_fts={n_fts}")

        # Verifica se a view edicoes_completas retorna texto para todas as edições
        n_view = conn.execute("SELECT COUNT(*) FROM edicoes_completas WHERE length(trim(coalesce(texto, ''))) > 0").fetchone()[0]
        if n_view != len(edicoes):
            erros.append(f"View edicoes_completas inconsistente: {n_view}/{len(edicoes)} edições com texto")

        return edicoes, paginas_por_edicao, erros
    except sqlite3.Error as err:
        return [], {}, [f"Erro ao ler SQLite {caminho}: {err}"]
    finally:
        conn.close()


def verificar_esquema_e_paginas(
    edicoes: list[dict],
    paginas_por_edicao: dict[int, list[dict]],
    inicio: date | None,
    fim: date | None,
) -> tuple[list[str], list[str]]:
    erros: list[str] = []
    avisos: list[str] = []

    for idx, ed in enumerate(edicoes, start=1):
        etiqueta = f"Edição #{idx} ({ed.get('fonte', '?')} {ed.get('numero_edicao', '?')})"

        for campo in CAMPOS_OBRIGATORIOS:
            val = ed.get(campo)
            if val is None or (isinstance(val, str) and not val.strip()):
                erros.append(f"{etiqueta}: campo obrigatório ausente ou vazio '{campo}'")

        if ed.get("fonte") not in FONTES_ESPERADAS:
            erros.append(f"{etiqueta}: fonte desconhecida '{ed.get('fonte')}'")

        if ed.get("tipo") not in TIPOS_VALIDOS:
            erros.append(f"{etiqueta}: tipo inválido '{ed.get('tipo')}'")

        url = str(ed.get("url_documento") or "")
        if not url.startswith("https://"):
            erros.append(f"{etiqueta}: URL inválida '{url}'")

        try:
            data_pub = date.fromisoformat(str(ed.get("data_publicacao") or ""))
            if inicio and data_pub < inicio:
                erros.append(f"{etiqueta}: data {data_pub} anterior ao início {inicio}")
            if fim and data_pub > fim:
                erros.append(f"{etiqueta}: data {data_pub} posterior ao fim {fim}")
        except ValueError:
            erros.append(f"{etiqueta}: data_publicacao inválida '{ed.get('data_publicacao')}'")

        coletado = str(ed.get("coletado_em") or "")
        try:
            dt = datetime.fromisoformat(coletado)
            if dt.tzinfo is None:
                erros.append(f"{etiqueta}: coletado_em sem fuso horário '{coletado}'")
        except ValueError:
            erros.append(f"{etiqueta}: coletado_em não é ISO 8601 válido '{coletado}'")

        n_esperado = ed.get("paginas")
        paginas = paginas_por_edicao.get(ed["id"], [])
        if not isinstance(n_esperado, int) or n_esperado <= 0:
            erros.append(f"{etiqueta}: contagem de páginas inválida ({n_esperado})")
            continue
        if len(paginas) != n_esperado:
            erros.append(f"{etiqueta}: divergência de páginas (edicoes.paginas={n_esperado}, tabela paginas={len(paginas)})")
            continue

        # Validação página por página (evita que PDF híbrido ou página vazia passe batido na média)
        nums_pagina = [p["numero_pagina"] for p in paginas]
        if nums_pagina != list(range(1, n_esperado + 1)):
            erros.append(f"{etiqueta}: numeração interna de páginas quebrada")

        paginas_curtas = []
        confs_baixas = []
        for p in paginas:
            txt_p = str(p.get("texto") or "").strip()
            alfanum_p = sum(ch.isalnum() for ch in txt_p)
            if alfanum_p < 40:
                paginas_curtas.append((p["numero_pagina"], alfanum_p))
            conf = p.get("confianca_ocr")
            if conf is not None:
                if conf < 0.60:
                    erros.append(f"{etiqueta} pág {p['numero_pagina']}: confiança OCR crítica ({conf:.2f})")
                elif conf < 0.85:
                    confs_baixas.append((p["numero_pagina"], conf))

        if paginas_curtas:
            # Uma página isolada muito curta pode ser folha em branco/carimbo; > 20% indica falha grave
            if len(paginas_curtas) > max(2, int(n_esperado * 0.20)):
                erros.append(
                    f"{etiqueta}: muitas páginas quase vazias ({len(paginas_curtas)}/{n_esperado}): {paginas_curtas[:5]}"
                )
            else:
                avisos.append(f"{etiqueta}: página(s) muito curta(s) (< 40 alfanum): {paginas_curtas}")

        if confs_baixas:
            avisos.append(
                f"{etiqueta}: {len(confs_baixas)} página(s) com confiança OCR < 0.85: "
                f"{[(n, round(c, 3)) for n, c in confs_baixas[:5]]}"
            )

        conf_media = ed.get("confianca_media")
        if conf_media is not None and conf_media < 0.75:
            erros.append(f"{etiqueta}: confiança média de OCR muito baixa ({conf_media:.3f})")

        # Validação no texto consolidado da edição
        texto_completo = "\n\n".join(str(p.get("texto") or "").strip() for p in paginas).strip()
        if len(texto_completo) < 500:
            erros.append(f"{etiqueta}: texto consolidado muito curto ({len(texto_completo)} chars)")
            continue

        if len(texto_completo) / n_esperado < 150:
            erros.append(
                f"{etiqueta}: média baixa de texto por página "
                f"({len(texto_completo) / n_esperado:.0f} chars/pág em {n_esperado} págs)"
            )

        qtd_invalidos = texto_completo.count("\ufffd")
        if qtd_invalidos / len(texto_completo) > 0.005:
            erros.append(f"{etiqueta}: excesso de caracteres ilegíveis U+FFFD ({qtd_invalidos})")

        nao_espaco = re.sub(r"\s+", "", texto_completo)
        if nao_espaco:
            alfanum = sum(ch.isalnum() for ch in nao_espaco)
            if alfanum / len(nao_espaco) < 0.50:
                erros.append(
                    f"{etiqueta}: proporção baixa de caracteres alfanuméricos ({alfanum / len(nao_espaco):.1%})"
                )

        texto_lower = texto_completo.lower()
        hits = sum(1 for termo in PALAVRAS_DIARIO if termo in texto_lower)
        if hits < 3:
            erros.append(f"{etiqueta}: texto não parece um diário oficial em português (apenas {hits} termos-chave)")

        num_digitos = re.sub(r"\D", "", str(ed.get("numero_edicao") or ""))
        if num_digitos:
            digitos_texto = re.sub(r"[\s.]+", "", texto_completo)
            if num_digitos not in digitos_texto:
                avisos.append(
                    f"{etiqueta}: número '{num_digitos}' não encontrado literalmente no texto "
                    "(pode ser erro de capa na própria fonte)"
                )

    return erros, avisos


def verificar_duplicatas(edicoes: list[dict]) -> list[str]:
    erros: list[str] = []
    vistos_chave: set[tuple[str, str, str, str]] = set()
    vistos_url: set[str] = set()
    vistos_hash: dict[tuple[str, str], str] = {}

    for ed in edicoes:
        fonte = str(ed.get("fonte") or "")
        chave = (
            fonte,
            str(ed.get("data_publicacao") or ""),
            str(ed.get("numero_edicao") or ""),
            str(ed.get("tipo") or ""),
        )
        if chave in vistos_chave:
            erros.append(f"Duplicata de edição: {chave}")
        vistos_chave.add(chave)

        url = str(ed.get("url_documento") or "")
        if url in vistos_url:
            erros.append(f"Duplicata de URL: {url}")
        vistos_url.add(url)

        sha = ed.get("sha256_pdf")
        if sha:
            chave_hash = (fonte, str(sha))
            if chave_hash in vistos_hash:
                erros.append(
                    f"PDF idêntico duplicado em {fonte}: edição {ed.get('numero_edicao')} "
                    f"tem mesmo SHA-256 que edição {vistos_hash[chave_hash]}"
                )
            vistos_hash[chave_hash] = str(ed.get("numero_edicao") or "")

    return erros


def verificar_completude_local(edicoes: list[dict], inicio: date | None, fim: date | None) -> list[str]:
    erros: list[str] = []
    por_fonte: dict[str, list[dict]] = defaultdict(list)
    for ed in edicoes:
        por_fonte[str(ed.get("fonte") or "")].append(ed)

    for fonte in sorted(FONTES_ESPERADAS):
        itens = por_fonte.get(fonte, [])
        if not itens:
            erros.append(f"Fonte ausente nos dados coletados: {fonte}")
            continue

        numeros_normais = [
            int(e["numero_edicao"])
            for e in itens
            if e.get("tipo") == "normal" and str(e.get("numero_edicao", "")).isdigit()
        ]
        if numeros_normais:
            menor, maior = min(numeros_normais), max(numeros_normais)
            faltando = sorted(set(range(menor, maior + 1)) - set(numeros_normais))
            if faltando:
                erros.append(
                    f"[{fonte}] Furo na numeração sequencial ({menor}..{maior}): edições ausentes = {faltando}"
                )

        datas = sorted({date.fromisoformat(e["data_publicacao"]) for e in itens if e.get("data_publicacao")})
        if datas:
            if inicio and (datas[0] - inicio).days > 6:
                erros.append(f"[{fonte}] Lacuna no início do período: pedido {inicio}, primeira edição em {datas[0]}")
            if fim and (fim - datas[-1]).days > 6:
                erros.append(f"[{fonte}] Lacuna no fim do período: pedido até {fim}, última edição em {datas[-1]}")
            for d1, d2 in zip(datas, datas[1:]):
                gap = (d2 - d1).days
                if gap > 6:
                    erros.append(f"[{fonte}] Lacuna incomum de {gap} dias sem edição entre {d1} e {d2}")

    return erros


def consultar_fortaleza_independente(inicio: date, fim: date) -> set[tuple[str, str, str, str]]:
    """Consulta diretamente a API de Fortaleza sem reutilizar código de coletar.py."""
    sessao = requests.Session()
    sessao.headers.update({"User-Agent": USER_AGENT})

    cfg = sessao.get(f"{FORTALEZA_BASE}/api/captcha-required", timeout=30)
    cfg.raise_for_status()
    cfg_json = cfg.json()
    if cfg_json.get("required"):
        raise RuntimeError("API de Fortaleza exigiu CAPTCHA durante a verificação.")
    intervalo = max(5.5, float(cfg_json.get("intervalSeconds", 5.0)) + 0.5)

    esperadas: set[tuple[str, str, str, str]] = set()
    primeira = True
    ano, mes = inicio.year, inicio.month
    while (ano, mes) <= (fim.year, fim.month):
        pagina = 1
        while True:
            if not primeira:
                time.sleep(intervalo)
            primeira = False

            resp = sessao.post(
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
                timeout=45,
            )
            resp.raise_for_status()
            lista = resp.json().get("list", {})
            for bruto in lista.get("entries", []):
                item = bruto.get("entry", bruto)
                data_str = str(item.get("dataPublicacao") or item.get("data_publicacao") or "")[:10]
                if not data_str:
                    continue
                data_pub = date.fromisoformat(data_str)
                if not (inicio <= data_pub <= fim):
                    continue
                numero = str(item.get("nrDiarioOficial") or item.get("nr_diario_oficial") or "").strip()
                ref = f"{item.get('name') or ''} {item.get('tituloDescricao') or ''}".lower()
                if re.search(r"\bsuplemento\b", ref) or re.search(r"s$", numero, re.IGNORECASE):
                    tipo = "suplemento"
                elif re.search(r"\bextra\b", ref):
                    tipo = "extra"
                else:
                    tipo = "normal"
                esperadas.add(("fortaleza", data_pub.isoformat(), numero, tipo))

            if not lista.get("pagination", {}).get("hasMoreItems"):
                break
            pagina += 1

        if mes == 12:
            ano, mes = ano + 1, 1
        else:
            mes += 1

    return esperadas


def verificar_contra_fontes(edicoes: list[dict], inicio: date, fim: date) -> list[str]:
    """Reconciliação ao vivo independente: sitemap XML de Macapá e API de Fortaleza."""
    erros: list[str] = []
    chaves_locais = {
        (e["fonte"], e["data_publicacao"], e["numero_edicao"], e["tipo"]) for e in edicoes
    }

    # 1. Macapá — prova independente via Yoast XML Sitemap
    print("[checar-fonte] Consultando sitemap independente de Macapá...")
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    idx_resp = requests.get(MACAPA_SITEMAP_INDEX_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    idx_resp.raise_for_status()
    idx_root = ET.fromstring(idx_resp.content)

    sitemaps_macapa = []
    for sm in idx_root.findall(".//sm:sitemap", ns):
        loc = (sm.findtext("sm:loc", default="", namespaces=ns) or "").strip()
        lastmod = (sm.findtext("sm:lastmod", default="", namespaces=ns) or "")[:10]
        if "official_diaries-sitemap" not in loc:
            continue
        if lastmod and date.fromisoformat(lastmod) < inicio:
            continue
        sitemaps_macapa.append(loc)

    sitemap_macapa = set()
    for sm_url in sitemaps_macapa:
        resp = requests.get(sm_url, headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        for loc in root.findall(".//sm:loc", ns):
            url = (loc.text or "").strip()
            m = re.search(r"/diarios-oficiais/(\d+(?:-[a-z])?)-(\d{2})-(\d{2})-(\d{4})/?$", url, re.IGNORECASE)
            if not m:
                continue
            num_raw, dd, mm, yyyy = m.groups()
            data_pub = date(int(yyyy), int(mm), int(dd))
            if inicio <= data_pub <= fim:
                num = num_raw.upper()
                url_low = url.lower()
                if re.search(r"\bsuplemento\b", url_low):
                    tipo = "suplemento"
                elif re.search(r"-[A-Z]$", num) or re.search(r"\bextra\b", url_low):
                    tipo = "extra"
                else:
                    tipo = "normal"
                sitemap_macapa.add(("macapa", data_pub.isoformat(), num, tipo))

    locais_mcp = {c for c in chaves_locais if c[0] == "macapa"}
    faltando_mcp = sorted(sitemap_macapa - locais_mcp)
    extras_mcp = sorted(locais_mcp - sitemap_macapa)
    if faltando_mcp:
        erros.append(f"[macapa] Edições no sitemap ausentes no banco: {faltando_mcp}")
    if extras_mcp:
        erros.append(f"[macapa] Edições no banco ausentes no sitemap: {extras_mcp}")
    if not faltando_mcp and not extras_mcp:
        print(f"  [macapa] OK — todas as {len(sitemap_macapa)} edições do sitemap batem 100% com o banco.")

    # 2. Fortaleza — consulta independente na API oficial
    print("[checar-fonte] Consultando API oficial de Fortaleza (verificação independente)...")
    esperadas_fort = consultar_fortaleza_independente(inicio, fim)
    locais_fort = {c for c in chaves_locais if c[0] == "fortaleza"}
    faltando_fort = sorted(esperadas_fort - locais_fort)
    extras_fort = sorted(locais_fort - esperadas_fort)
    if faltando_fort:
        erros.append(f"[fortaleza] Edições na API ausentes no banco: {faltando_fort}")
    if extras_fort:
        erros.append(f"[fortaleza] Edições no banco ausentes na API: {extras_fort}")
    if not faltando_fort and not extras_fort:
        print(f"  [fortaleza] OK — todas as {len(esperadas_fort)} edições únicas da API batem 100% com o banco.")

    return erros


def imprimir_resumo(edicoes: list[dict], paginas_por_edicao: dict[int, list[dict]]) -> None:
    por_fonte: dict[str, list[dict]] = defaultdict(list)
    for ed in edicoes:
        por_fonte[str(ed.get("fonte") or "?")].append(ed)

    print("\n=== Resumo da Coleta ===")
    for fonte in sorted(por_fonte):
        itens = por_fonte[fonte]
        datas = sorted(e["data_publicacao"] for e in itens if e.get("data_publicacao"))
        tipos: dict[str, int] = defaultdict(int)
        metodos_pag: dict[str, int] = defaultdict(int)
        confs = []
        total_pags = 0
        for e in itens:
            tipos[str(e.get("tipo"))] += 1
            for p in paginas_por_edicao.get(e["id"], []):
                total_pags += 1
                metodos_pag[str(p.get("metodo_extracao"))] += 1
                if p.get("confianca_ocr") is not None:
                    confs.append(p["confianca_ocr"])
        nums = [int(e["numero_edicao"]) for e in itens if str(e.get("numero_edicao", "")).isdigit()]
        faixa_num = f"{min(nums)}..{max(nums)}" if nums else "n/d"
        conf_str = f" | conf_ocr_media={sum(confs)/len(confs):.3f}" if confs else ""
        print(
            f"- {fonte}: {len(itens)} edições ({total_pags} págs) | período {datas[0]} a {datas[-1]} | "
            f"numeração {faixa_num} | tipos={dict(tipos)} | páginas={dict(metodos_pag)}{conf_str}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verifica completude, duplicatas e legibilidade página por página no SQLite."
    )
    parser.add_argument("arquivo", nargs="?", default="dados/diarios.db", help="Caminho do banco SQLite")
    parser.add_argument("--inicio", help="Data inicial esperada (YYYY-MM-DD)")
    parser.add_argument("--fim", help="Data final esperada (YYYY-MM-DD)")
    parser.add_argument(
        "--checar-fonte",
        action="store_true",
        help="Faz reconciliação ao vivo independente com o sitemap de Macapá e a API de Fortaleza",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    inicio = date.fromisoformat(args.inicio) if args.inicio else None
    fim = date.fromisoformat(args.fim) if args.fim else None

    edicoes, paginas_por_edicao, erros = carregar_sqlite(Path(args.arquivo))
    avisos: list[str] = []
    if edicoes:
        imprimir_resumo(edicoes, paginas_por_edicao)
        erros_q, avisos = verificar_esquema_e_paginas(edicoes, paginas_por_edicao, inicio, fim)
        erros.extend(erros_q)
        erros.extend(verificar_duplicatas(edicoes))
        erros.extend(verificar_completude_local(edicoes, inicio, fim))
        if args.checar_fonte:
            if not (inicio and fim):
                datas = [date.fromisoformat(e["data_publicacao"]) for e in edicoes if e.get("data_publicacao")]
                inicio = inicio or min(datas)
                fim = fim or max(datas)
            erros.extend(verificar_contra_fontes(edicoes, inicio, fim))

    if avisos:
        print(f"\nAVISOS ({len(avisos)}):")
        for aviso in avisos:
            print(f"  - {aviso}")

    if erros:
        print(f"\nFALHA NA VERIFICAÇÃO ({len(erros)} problema(s)):", file=sys.stderr)
        for err in erros:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    print("\nVERIFICAÇÃO OK: coleta completa, sem duplicatas, FTS5 íntegro e todas as páginas legíveis.")


if __name__ == "__main__":
    main()
