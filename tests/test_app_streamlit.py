"""Teste headless da UI (Streamlit AppTest): as duas abas renderizam sem exceção.

Roda offline: força o provedor `fake`, então nenhuma chamada de rede acontece.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from banco_agil.domain.modelos import Cliente

streamlit_testing = pytest.importorskip("streamlit.testing.v1")
AppTest = streamlit_testing.AppTest

RAIZ_APP = Path(__file__).resolve().parents[1] / "app.py"


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AppTest:
    # Provedor de mentira: a UI renderiza inteira sem tocar em rede ou chave.
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("LLM_MODEL", "fake")
    monkeypatch.setenv("LLM_FALLBACK_1", "")
    monkeypatch.setenv("LLM_FALLBACK_2", "")

    # .env de mentira: divergente do real de propósito, para haver o que gravar.
    env_falso = tmp_path / ".env"
    env_falso.write_text(
        "LLM_PROVIDER=gemini\nGEMINI_API_KEY=chave-falsa-123456\n", encoding="utf-8"
    )
    monkeypatch.setenv("BANCO_AGIL_ENV", str(env_falso))

    execucao = AppTest.from_file(RAIZ_APP, default_timeout=60)
    execucao.run()
    return execucao


class TestInterface:
    def test_abre_sem_excecao(self, app: AppTest) -> None:
        assert not app.exception, [str(erro) for erro in app.exception]

    def test_tem_as_duas_abas(self, app: AppTest) -> None:
        titulos = [aba.label for aba in app.tabs]
        assert titulos == ["Atendimento", "Configurações"]

    def test_campo_de_conversa_existe(self, app: AppTest) -> None:
        assert app.chat_input

    def test_aba_de_configuracao_expoe_os_tres_slots(self, app: AppTest) -> None:
        rotulos = [expander.label for expander in app.expander]
        assert "Modelo primário" in rotulos
        assert "Modelo secundário" in rotulos
        assert "Modelo terciário" in rotulos

    def test_escolha_de_provedor_lista_os_suportados(self, app: AppTest) -> None:
        opcoes = app.selectbox[0].options
        assert "openrouter" in opcoes
        assert "gemini" in opcoes
        assert "groq" in opcoes

    def test_botao_de_gravar_comeca_desabilitado(self, app: AppTest) -> None:
        """Sem a confirmação marcada, não existe caminho para escrever no .env."""
        botoes = {botao.label: botao for botao in app.button}
        assert "Gravar no .env agora" in botoes
        assert botoes["Gravar no .env agora"].disabled

    def test_confirmacao_habilita_o_botao(self, app: AppTest) -> None:
        app.checkbox[-1].check().run()
        botoes = {botao.label: botao for botao in app.button}
        assert not botoes["Gravar no .env agora"].disabled

    def test_conversa_com_provedor_fake_responde(self, app: AppTest) -> None:
        app.chat_input[0].set_value("bom dia").run()

        assert not app.exception
        historico = app.session_state["historico"]
        assert len(historico) >= 3  # abertura + mensagem do cliente + resposta
        assert historico[-1][0] == "assistant"
        assert historico[-1][1].strip()

    def test_aba_de_configuracao_tem_os_campos_de_chave(self, app: AppTest) -> None:
        rotulos = [campo.label for campo in app.text_input]
        assert any("GEMINI_API_KEY" in rotulo for rotulo in rotulos)
        assert any("OPENROUTER_API_KEY" in rotulo for rotulo in rotulos)

    def test_preview_da_gravacao_mostra_a_mudanca(self, app: AppTest) -> None:
        """O `.env` de teste tem chaves faltando: o preview mostra o que será gravado."""
        textos = " ".join(str(bloco.value) for bloco in app.markdown)
        assert "vão ser sobrescritas em `.env`" in textos
        assert "→" in textos  # linha de diff: valor atual → valor novo


CPF_MARIA = "52998224725"


def _campo_de_cpf(app: AppTest) -> Any:
    return next(campo for campo in app.text_input if campo.label == "CPF")


def _botao(app: AppTest, rotulo: str) -> Any:
    return next(botao for botao in app.button if botao.label == rotulo)


class TestCampoDedicado:
    """O campo dedicado nasce da decisão determinística, não do texto do modelo."""

    def test_campo_de_cpf_aparece_quando_o_atendimento_espera_o_cpf(self, app: AppTest) -> None:
        assert not app.exception
        assert "CPF" in [campo.label for campo in app.text_input]
        assert "Confirmar CPF" in [botao.label for botao in app.button]

    def test_cpf_enviado_pelo_campo_entra_na_conversa(self, app: AppTest) -> None:
        antes = len(app.session_state["historico"])
        _campo_de_cpf(app).set_value("529.982.247-25")
        _botao(app, "Confirmar CPF").click().run()

        assert not app.exception
        historico = app.session_state["historico"]
        assert len(historico) == antes + 2
        assert historico[-2][0] == "user"
        assert historico[-2][1] == CPF_MARIA  # a UI normaliza antes de enviar
        assert historico[-1][0] == "assistant"

    def test_cpf_com_digito_invalido_e_barrado_na_tela(self, app: AppTest) -> None:
        antes = len(app.session_state["historico"])
        _campo_de_cpf(app).set_value("111.111.111-11")
        _botao(app, "Confirmar CPF").click().run()

        assert len(app.session_state["historico"]) == antes  # nada foi enviado
        assert app.error  # e a tela explica o porquê

    def test_texto_livre_continua_funcionando_com_o_campo_aberto(self, app: AppTest) -> None:
        """O campo é atalho, não porta fechada: quem digitar no chat tem que funcionar."""
        antes = len(app.session_state["historico"])
        app.chat_input[0].set_value("bom dia, meu cpf é 52998224725").run()

        assert not app.exception
        assert len(app.session_state["historico"]) == antes + 2


class TestTrocaDeCampoECartao:
    """Quando o estado avança, a tela troca: CPF → calendário → cartão do cliente."""

    def test_calendario_substitui_o_campo_de_cpf(self, app: AppTest) -> None:
        atendimento = app.session_state["atendimento"]
        atendimento.estado.cpf_informado = CPF_MARIA
        app.run()

        assert not app.exception
        assert "Data de nascimento" in [campo.label for campo in app.date_input]
        assert "Confirmar data" in [botao.label for botao in app.button]
        assert "CPF" not in [campo.label for campo in app.text_input]

    def test_data_enviada_pelo_calendario_entra_na_conversa(self, app: AppTest) -> None:
        app.session_state["atendimento"].estado.cpf_informado = CPF_MARIA
        app.run()
        antes = len(app.session_state["historico"])

        app.date_input[0].set_value(dt.date(1990, 5, 12))
        _botao(app, "Confirmar data").click().run()

        assert not app.exception
        historico = app.session_state["historico"]
        assert len(historico) == antes + 2
        assert historico[-2][1] == "12/05/1990"  # formato que o pipeline entende

    def test_cartao_do_cliente_so_depois_de_autenticado(self, app: AppTest) -> None:
        assert len(app.metric) == 0  # nada de limite/score antes de autenticar

        estado = app.session_state["atendimento"].estado
        estado.cliente = Cliente(
            cpf=CPF_MARIA,
            nome="Maria Souza",
            data_nascimento=dt.date(1990, 5, 12),
            limite_credito=3000.0,
            score=650,
        )
        estado.autenticado = True
        app.run()

        assert not app.exception
        assert "Limite atual" in [metrica.label for metrica in app.metric]
        assert "Score" in [metrica.label for metrica in app.metric]
        valores = " ".join(str(metrica.value) for metrica in app.metric)
        assert "3.000" in valores
        assert "650" in valores


class TestMascaraNoMarkdown:
    """A máscara do CPF tem `***`, que o Streamlit leria como ênfase: tem de sair literal."""

    def test_cartao_do_cliente_escapa_a_mascara(self, app: AppTest) -> None:
        estado = app.session_state["atendimento"].estado
        estado.cliente = Cliente(
            cpf=CPF_MARIA,
            nome="Maria Souza",
            data_nascimento=dt.date(1990, 5, 12),
            limite_credito=3000.0,
            score=650,
        )
        estado.autenticado = True
        app.run()

        assert not app.exception
        textos = " ".join(str(bloco.value) for bloco in app.markdown)
        assert "CPF 529.\\*\\*\\*.\\*\\*\\*-25" in textos

    def test_erro_de_cpf_invalido_escapa_a_mascara(self, app: AppTest) -> None:
        _campo_de_cpf(app).set_value("111.111.111-11")
        _botao(app, "Confirmar CPF").click().run()

        assert app.error
        textos = " ".join(str(bloco.value) for bloco in app.error)
        assert "111.\\*\\*\\*.\\*\\*\\*-11" in textos
