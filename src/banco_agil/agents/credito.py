"""Agente de Crédito — consulta de limite e pedido de aumento.

Escopo (enunciado): informar o limite disponível; registrar o pedido de aumento
em `solicitacoes_aumento_limite.csv`; checar o score contra `score_limite.csv`
para aprovar/rejeitar; se rejeitado, oferecer a Entrevista de Crédito (ou
encerrar, se o cliente recusar).

A decisão não está no prompt: quem aprova é `ServicoCredito`, em Python.
"""

from __future__ import annotations

from banco_agil.agents.base import EspecificacaoAgente
from banco_agil.agents.ferramentas import (
    F_CONSULTAR_LIMITE,
    F_ENCERRAR,
    F_HISTORICO,
    F_SOLICITAR_AUMENTO,
    F_TRANSFERIR,
)

__all__ = ["AGENTE_CREDITO", "PROMPT", "ESPECIFICACAO"]

AGENTE_CREDITO = "credito"

PROMPT = """\
## Seu papel agora: limites e crédito

1. Antes de falar de números, chame `consultar_limite` para ter o limite atual
   e o score do cliente. Nunca cite um valor que não veio da ferramenta.
2. Se o cliente quiser aumentar o limite, confirme o **valor desejado** em
   reais (uma pergunta só) e chame `solicitar_aumento_de_limite`.
3. Se a solicitação for aprovada: informe o novo limite e o que acontece a
   partir de agora, de forma direta.
4. Se for rejeitada: explique com transparência que o score atual não permite
   aquele valor, informe até quanto o score dele permite e ofereça a
   possibilidade de uma conversa rápida de atualização de dados para tentar
   reajustar o score. Se ele aceitar, chame `transferir_para` com destino
   `entrevista_de_credito` e motivo descrevendo o pedido que ficou pendente.
   Se ele recusar, ofereça ajuda em outro assunto ou encerrar.
5. Se o cliente quiser saber o histórico dos pedidos, use
   `consultar_historico_de_solicitacoes`.
6. Ao voltar de uma atualização de dados, releia o contexto: o score pode ter
   mudado. Refaça a análise com a ferramenta antes de responder.
"""

ESPECIFICACAO = EspecificacaoAgente(
    nome=AGENTE_CREDITO,
    titulo="Agente de Crédito",
    prompt=PROMPT,
    ferramentas=(
        F_CONSULTAR_LIMITE,
        F_SOLICITAR_AUMENTO,
        F_HISTORICO,
        F_TRANSFERIR,
        F_ENCERRAR,
    ),
    descricao="limite de crédito, pedido de aumento, histórico de pedidos",
)
