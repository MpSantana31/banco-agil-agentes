"""Modelos de domínio do Banco Ágil.

São *dataclasses* puras: a conversão de/para as linhas dos CSVs mora aqui, então
nenhuma outra camada precisa saber que o armazenamento é CSV (trocar por
Postgres = trocar só o repositório).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from banco_agil.errors import DataStoreError

__all__ = [
    "Cliente",
    "FaixaScore",
    "SolicitacaoAumento",
    "Cotacao",
    "STATUS_PENDENTE",
    "STATUS_APROVADO",
    "STATUS_REJEITADO",
    "normalizar_status",
    "formatar_moeda",
]


def _numero(valor: object) -> float:
    """Converte para float rejeitando NaN/infinito — dado corrompido não passa."""
    try:
        numero = float(str(valor).strip())
    except (TypeError, ValueError) as erro:
        raise DataStoreError(f"valor numérico inválido: {valor!r}.") from erro
    if not math.isfinite(numero):
        raise DataStoreError(f"valor numérico não finito: {valor!r}.")
    return numero


def _inteiro(valor: object) -> int:
    """Converte para inteiro, rejeitando fracionário e não finito."""
    numero = _numero(valor)
    if numero != int(numero):
        raise DataStoreError(f"esperado número inteiro, veio: {valor!r}.")
    return int(numero)


def formatar_moeda(valor: float) -> str:
    """Formata em reais no padrão brasileiro: ``R$ 8.000,00``."""
    texto = f"{valor:,.2f}"
    return "R$ " + texto.replace(",", "@").replace(".", ",").replace("@", ".")


STATUS_PENDENTE = "pendente"
STATUS_APROVADO = "aprovado"
STATUS_REJEITADO = "rejeitado"
# Enunciado diz "reprovado", CSV diz "rejeitado": grava "rejeitado" e aceita o sinônimo.
_ALIASES_STATUS = {"reprovado": STATUS_REJEITADO, "negado": STATUS_REJEITADO}


def normalizar_status(valor: object) -> str:
    """Normaliza o texto de status para ``pendente``, ``aprovado`` ou ``rejeitado``."""
    texto = str(valor or "").strip().lower()
    texto = _ALIASES_STATUS.get(texto, texto)
    if texto not in (STATUS_PENDENTE, STATUS_APROVADO, STATUS_REJEITADO):
        raise DataStoreError(f"Status desconhecido: {valor!r}.")
    return texto


@dataclass(frozen=True, slots=True)
class Cliente:
    """Cliente autenticado na base."""

    cpf: str
    nome: str
    data_nascimento: dt.date
    limite_credito: float
    score: int

    @classmethod
    def de_linha(cls, linha: dict[str, str]) -> Cliente:
        try:
            return cls(
                cpf=linha["cpf"],
                nome=linha["nome"],
                data_nascimento=dt.date.fromisoformat(linha["data_nascimento"]),
                limite_credito=_numero(linha["limite_credito"]),
                score=_inteiro(linha["score"]),
            )
        except (KeyError, ValueError) as erro:
            raise DataStoreError(f"Registro de cliente inválido: {erro}.") from erro

    @property
    def primeiro_nome(self) -> str:
        return self.nome.split()[0]

    @property
    def faixa(self) -> str:
        """Faixa de score, só para exibição."""
        if self.score >= 800:
            return "muito bom"
        if self.score >= 600:
            return "bom"
        if self.score >= 400:
            return "regular"
        if self.score >= 200:
            return "baixo"
        return "muito baixo"


@dataclass(frozen=True, slots=True)
class FaixaScore:
    """Faixa de score -> limite máximo permitido (linha de ``score_limite.csv``)."""

    score_min: int
    score_max: int
    limite_maximo: float

    @classmethod
    def de_linha(cls, linha: dict[str, str]) -> FaixaScore:
        try:
            return cls(
                score_min=_inteiro(linha["score_min"]),
                score_max=_inteiro(linha["score_max"]),
                limite_maximo=_numero(linha["limite_maximo"]),
            )
        except (KeyError, ValueError) as erro:
            raise DataStoreError(f"Faixa de score inválida: {erro}.") from erro

    def contem(self, score: int) -> bool:
        return self.score_min <= score <= self.score_max


@dataclass(frozen=True, slots=True)
class SolicitacaoAumento:
    """Pedido de aumento de limite, com o resultado da checagem de score."""

    cpf: str
    data_hora: str
    limite_atual: float
    novo_limite: float
    status: str
    score_no_pedido: int
    motivo: str = field(default="")

    @classmethod
    def de_linha(cls, linha: dict[str, str]) -> SolicitacaoAumento:
        try:
            return cls(
                cpf=linha["cpf_cliente"],
                data_hora=linha["data_hora_solicitacao"],
                limite_atual=_numero(linha["limite_atual"]),
                novo_limite=_numero(linha["novo_limite_solicitado"]),
                status=normalizar_status(linha["status_pedido"]),
                score_no_pedido=_inteiro(linha.get("score_no_pedido") or 0),
                motivo=linha.get("motivo", ""),
            )
        except (KeyError, ValueError) as erro:
            raise DataStoreError(f"Solicitação inválida: {erro}.") from erro

    @property
    def aprovada(self) -> bool:
        return self.status == STATUS_APROVADO

    @property
    def pendente(self) -> bool:
        return self.status == STATUS_PENDENTE

    def para_linha(self) -> dict[str, str]:
        # Nomes de coluna exatamente como no enunciado.
        return {
            "cpf_cliente": self.cpf,
            "data_hora_solicitacao": self.data_hora,
            "limite_atual": f"{self.limite_atual:.2f}",
            "novo_limite_solicitado": f"{self.novo_limite:.2f}",
            "status_pedido": self.status,
            "score_no_pedido": str(self.score_no_pedido),
            "motivo": self.motivo,
        }


@dataclass(frozen=True, slots=True)
class Cotacao:
    """Cotação de moeda vinda de provedor externo."""

    moeda: str
    nome: str
    valor: float
    atualizado_em: str
    fonte: str

    def valor_formatado(self) -> str:
        return formatar_moeda(self.valor)
