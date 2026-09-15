"""O que a interface deve mostrar no próximo turno — decidido em Python puro.

Por que existe: um campo dedicado de CPF (com máscara e validação de dígito) e um
calendário para a data de nascimento só são seguros se a **interface souber** qual
dado está faltando. Essa informação vem da máquina de estados, não do texto que o
modelo escreveu: interpretar a fala do modelo para decidir a UI seria trocar uma
decisão determinística por uma suposição.

Nada aqui chama rede, modelo ou disco.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from banco_agil.domain.modelos import formatar_moeda
from banco_agil.orchestrator.estado import EstadoSessao
from banco_agil.tools.validators import mascarar_cpf

__all__ = ["CampoEsperado", "campo_esperado", "dados_visiveis"]


class CampoEsperado(StrEnum):
    """Dado que o atendimento está aguardando agora."""

    CPF = "cpf"
    DATA_NASCIMENTO = "data_nascimento"


@dataclass(frozen=True, slots=True)
class DadosVisiveis:
    """Dados do cliente que a interface pode exibir **depois** de autenticado."""

    nome: str
    primeiro_nome: str
    cpf_mascarado: str
    limite: str
    score: int
    faixa: str

    def como_dicionario(self) -> dict[str, Any]:
        return {
            "nome": self.nome,
            "primeiro_nome": self.primeiro_nome,
            "cpf": self.cpf_mascarado,
            "limite": self.limite,
            "score": self.score,
            "faixa": self.faixa,
        }


def campo_esperado(estado: EstadoSessao) -> CampoEsperado | None:
    """Devolve o dado que falta, ou ``None`` quando é melhor deixar texto livre."""
    if estado.encerrado:
        return None
    if estado.autenticado:
        return None
    if not estado.cpf_informado:
        return CampoEsperado.CPF
    return CampoEsperado.DATA_NASCIMENTO


def dados_visiveis(estado: EstadoSessao) -> DadosVisiveis | None:
    """Dados do cliente para exibição — ``None`` enquanto não houver autenticação.

    A guarda é intencional: antes de autenticar, o atendimento não pode revelar
    nada sobre limite, score ou existência do CPF na base.
    """
    if not estado.autenticado or estado.cliente is None:
        return None
    cliente = estado.cliente
    return DadosVisiveis(
        nome=cliente.nome,
        primeiro_nome=cliente.primeiro_nome,
        cpf_mascarado=mascarar_cpf(cliente.cpf),
        limite=formatar_moeda(cliente.limite_credito),
        score=cliente.score,
        faixa=cliente.faixa,
    )
