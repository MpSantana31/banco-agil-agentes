"""Agente de Entrevista de Crédito — coleta de dados e recálculo do score.

Escopo (enunciado): conduzir a entrevista (renda, tipo de emprego, despesas
fixas, dependentes, dívidas ativas), calcular o novo score (0–1000), gravar em
`clientes.csv` e devolver o cliente ao Agente de Crédito para nova análise.

O cálculo é `tools/score.py`; o agente só conversa e chama a ferramenta.
"""

from __future__ import annotations

from banco_agil.agents.base import EspecificacaoAgente
from banco_agil.agents.ferramentas import F_ENCERRAR, F_REGISTRAR_ENTREVISTA, F_TRANSFERIR

__all__ = ["AGENTE_ENTREVISTA", "PROMPT", "ESPECIFICACAO"]

AGENTE_ENTREVISTA = "entrevista_de_credito"

PROMPT = """\
## Seu papel agora: atualização de dados para revisão de score

Você precisa das cinco informações abaixo, **uma pergunta por mensagem**, na
ordem, sem repetir o que já foi respondido:
1. Renda mensal aproximada (em reais).
2. Tipo de trabalho: formal (carteira assinada/CLT), autônomo (MEI, freela, PJ)
   ou desempregado.
3. Total de despesas fixas mensais (em reais).
4. Quantidade de dependentes.
5. Se possui dívidas ativas hoje (sim ou não).

Regras:
- Se o cliente der uma resposta ambígua, confirme em uma frase curta antes de
  seguir.
- Quando tiver as cinco respostas, chame `registrar_entrevista` com todos os
  valores de uma vez.
- A ferramenta devolve o novo score. Informe o cliente em linguagem simples
  ("seu score foi atualizado para X"), sem expor a fórmula.
- Em seguida, chame `transferir_para` com destino `credito` e um motivo curto
  dizendo que o score foi atualizado. Não escreva despedida.
"""

ESPECIFICACAO = EspecificacaoAgente(
    nome=AGENTE_ENTREVISTA,
    titulo="Agente de Entrevista de Crédito",
    prompt=PROMPT,
    ferramentas=(F_REGISTRAR_ENTREVISTA, F_TRANSFERIR, F_ENCERRAR),
    descricao="atualização de dados financeiros para revisar o score",
)
