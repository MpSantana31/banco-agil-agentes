"""Raiz de composição: monta repositórios e serviços a partir das configurações.

Nenhum agente instancia repositório: quem quiser crédito pede o
``ServicoCredito`` pronto. Trocar CSV por banco de dados acontece aqui, sem
tocar em agente, prompt ou ferramenta.
"""

from __future__ import annotations

from dataclasses import dataclass

from banco_agil.config import Settings, obter_settings
from banco_agil.domain.modelos import FaixaScore
from banco_agil.schema import (
    COLUNAS_CLIENTES,
    COLUNAS_SCORE_LIMITE,
    COLUNAS_SOLICITACOES,
)
from banco_agil.tools.auth import ServicoAutenticacao
from banco_agil.tools.credit import ServicoCredito, carregar_faixas
from banco_agil.tools.csv_repo import CsvRepository
from banco_agil.tools.fx import ServicoCambio

__all__ = ["Servicos", "construir_servicos"]


@dataclass(slots=True)
class Servicos:
    """Serviços de domínio prontos para uso pelos agentes."""

    settings: Settings
    clientes: CsvRepository
    solicitacoes: CsvRepository
    faixas: list[FaixaScore]
    autenticacao: ServicoAutenticacao
    credito: ServicoCredito
    cambio: ServicoCambio


def construir_servicos(settings: Settings | None = None) -> Servicos:
    """Monta os repositórios e serviços. Cria os CSVs que faltarem."""
    settings = settings or obter_settings()

    repo_clientes = CsvRepository(settings.caminho_clientes, colunas=COLUNAS_CLIENTES, chave="cpf")
    repo_score_limite = CsvRepository(settings.caminho_score_limite, colunas=COLUNAS_SCORE_LIMITE)
    repo_solicitacoes = CsvRepository(settings.caminho_solicitacoes, colunas=COLUNAS_SOLICITACOES)
    repo_clientes.garantir_arquivo()
    repo_score_limite.garantir_arquivo()
    repo_solicitacoes.garantir_arquivo()

    faixas = carregar_faixas(repo_score_limite)

    return Servicos(
        settings=settings,
        clientes=repo_clientes,
        solicitacoes=repo_solicitacoes,
        faixas=faixas,
        autenticacao=ServicoAutenticacao(repo_clientes),
        credito=ServicoCredito(repo_clientes, repo_solicitacoes, faixas),
        cambio=ServicoCambio(timeout=settings.fx_timeout, cache_ttl=settings.fx_cache_ttl_segundos),
    )
