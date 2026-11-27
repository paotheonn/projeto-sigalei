"""Busca exata de nomes, CNPJs e números de processo/decreto no SQLite + FTS5.

Projetado para monitoramento jurídico com foco em recall (não perder menções):
- Busca textual: frase exata via FTS5 (insensível a acentos e maiúsculas)
- Busca por CNPJ (--cnpj): encontra com ou sem pontuação (07.954.605/0001-60 ou 07954605000160)
- Busca por processo/decreto (--processo): encontra mesmo variando espaços/separadores

Uso:
    python buscar.py "VITÓRIA CRISTINA GIBSON"
    python buscar.py --processo "6046681-54.2026.8.03.0001"
    python buscar.py --cnpj "07.954.605/0001-60"
    python buscar.py "8.243/2026" --fonte macapa
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path


def escapar_frase_fts(termo: str) -> str:
    """Envolve o termo em aspas duplas para busca exata de frase no FTS5."""
    limpo = termo.replace('"', '""').strip()
    return f'"{limpo}"'


def montar_regex_digitos(valor: str) -> tuple[re.Pattern, list[str]]:
    """Constrói regex exata para dígitos com separadores opcionais e blocos para pré-filtro FTS5."""
    blocos = re.findall(r"\d+", valor)
    digitos = "".join(blocos)
    if len(digitos) < 4:
        raise SystemExit(f"Erro: informe ao menos 4 dígitos para busca numérica (recebido: {valor!r}).")

    # Permite pontuação/espaço entre dígitos (ex.: 07.954.605/0001-60, 07954605000160 ou 8.243 / 2026)
    padrao = r"(?<!\d)" + r"[\s./\-]*".join(re.escape(ch) for ch in digitos) + r"(?!\d)"
    # Usa os blocos numéricos mais longos como pré-filtro rápido no FTS5 (ou fallback scan se vier grudado)
    return re.compile(padrao), blocos


def montar_trecho(texto: str, inicio: int, fim: int, janela: int = 90) -> str:
    a = max(0, inicio - janela)
    b = min(len(texto), fim + janela)
    trecho = texto[a:b].replace("\n", " ")
    trecho = re.sub(r"\s+", " ", trecho).strip()
    prefixo = "..." if a > 0 else ""
    sufixo = "..." if b < len(texto) else ""
    return f"{prefixo}{trecho}{sufixo}"


def montar_filtros_sql(args: argparse.Namespace) -> tuple[str, list]:
    clausulas = []
    params: list = []
    if args.fonte:
        clausulas.append("e.fonte = ?")
        params.append(args.fonte)
    if args.inicio:
        clausulas.append("e.data_publicacao >= ?")
        params.append(args.inicio)
    if args.fim:
        clausulas.append("e.data_publicacao <= ?")
        params.append(args.fim)
    where = (" AND " + " AND ".join(clausulas)) if clausulas else ""
    return where, params


def buscar_texto_exato(conn: sqlite3.Connection, termo: str, args: argparse.Namespace) -> list[dict]:
    where, params = montar_filtros_sql(args)
    sql = f"""
        SELECT
            e.fonte,
            e.data_publicacao,
            e.numero_edicao,
            e.tipo,
            e.url_documento,
            p.numero_pagina,
            p.metodo_extracao,
            p.confianca_ocr,
            snippet(paginas_fts, 0, '[', ']', '...', 24) AS trecho
        FROM paginas_fts
        JOIN paginas p ON p.id = paginas_fts.rowid
        JOIN edicoes e ON e.id = p.edicao_id
        WHERE paginas_fts MATCH ?
        {where}
        ORDER BY e.data_publicacao DESC, e.fonte, p.numero_pagina
        LIMIT ?
    """
    rows = conn.execute(sql, [escapar_frase_fts(termo), *params, args.limite]).fetchall()
    return [dict(r) for r in rows]


def buscar_numerico_exato(
    conn: sqlite3.Connection,
    valor: str,
    args: argparse.Namespace,
    eh_cnpj: bool = False,
) -> list[dict]:
    """Busca exata de CNPJ/processo tolerando variações de pontuação/espaço (inclusive entrada só-dígitos)."""
    regex, blocos = montar_regex_digitos(valor)
    where, params = montar_filtros_sql(args)
    digitos = "".join(blocos)

    # Monta pré-filtro FTS5 seguro mesmo quando o usuário passa o número sem pontuação
    fts_query: str | None = None
    if eh_cnpj and len(digitos) == 14:
        # Formato padrão XX.XXX.XXX/XXXX-XX ou corrido XXXXXXXXXXXXXX
        fts_query = f'("{digitos[2:5]}" AND "{digitos[5:8]}" AND "{digitos[8:12]}") OR "{digitos}"'
    elif (not eh_cnpj) and len(digitos) == 20:
        # Formato CNJ NNNNNNN-DD.AAAA.J.TR.OOOO ou corrido
        fts_query = f'("{digitos[:7]}" AND "{digitos[9:13]}") OR "{digitos}"'
    elif len(blocos) > 1 and max(len(b) for b in blocos) >= 4:
        maior_bloco = max(blocos, key=len)
        fts_query = f'"{maior_bloco}" OR "{digitos}"'

    if fts_query:
        sql = f"""
            SELECT
                e.fonte, e.data_publicacao, e.numero_edicao, e.tipo, e.url_documento,
                p.numero_pagina, p.metodo_extracao, p.confianca_ocr, p.texto
            FROM paginas_fts
            JOIN paginas p ON p.id = paginas_fts.rowid
            JOIN edicoes e ON e.id = p.edicao_id
            WHERE paginas_fts MATCH ?
            {where}
            ORDER BY e.data_publicacao DESC, e.fonte, p.numero_pagina
        """
        candidatos = conn.execute(sql, [fts_query, *params]).fetchall()
    else:
        # Número curto/genérico sem separadores: varredura direta nas páginas para garantir 100% de recall
        sql = f"""
            SELECT
                e.fonte, e.data_publicacao, e.numero_edicao, e.tipo, e.url_documento,
                p.numero_pagina, p.metodo_extracao, p.confianca_ocr, p.texto
            FROM paginas p
            JOIN edicoes e ON e.id = p.edicao_id
            WHERE 1=1
            {where}
            ORDER BY e.data_publicacao DESC, e.fonte, p.numero_pagina
        """
        candidatos = conn.execute(sql, params).fetchall()

    achados = []
    for row in candidatos:
        m = regex.search(row["texto"])
        if not m:
            continue
        item = dict(row)
        item["trecho"] = montar_trecho(row["texto"], m.start(), m.end())
        del item["texto"]
        achados.append(item)
        if len(achados) >= args.limite:
            break
    return achados


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Busca exata por nome, CNPJ ou número de processo no banco SQLite + FTS5."
    )
    parser.add_argument("termo", nargs="?", help="Texto/nome exato a procurar (insensível a acento e caixa)")
    parser.add_argument("--cnpj", help="Busca exata por CNPJ (com ou sem pontuação)")
    parser.add_argument("--processo", help="Busca exata por número de processo/decreto (com ou sem pontuação)")
    parser.add_argument("--banco", default="dados/diarios.db", help="Caminho do banco SQLite")
    parser.add_argument("--fonte", choices=["macapa", "fortaleza"], help="Filtrar por município")
    parser.add_argument("--inicio", help="Data inicial YYYY-MM-DD")
    parser.add_argument("--fim", help="Data final YYYY-MM-DD")
    parser.add_argument("--limite", type=int, default=20, help="Máximo de ocorrências exibidas")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    modos = sum(bool(x) for x in (args.termo, args.cnpj, args.processo))
    if modos != 1:
        raise SystemExit("Erro: informe exatamente um critério: <termo>, --cnpj ou --processo.")

    banco = Path(args.banco)
    if not banco.exists():
        raise SystemExit(f"Erro: banco não encontrado em {banco}.")

    conn = sqlite3.connect(banco)
    conn.row_factory = sqlite3.Row
    try:
        if args.cnpj:
            rotulo = f"CNPJ {args.cnpj}"
            achados = buscar_numerico_exato(conn, args.cnpj, args, eh_cnpj=True)
        elif args.processo:
            rotulo = f"processo/número {args.processo}"
            achados = buscar_numerico_exato(conn, args.processo, args, eh_cnpj=False)
        else:
            rotulo = f'"{args.termo}"'
            achados = buscar_texto_exato(conn, args.termo, args)
    finally:
        conn.close()

    print(f"Busca exata por {rotulo}: {len(achados)} ocorrência(s) encontrada(s).")
    for idx, hit in enumerate(achados, start=1):
        conf = f" | conf={hit['confianca_ocr']:.3f}" if hit["confianca_ocr"] is not None else ""
        print(
            f"\n[{idx}] {hit['fonte'].upper()} | {hit['data_publicacao']} | "
            f"Edição {hit['numero_edicao']} ({hit['tipo']}) | pág. {hit['numero_pagina']} "
            f"[{hit['metodo_extracao']}{conf}]"
        )
        print(f"    URL: {hit['url_documento']}")
        print(f"    Trecho: {hit['trecho']}")

    if not achados:
        sys.exit(0)


if __name__ == "__main__":
    main()
