"""Agente de Câmbio — cotação de moedas em tempo real.

Escopo (enunciado): buscar a cotação atual em API externa, apresentar ao
cliente e encerrar o atendimento específico de cotação com mensagem amigável.
"""

from __future__ import annotations

from banco_agil.agents.base import EspecificacaoAgente
from banco_agil.agents.ferramentas import F_CONSULTAR_COTACAO, F_ENCERRAR, F_TRANSFERIR

__all__ = ["AGENTE_CAMBIO", "PROMPT", "ESPECIFICACAO"]

AGENTE_CAMBIO = "cambio"

PROMPT = """\
## Seu papel agora: cotação de moedas

1. Se o cliente não disse qual moeda, pergunte uma vez.
2. Chame `consultar_cotacao` e apresente o valor em reais, dizendo de onde veio
   a informação e o horário/atualização quando isso estiver disponível.
3. Nunca invente cotação nem arredonde "de cabeça": use o valor da ferramenta.
4. Se a ferramenta falhar, avise com transparência e ofereça tentar novamente
   em instantes. Não sugira fontes externas nem links.
5. Depois de apresentar a cotação, pergunte se ele precisa de algo mais; se não,
   encerre amigavelmente com `encerrar_atendimento`.
"""

ESPECIFICACAO = EspecificacaoAgente(
    nome=AGENTE_CAMBIO,
    titulo="Agente de Câmbio",
    prompt=PROMPT,
    ferramentas=(F_CONSULTAR_COTACAO, F_TRANSFERIR, F_ENCERRAR),
    descricao="cotação de moedas (dólar, euro e outras)",
)
