"""Gera a base inicial do Banco Ágil (dados fictícios, CPFs com DV válido).

Uso:
    uv run python scripts/gerar_dados_iniciais.py            # cria o que faltar
    uv run python scripts/gerar_dados_iniciais.py --forcar    # recria tudo

Os CPFs são calculados com dígitos verificadores reais para que a validação do
Agente de Triagem seja exercitada de verdade — não são "números bonitos".
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from banco_agil.schema import (
    COLUNAS_CLIENTES,
    COLUNAS_SCORE_LIMITE,
    COLUNAS_SOLICITACOES,
)

RAIZ = Path(__file__).resolve().parents[1]
DATA_DIR = RAIZ / "data"

CLIENTES: list[tuple[str, str, str, str, int]] = [
    ("529982247", "Maria Souza", "1990-05-12", "3000.00", 650),
    ("390533447", "João Conceição", "1985-11-03", "8000.00", 820),
    ("862883467", "Ana Paula Ribeiro", "1998-02-27", "1500.00", 410),
    ("971583240", "Carlos Eduardo Lima", "1979-07-19", "20000.00", 930),
    ("142639785", "Fernanda Alves", "2001-09-08", "500.00", 180),
]

FAIXAS: list[tuple[int, int, str]] = [
    (0, 199, "500.00"),
    (200, 399, "1500.00"),
    (400, 599, "3000.00"),
    (600, 799, "8000.00"),
    (800, 1000, "20000.00"),
]


def digito_verificador(digitos: str) -> str:
    pesos = range(len(digitos) + 1, 1, -1)
    soma = sum(int(d) * peso for d, peso in zip(digitos, pesos, strict=True))
    resto = soma % 11
    return "0" if resto < 2 else str(11 - resto)


def cpf_com_dv(base9: str) -> str:
    primeiro = digito_verificador(base9)
    return base9 + primeiro + digito_verificador(base9 + primeiro)


def escrever(caminho: Path, colunas: list[str], linhas: list[list[str]]) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", encoding="utf-8", newline="") as arquivo:
        escritor = csv.writer(arquivo, lineterminator="\n")
        escritor.writerow(colunas)
        escritor.writerows(linhas)
    print(f"  ok  {caminho.relative_to(RAIZ)} ({len(linhas)} linha(s))")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--forcar",
        action="store_true",
        help="Recria também o CSV de solicitações (apaga o histórico existente).",
    )
    argumentos = parser.parse_args()

    print("Gerando base de dados do Banco Ágil:")

    escrever(
        DATA_DIR / "clientes.csv",
        COLUNAS_CLIENTES,
        [
            [cpf_com_dv(base), nome, nascimento, limite, str(score)]
            for base, nome, nascimento, limite, score in CLIENTES
        ],
    )

    escrever(
        DATA_DIR / "score_limite.csv",
        COLUNAS_SCORE_LIMITE,
        [[str(minimo), str(maximo), limite] for minimo, maximo, limite in FAIXAS],
    )

    caminho_solicitacoes = DATA_DIR / "solicitacoes_aumento_limite.csv"
    if caminho_solicitacoes.exists() and not argumentos.forcar:
        print(f"  --  {caminho_solicitacoes.relative_to(RAIZ)} já existe (mantido)")
    else:
        escrever(caminho_solicitacoes, COLUNAS_SOLICITACOES, [])

    print("\nCPFs de teste (válidos, use em qualquer data de nascimento acima):")
    for base, nome, *_ in CLIENTES:
        print(f"  {cpf_com_dv(base)}  {nome}")


if __name__ == "__main__":
    main()
