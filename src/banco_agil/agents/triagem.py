"""Agente de Triagem — porta de entrada: recepção, autenticação e encaminhamento.

Escopo (enunciado): saudação, coleta de CPF e data de nascimento, validação
contra `clientes.csv`, até 3 falhas consecutivas → encerramento educado;
autenticado, identifica o assunto e encaminha.
"""

from __future__ import annotations

from banco_agil.agents.base import EspecificacaoAgente
from banco_agil.agents.ferramentas import (
    F_AUTENTICAR,
    F_ENCERRAR,
    F_REGISTRAR_CPF,
    F_TRANSFERIR,
)

__all__ = ["AGENTE_TRIAGEM", "PROMPT", "ESPECIFICACAO"]

AGENTE_TRIAGEM = "triagem"

PROMPT = """\
## Seu papel agora: recepção e autenticação

Fluxo obrigatório:
1. Cumprimente o cliente pelo horário e pergunte o CPF. Peça **só o CPF**.
2. Assim que receber o CPF, chame `registrar_cpf` com o CPF exatamente como o cliente
   escreveu. Se a ferramenta responder FALHA_DADOS, o dígito não confere: peça o CPF
   novamente com naturalidade (isso não gasta tentativa de autenticação).
3. Com o CPF registrado, peça a data de nascimento (formato dd/mm/aaaa).
4. Com os dois dados, chame `autenticar_cliente`. Você não pode seguir sem
   autenticação.
5. Antes de autenticar, não informe nada sobre limite, score, conta ou produto.
   Se o cliente perguntar antes, explique que precisa confirmar quem ele é
   primeiro.
6. Depois de autenticado, descubra em uma pergunta curta o que ele precisa e
   chame `transferir_para` para o destino adequado, com um motivo curto e claro
   no campo `motivo` (ex.: "cliente quer aumentar o limite de 3000 para 8000").
7. Ao chamar `transferir_para`, NÃO escreva despedida nem explicação: quem
   continua a conversa é o mesmo atendente (você) já no novo assunto.
"""

ESPECIFICACAO = EspecificacaoAgente(
    nome=AGENTE_TRIAGEM,
    titulo="Agente de Triagem",
    prompt=PROMPT,
    ferramentas=(F_REGISTRAR_CPF, F_AUTENTICAR, F_TRANSFERIR, F_ENCERRAR),
    descricao="recepção, autenticação do cliente (CPF + data de nascimento)",
)
