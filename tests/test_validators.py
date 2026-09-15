"""Testes dos validadores de entrada (CPF, data, dinheiro, enumerações).

Tudo roda offline: validadores são função pura, sem LLM e sem rede.
"""

from __future__ import annotations

import datetime as dt

import pytest

from banco_agil.errors import ValidationError
from banco_agil.tools.validators import (
    mascarar_cpf,
    normalizar_cpf,
    normalizar_dependentes,
    normalizar_emprego,
    normalizar_tem_dividas,
    parse_data_nascimento,
    parse_valor_monetario,
    validar_cpf,
)

CPF_VALIDO = "52998224725"
CPF_VALIDO_FORMATADO = "529.982.247-25"


class TestNormalizarCpf:
    def test_remove_mascara(self) -> None:
        assert normalizar_cpf(CPF_VALIDO_FORMATADO) == CPF_VALIDO

    def test_aceita_pontuacao_variada(self) -> None:
        assert normalizar_cpf(" 529 982 247 25 ") == CPF_VALIDO


class TestValidarCpf:
    def test_aceita_cpf_valido(self) -> None:
        assert validar_cpf(CPF_VALIDO) == CPF_VALIDO

    def test_aceita_cpf_valido_formatado(self) -> None:
        assert validar_cpf(CPF_VALIDO_FORMATADO) == CPF_VALIDO

    def test_rejeita_tamanho_errado(self) -> None:
        with pytest.raises(ValidationError):
            validar_cpf("5299822472")

    def test_rejeita_digito_verificador_errado(self) -> None:
        with pytest.raises(ValidationError):
            validar_cpf("52998224724")

    def test_rejeita_todos_digitos_iguais(self) -> None:
        for cpf in ("00000000000", "11111111111", "99999999999"):
            with pytest.raises(ValidationError):
                validar_cpf(cpf)

    def test_rejeita_texto_sem_digitos(self) -> None:
        with pytest.raises(ValidationError):
            validar_cpf("nao tenho cpf")

    def test_mensagem_nao_vaza_o_documento_completo(self) -> None:
        with pytest.raises(ValidationError) as exc:
            validar_cpf("52998224724")
        assert CPF_VALIDO not in str(exc.value)


class TestMascararCpf:
    def test_mascara_meio_do_documento(self) -> None:
        assert mascarar_cpf(CPF_VALIDO) == "529.***.***-25"
        assert mascarar_cpf(CPF_VALIDO_FORMATADO) == "529.***.***-25"

    def test_entrada_curta_nao_quebra(self) -> None:
        assert mascarar_cpf("123") == "***"


class TestParseDataNascimento:
    @pytest.mark.parametrize(
        "entrada",
        ["12/05/1990", "12-05-1990", "1990-05-12", "12.05.1990", " 12/05/1990 "],
    )
    def test_aceita_formatos_comuns(self, entrada: str) -> None:
        assert parse_data_nascimento(entrada) == dt.date(1990, 5, 12)

    def test_rejeita_data_inexistente(self) -> None:
        with pytest.raises(ValidationError):
            parse_data_nascimento("31/02/1990")

    def test_rejeita_data_futura(self) -> None:
        with pytest.raises(ValidationError):
            parse_data_nascimento("01/01/2999")

    def test_rejeita_texto_livre(self) -> None:
        with pytest.raises(ValidationError):
            parse_data_nascimento("ontem")


class TestParseValorMonetario:
    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            ("8000", 8000.0),
            ("8.000", 8000.0),
            ("8.000,50", 8000.5),
            ("R$ 8.000,50", 8000.5),
            ("8000.50", 8000.5),
            ("8,5", 8.5),
            ("1.234.567,89", 1234567.89),
            ("0", 0.0),
        ],
    )
    def test_interpreta_formatos_br_e_us(self, entrada: str, esperado: float) -> None:
        assert parse_valor_monetario(entrada) == pytest.approx(esperado)

    def test_aceita_numero_puro(self) -> None:
        assert parse_valor_monetario(1500) == pytest.approx(1500.0)

    def test_rejeita_negativo(self) -> None:
        with pytest.raises(ValidationError):
            parse_valor_monetario("-100")

    def test_rejeita_texto(self) -> None:
        with pytest.raises(ValidationError):
            parse_valor_monetario("um pouco mais de oito mil")


class TestNormalizacaoDeEnums:
    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            ("formal", "formal"),
            ("CLT", "formal"),
            ("carteira assinada", "formal"),
            ("autônomo", "autonomo"),
            ("autonomo", "autonomo"),
            ("Autônoma", "autonomo"),
            ("MEI", "autonomo"),
            ("freelancer", "autonomo"),
            ("desempregado", "desempregado"),
            ("sem emprego", "desempregado"),
            ("estou desempregada", "desempregado"),
        ],
    )
    def test_emprego(self, entrada: str, esperado: str) -> None:
        assert normalizar_emprego(entrada) == esperado

    def test_emprego_desconhecido_levanta(self) -> None:
        with pytest.raises(ValidationError):
            normalizar_emprego("astronauta")

    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            (0, 0),
            ("0", 0),
            ("nenhum", 0),
            (2, 2),
            ("dois", 2),
            ("2 dependentes", 2),
            ("3+", 3),
            ("4", 3),
            (7, 3),
        ],
    )
    def test_dependentes_saturam_em_tres(self, entrada: object, esperado: int) -> None:
        assert normalizar_dependentes(entrada) == esperado

    def test_dependentes_invalido_levanta(self) -> None:
        with pytest.raises(ValidationError):
            normalizar_dependentes("muitos")

    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            (True, True),
            (False, False),
            ("sim", True),
            ("tenho dívidas", True),
            ("não", False),
            ("nao", False),
            ("não tenho", False),
        ],
    )
    def test_dividas(self, entrada: object, esperado: bool) -> None:
        assert normalizar_tem_dividas(entrada) is esperado

    def test_dividas_invalido_levanta(self) -> None:
        with pytest.raises(ValidationError):
            normalizar_tem_dividas("talvez")
