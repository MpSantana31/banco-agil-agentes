"""Contrato de schema dos arquivos CSV — fonte única da verdade.

Os nomes das colunas de ``solicitacoes_aumento_limite.csv`` são **exatamente**
os do enunciado (``cpf_cliente``, ``data_hora_solicitacao``, ``limite_atual``,
``novo_limite_solicitado``, ``status_pedido``). As três últimas colunas são um
acréscimo nosso, para auditoria da decisão — nunca substituem as pedidas.
"""

from __future__ import annotations

__all__ = [
    "COLUNAS_CLIENTES",
    "COLUNAS_SCORE_LIMITE",
    "COLUNAS_SOLICITACOES",
    "NOME_CLIENTES",
    "NOME_SCORE_LIMITE",
    "NOME_SOLICITACOES",
]

NOME_CLIENTES = "clientes.csv"
NOME_SCORE_LIMITE = "score_limite.csv"
NOME_SOLICITACOES = "solicitacoes_aumento_limite.csv"

COLUNAS_CLIENTES: list[str] = [
    "cpf",
    "nome",
    "data_nascimento",
    "limite_credito",
    "score",
]

COLUNAS_SCORE_LIMITE: list[str] = [
    "score_min",
    "score_max",
    "limite_maximo",
]

# Colunas exigidas pelo enunciado, na ordem em que ele as lista.
COLUNAS_SOLICITACOES: list[str] = [
    "cpf_cliente",
    "data_hora_solicitacao",
    "limite_atual",
    "novo_limite_solicitado",
    "status_pedido",
    "score_no_pedido",
    "motivo",
]
