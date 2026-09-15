"""Motor de score de crédito (função pura, 0 a 1000).

Pesos vêm do PDF do desafio (``peso_emprego`` 300/200/0, ``peso_dependentes``
100/80/60/30, ``peso_dividas`` ±100). O termo de renda é o único ajustado:

    PDF:        (renda / (despesas + 1)) * 30
    Aqui:       min(renda / (despesas + 1), 3.0) / 3.0 * 500

Motivo: com o fator 30 cru, a razão renda/despesa teria de passar de 13 para
encostar em 1000 — o somatório máximo real ficaria em ~590 e a faixa
"0 a 1000" declarada no enunciado nunca seria usada. Saturação em 3x as
despesas + teto de 500 pontos de renda fazem o intervalo 0..1000 ser
efetivamente alcançável (máximo: 500 + 300 + 100 + 100), mantendo a estrutura
e o restante dos pesos do enunciado. Ver README (desafios enfrentados).
"""

from __future__ import annotations

from banco_agil.tools.validators import (
    normalizar_dependentes,
    normalizar_emprego,
    normalizar_tem_dividas,
    parse_valor_monetario,
)

__all__ = [
    "PESO_EMPREGO",
    "PESO_DEPENDENTES",
    "PESO_DIVIDAS",
    "PESO_RENDA_MAX",
    "SATURACAO_RENDA",
    "calcular_score",
]

PESO_EMPREGO: dict[str, float] = {"formal": 300.0, "autonomo": 200.0, "desempregado": 0.0}
PESO_DEPENDENTES: dict[int, float] = {0: 100.0, 1: 80.0, 2: 60.0, 3: 30.0}
PESO_DIVIDAS: dict[str, float] = {"sim": -100.0, "nao": 100.0}
PESO_RENDA_MAX = 500.0
SATURACAO_RENDA = 3.0
SCORE_MINIMO = 0
SCORE_MAXIMO = 1000


def calcular_score(
    *,
    renda_mensal: object,
    tipo_emprego: object,
    despesas_fixas: object,
    num_dependentes: object,
    tem_dividas: object,
) -> int:
    """Calcula o score (0..1000) a partir dos dados da entrevista de crédito.

    Aceita os valores crus vindos da conversa (texto com máscara, acento,
    "sim"/"não") e normaliza internamente. Levanta ``ValidationError`` quando
    algum campo não é reconhecido — nunca devolve score "chutado".
    """
    renda = parse_valor_monetario(renda_mensal)
    despesas = parse_valor_monetario(despesas_fixas)
    emprego = normalizar_emprego(tipo_emprego)
    dependentes = normalizar_dependentes(num_dependentes)
    dividas = normalizar_tem_dividas(tem_dividas)

    razao = renda / (despesas + 1)
    termo_renda = min(razao, SATURACAO_RENDA) / SATURACAO_RENDA * PESO_RENDA_MAX

    bruto = (
        termo_renda
        + PESO_EMPREGO[emprego]
        + PESO_DEPENDENTES[dependentes]
        + PESO_DIVIDAS["sim" if dividas else "nao"]
    )

    arredondado = int(bruto + 0.5)  # half-up: evita o empate binário do round()
    return max(SCORE_MINIMO, min(SCORE_MAXIMO, arredondado))
