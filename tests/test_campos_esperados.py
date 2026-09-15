"""Testes do campo esperado e dos dados visíveis — decisão determinística.

O que se prova aqui: a interface sabe qual dado pedir sem ler a fala do modelo, e
nada sobre limite/score aparece antes da autenticação.
"""

from __future__ import annotations

import datetime as dt

import pytest

from banco_agil.agents.base import RegistroDeAgentes  # noqa: F401 - tipo do construtor
from banco_agil.agents.especificacoes import construir_registro
from banco_agil.domain.modelos import Cliente
from banco_agil.errors import ValidationError
from banco_agil.orchestrator.campos import (
    CampoEsperado,
    campo_esperado,
    dados_visiveis,
)
from banco_agil.orchestrator.estado import EstadoSessao
from banco_agil.tools.validators import validar_cpf

CPF_MARIA = "52998224725"


def _cliente(**campos: object) -> Cliente:
    padrao: dict[str, object] = {
        "cpf": CPF_MARIA,
        "nome": "Maria Souza",
        "data_nascimento": dt.date(1990, 5, 12),
        "limite_credito": 3000.0,
        "score": 650,
    }
    padrao.update(campos)
    return Cliente(**padrao)  # type: ignore[arg-type]


class TestCampoEsperado:
    def test_comeca_pedindo_cpf(self) -> None:
        assert campo_esperado(EstadoSessao()) is CampoEsperado.CPF

    def test_com_cpf_registrado_pede_a_data(self) -> None:
        estado = EstadoSessao()
        estado.cpf_informado = CPF_MARIA
        assert campo_esperado(estado) is CampoEsperado.DATA_NASCIMENTO

    def test_autenticado_nao_pede_nada(self) -> None:
        estado = EstadoSessao(autenticado=True, cliente=_cliente(), cpf_informado=CPF_MARIA)
        assert campo_esperado(estado) is None

    def test_encerrado_nao_pede_nada(self) -> None:
        estado = EstadoSessao(cpf_informado=CPF_MARIA, encerrado=True)
        assert campo_esperado(estado) is None

    def test_data_invalida_nao_muda_o_campo_pedido(self) -> None:
        """Errou a data: continua pedindo data, e a UI mostra o calendário de novo."""
        estado = EstadoSessao(cpf_informado=CPF_MARIA, tentativas_autenticacao=1)
        assert campo_esperado(estado) is CampoEsperado.DATA_NASCIMENTO


class TestDadosVisiveis:
    def test_nada_antes_de_autenticar(self) -> None:
        """Regra do enunciado: nada de limite/score antes de confirmar quem é o cliente."""
        assert dados_visiveis(EstadoSessao()) is None
        assert dados_visiveis(EstadoSessao(cpf_informado=CPF_MARIA)) is None

    def test_cliente_sem_autenticacao_declarada_nao_vaza(self) -> None:
        """Estado inconsistente (cliente preenchido, autenticado=False) não vaza dado."""
        estado = EstadoSessao(cliente=_cliente())
        assert dados_visiveis(estado) is None

    def test_depois_de_autenticar_mostra_nome_limite_e_faixa(self) -> None:
        estado = EstadoSessao(autenticado=True, cliente=_cliente())
        dados = dados_visiveis(estado)
        assert dados is not None
        assert dados.nome == "Maria Souza"
        assert dados.primeiro_nome == "Maria"
        assert dados.score == 650
        assert "3.000" in dados.limite
        assert dados.faixa
        # CPF nunca aparece inteiro na interface
        assert dados.cpf_mascarado != CPF_MARIA
        assert CPF_MARIA not in dados.como_dicionario()["cpf"]

    def test_dicionario_para_a_ui_tem_as_chaves_esperadas(self) -> None:
        estado = EstadoSessao(autenticado=True, cliente=_cliente())
        dados = dados_visiveis(estado)
        assert dados is not None
        assert set(dados.como_dicionario()) == {
            "nome",
            "primeiro_nome",
            "cpf",
            "limite",
            "score",
            "faixa",
        }


class TestFerramentaRegistrarCpf:
    """`registrar_cpf` é o que habilita o campo dedicado na interface."""

    @pytest.fixture
    def ferramentas(self) -> tuple[EstadoSessao, dict[str, object]]:
        from banco_agil.agents.ferramentas import construir_ferramentas
        from banco_agil.config import Settings
        from banco_agil.servicos import construir_servicos

        estado = EstadoSessao()
        settings = Settings(_env_file=None, llm_provider="fake")
        servicos = construir_servicos(settings)
        registro = construir_registro()
        return estado, construir_ferramentas(estado, servicos, registro)

    def test_registra_cpf_valido_e_avisa_que_falta_a_data(
        self, ferramentas: tuple[EstadoSessao, dict[str, object]]
    ) -> None:
        estado, mapa = ferramentas
        saida = mapa["registrar_cpf"].invoke({"cpf": "529.982.247-25"})  # type: ignore[union-attr]

        assert estado.cpf_informado == CPF_MARIA
        assert "data de nascimento" in saida.lower()
        assert campo_esperado(estado) is CampoEsperado.DATA_NASCIMENTO

    def test_cpf_com_digito_invalido_nao_registra(
        self, ferramentas: tuple[EstadoSessao, dict[str, object]]
    ) -> None:
        estado, mapa = ferramentas
        saida = mapa["registrar_cpf"].invoke({"cpf": "111.111.111-11"})  # type: ignore[union-attr]

        assert estado.cpf_informado is None
        assert "FALHA_DADOS" in saida
        assert estado.tentativas_autenticacao == 0  # não consumiu tentativa
        assert campo_esperado(estado) is CampoEsperado.CPF

    def test_autenticacao_sem_registrar_cpf_ainda_marca_o_cpf(
        self, ferramentas: tuple[EstadoSessao, dict[str, object]]
    ) -> None:
        """Rede de proteção: se o modelo autenticar direto, o campo também é marcado."""
        estado, mapa = ferramentas
        mapa["autenticar_cliente"].invoke(  # type: ignore[union-attr]
            {"cpf": CPF_MARIA, "data_nascimento": "12/05/1990"}
        )

        assert estado.cpf_informado == CPF_MARIA
        assert estado.autenticado is True
        assert campo_esperado(estado) is None


class TestValidacaoDoCampoNaUi:
    """A mesma validação que a UI usa antes de enviar o CPF."""

    @pytest.mark.parametrize("valor", ["111.111.111-11", "123", "", "abc"])
    def test_cpf_invalido_e_barrado_antes_de_enviar(self, valor: str) -> None:
        with pytest.raises(ValidationError):
            validar_cpf(valor)

    @pytest.mark.parametrize("valor", ["52998224725", "529.982.247-25"])
    def test_cpf_valido_passa(self, valor: str) -> None:
        assert validar_cpf(valor) == CPF_MARIA
