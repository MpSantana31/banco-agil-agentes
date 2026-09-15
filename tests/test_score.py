"""Testes do motor de score (função pura, sem LLM).

Os pesos são os sugeridos no PDF do desafio; o termo de renda é normalizado
(saturação em 3x as despesas) para que a faixa declarada de 0 a 1000 seja
realmente alcançável — ver README, seção "desafios enfrentados".
"""

from __future__ import annotations

import pytest

from banco_agil.errors import ValidationError
from banco_agil.tools.score import (
    PESO_DEPENDENTES,
    PESO_DIVIDAS,
    PESO_EMPREGO,
    PESO_RENDA_MAX,
    SATURACAO_RENDA,
    calcular_score,
)


def test_pesos_espelham_o_pdf() -> None:
    assert PESO_EMPREGO == {"formal": 300, "autonomo": 200, "desempregado": 0}
    assert PESO_DEPENDENTES == {0: 100, 1: 80, 2: 60, 3: 30}
    assert PESO_DIVIDAS == {"sim": -100, "nao": 100}
    assert (PESO_RENDA_MAX, SATURACAO_RENDA) == (500.0, 3.0)


class TestCalculoBasico:
    def test_cliente_solido_atinge_o_teto(self) -> None:
        # razao 8x (satura em 3x) + formal + sem dependentes + sem dividas
        assert (
            calcular_score(
                renda_mensal=8000,
                tipo_emprego="formal",
                despesas_fixas=1000,
                num_dependentes=0,
                tem_dividas=False,
            )
            == 1000
        )

    def test_divida_desconta_duzentos(self) -> None:
        kwargs = {
            "renda_mensal": 8000,
            "tipo_emprego": "formal",
            "despesas_fixas": 1000,
            "num_dependentes": 0,
        }
        sem_divida = calcular_score(**kwargs, tem_dividas=False)
        com_divida = calcular_score(**kwargs, tem_dividas=True)
        assert sem_divida - com_divida == 200

    def test_renda_satura_em_tres_vezes_as_despesas(self) -> None:
        # 30.000 de renda nao vale mais que 3.000 com 1.000 de despesa
        caro = calcular_score(
            renda_mensal=30000,
            tipo_emprego="formal",
            despesas_fixas=1000,
            num_dependentes=0,
            tem_dividas=False,
        )
        limite = calcular_score(
            renda_mensal=3000,
            tipo_emprego="formal",
            despesas_fixas=1000,
            num_dependentes=0,
            tem_dividas=False,
        )
        assert caro == limite == 1000

    def test_termo_de_renda_e_proporcional_ate_saturar(self) -> None:
        # razao 1.0 (metade da saturacao) => metade dos 500 pontos de renda
        score = calcular_score(
            renda_mensal=2000,
            tipo_emprego="desempregado",  # peso 0
            despesas_fixas=1000,  # razao = 2.0 -> 2/3 de 500 = 333.33
            num_dependentes=0,  # +100
            tem_dividas=False,  # +100
        )
        assert score == 533


class TestClampEFaixas:
    def test_pior_cenario_zera_sem_estourar(self) -> None:
        score = calcular_score(
            renda_mensal=0,
            tipo_emprego="desempregado",
            despesas_fixas=1000,
            num_dependentes=3,
            tem_dividas=True,
        )
        assert score == 0

    def test_melhor_cenario_nao_passa_de_mil(self) -> None:
        score = calcular_score(
            renda_mensal=1_000_000,
            tipo_emprego="formal",
            despesas_fixas=0,
            num_dependentes=0,
            tem_dividas=False,
        )
        assert score == 1000

    @pytest.mark.parametrize(
        ("renda", "despesas", "emprego", "dependentes", "dividas", "esperado"),
        [
            (8000, 1000, "formal", 0, False, 1000),
            (8000, 1000, "formal", 0, True, 800),
            (3000, 3000, "formal", 2, True, 427),
            (2000, 1000, "autonomo", 1, False, 713),
            (0, 1000, "desempregado", 3, True, 0),
        ],
    )
    def test_tabela_de_exemplos_do_plano(
        self,
        renda: float,
        despesas: float,
        emprego: str,
        dependentes: int,
        dividas: bool,
        esperado: int,
    ) -> None:
        assert (
            calcular_score(
                renda_mensal=renda,
                tipo_emprego=emprego,
                despesas_fixas=despesas,
                num_dependentes=dependentes,
                tem_dividas=dividas,
            )
            == esperado
        )


class TestEntradasFlexiveis:
    def test_aceita_texto_do_cliente(self) -> None:
        """A LLM repassa texto cru; o motor normaliza antes de calcular."""
        score = calcular_score(
            renda_mensal="R$ 8.000",
            tipo_emprego="carteira assinada",
            despesas_fixas="1.000,00",
            num_dependentes="2",
            tem_dividas="não",
        )
        assert score == calcular_score(
            renda_mensal=8000,
            tipo_emprego="formal",
            despesas_fixas=1000,
            num_dependentes=2,
            tem_dividas=False,
        )

    def test_emprego_invalido_levanta(self) -> None:
        with pytest.raises(ValidationError):
            calcular_score(
                renda_mensal=1000,
                tipo_emprego="aposentado",
                despesas_fixas=500,
                num_dependentes=0,
                tem_dividas=False,
            )

    def test_resultado_e_sempre_inteiro(self) -> None:
        score = calcular_score(
            renda_mensal=1234.56,
            tipo_emprego="autonomo",
            despesas_fixas=789.1,
            num_dependentes=1,
            tem_dividas=True,
        )
        assert isinstance(score, int)
