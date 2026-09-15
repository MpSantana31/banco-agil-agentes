"""Catálogo dos quatro agentes (registro usado pelo orquestrador).

Cada agente vive no seu próprio módulo (`triagem.py`, `credito.py`,
`entrevista.py`, `cambio.py`) com prompt e ferramentas. Aqui só montamos o
catálogo e aplicamos o prompt-base comum a todos os papéis.
"""

from __future__ import annotations

from dataclasses import replace

from banco_agil.agents import cambio, credito, entrevista, triagem
from banco_agil.agents.base import EspecificacaoAgente, RegistroDeAgentes
from banco_agil.agents.prompts import prompt_da_base
from banco_agil.config import Settings, obter_settings

__all__ = [
    "AGENTE_TRIAGEM",
    "AGENTE_CREDITO",
    "AGENTE_ENTREVISTA",
    "AGENTE_CAMBIO",
    "ESPECIFICACOES",
    "construir_registro",
]

AGENTE_TRIAGEM = triagem.AGENTE_TRIAGEM
AGENTE_CREDITO = credito.AGENTE_CREDITO
AGENTE_ENTREVISTA = entrevista.AGENTE_ENTREVISTA
AGENTE_CAMBIO = cambio.AGENTE_CAMBIO

# Ordem de exibição: triagem primeiro, depois os especialistas.
ESPECIFICACOES: tuple[EspecificacaoAgente, ...] = (
    triagem.ESPECIFICACAO,
    credito.ESPECIFICACAO,
    entrevista.ESPECIFICACAO,
    cambio.ESPECIFICACAO,
)


def construir_registro(settings: Settings | None = None) -> RegistroDeAgentes:
    """Monta o catálogo com o prompt-base (identidade única + destinos) aplicado."""
    settings = settings or obter_settings()

    catalogo = RegistroDeAgentes(ESPECIFICACOES)
    base = prompt_da_base(
        max_tentativas=settings.max_tentativas_autenticacao,
        destinos=catalogo.descricao_para_o_modelo(),
    )
    return RegistroDeAgentes(
        [
            replace(especificacao, prompt=f"{base}\n{especificacao.prompt}")
            for especificacao in ESPECIFICACOES
        ]
    )
