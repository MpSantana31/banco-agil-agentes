"""Regras de crédito: consulta de limite e pedido de aumento.

A decisão **não** passa pelo modelo: o LLM coleta o valor desejado, e quem
aprova/rejeita é esta camada, comparando o score do cliente com
``score_limite.csv``. É o que permite testar a regra sem chamar LLM.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence

from banco_agil.domain.modelos import (
    STATUS_APROVADO,
    STATUS_PENDENTE,
    STATUS_REJEITADO,
    Cliente,
    FaixaScore,
    SolicitacaoAumento,
)
from banco_agil.errors import DataStoreError, ValidationError
from banco_agil.tools.csv_repo import CsvRepository
from banco_agil.tools.validators import parse_valor_monetario

__all__ = ["ServicoCredito", "carregar_faixas"]

Relogio = Callable[[], dt.datetime]


def carregar_faixas(repo_score_limite: CsvRepository) -> list[FaixaScore]:
    """Lê e valida a tabela de faixas (sem sobreposição, cobrindo 0..1000)."""
    faixas = [FaixaScore.de_linha(linha) for linha in repo_score_limite.ler_todos()]
    if not faixas:
        raise DataStoreError("Tabela score_limite.csv vazia: não há como decidir limite.")

    faixas.sort(key=lambda faixa: faixa.score_min)
    for anterior, atual in zip(faixas, faixas[1:], strict=False):
        if atual.score_min <= anterior.score_max:
            raise DataStoreError(
                f"Faixas de score sobrepostas: {anterior.score_min}-{anterior.score_max} "
                f"e {atual.score_min}-{atual.score_max}."
            )
    return faixas


class ServicoCredito:
    """Consulta de limite e solicitação de aumento com base no score."""

    def __init__(
        self,
        repo_clientes: CsvRepository,
        repo_solicitacoes: CsvRepository,
        faixas: Sequence[FaixaScore],
        relogio: Relogio | None = None,
    ) -> None:
        if not faixas:
            raise DataStoreError("Serviço de crédito exige ao menos uma faixa de score.")
        self._clientes = repo_clientes
        self._solicitacoes = repo_solicitacoes
        self._faixas = list(faixas)
        self._relogio = relogio or (lambda: dt.datetime.now(dt.UTC))

    def cliente(self, cpf: str) -> Cliente:
        """Cliente atual (com o score já recalculado, se houve entrevista)."""
        linha = self._clientes.buscar(cpf)
        if linha is None:
            raise DataStoreError(f"Cliente {cpf} não encontrado na base.")
        return Cliente.de_linha(linha)

    def faixa_para(self, score: int) -> FaixaScore:
        """Faixa de score correspondente (levanta se o score estiver fora da tabela)."""
        for faixa in self._faixas:
            if faixa.contem(score):
                return faixa
        raise DataStoreError(
            f"Score {score} não está em nenhuma faixa de score_limite.csv "
            f"({self._faixas[0].score_min}..{self._faixas[-1].score_max})."
        )

    def limite_maximo_para(self, score: int) -> float:
        """Maior limite permitido para o score informado."""
        return self.faixa_para(score).limite_maximo

    def historico(self, cpf: str) -> list[SolicitacaoAumento]:
        """Solicitações já registradas para o cliente (mais recentes primeiro)."""
        linhas = self._solicitacoes.filtrar(cpf_cliente=cpf)
        registros = [SolicitacaoAumento.de_linha(linha) for linha in linhas]
        return list(reversed(registros))

    def solicitar_aumento(self, cpf: str, novo_limite: object) -> SolicitacaoAumento:
        """Registra o pedido de aumento e devolve o resultado da análise.

        Fluxo do enunciado, em duas etapas: o pedido é **registrado** com
        status ``pendente`` e, na sequência, a checagem de score o leva para
        ``aprovado`` ou ``rejeitado``. Se aprovado, o limite do cliente é
        atualizado em ``clientes.csv``.
        """
        cliente = self.cliente(cpf)
        valor_desejado = parse_valor_monetario(novo_limite)

        if valor_desejado <= cliente.limite_credito:
            raise ValidationError(
                f"O limite atual do cliente já é de {cliente.limite_credito:.2f}; "
                "o valor pedido precisa ser maior que isso."
            )

        pendente = SolicitacaoAumento(
            cpf=cliente.cpf,
            data_hora=self._relogio().isoformat(timespec="seconds"),
            limite_atual=cliente.limite_credito,
            novo_limite=valor_desejado,
            status=STATUS_PENDENTE,
            score_no_pedido=cliente.score,
            motivo="aguardando checagem de score",
        )
        # Criar e resolver acontecem sob o mesmo lock: `cpf` + `data_hora` (com
        # precisão de segundo) não é chave única, então sem isso a resolução de uma
        # sessão casaria com a linha aberta por outra no mesmo segundo.
        with self._solicitacoes.travar():
            self._solicitacoes.anexar(pendente.para_linha())

            faixa = self.faixa_para(cliente.score)
            aprovada = valor_desejado <= faixa.limite_maximo
            status = STATUS_APROVADO if aprovada else STATUS_REJEITADO
            motivo = (
                f"score {cliente.score} permite até {faixa.limite_maximo:.2f}"
                if aprovada
                else (
                    f"score {cliente.score} permite até {faixa.limite_maximo:.2f}; "
                    f"solicitado {valor_desejado:.2f}"
                )
            )

            linha_final = self._solicitacoes.atualizar_ultima_ocorrencia(
                {
                    "cpf_cliente": pendente.cpf,
                    "data_hora_solicitacao": pendente.data_hora,
                },
                status_pedido=status,
                motivo=motivo,
            )

            if aprovada:
                self._clientes.atualizar(cliente.cpf, limite_credito=f"{valor_desejado:.2f}")

        return SolicitacaoAumento.de_linha(linha_final)
