"""Testes da cadeia de modelos (fallback entre provedores).

Tudo offline: os modelos são dublês que falham sob encomenda.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from banco_agil.config import Settings
from banco_agil.errors import LLMProviderError
from banco_agil.llm.corrente import (
    CorrenteDeModelos,
    PassoDaCadeia,
    e_falha_de_infra,
    e_falha_transitoria,
    montar_cadeia,
)
from banco_agil.llm.factory import construir_cadeia, passos_da_cadeia

PASSO_1 = PassoDaCadeia(provedor="gemini", modelo="gemini-3.6-flash")
PASSO_2 = PassoDaCadeia(provedor="openrouter", modelo="google/gemma-4-26b-a4b-it:free")


def _settings(**campos: object) -> Settings:
    """`Settings` isolado: sem ler o `.env` do projeto."""
    return Settings(_env_file=None, **campos)  # type: ignore[arg-type]


class ModeloDuble(BaseChatModel):
    """Modelo que responde um texto fixo — ou levanta o erro programado.

    ``erro`` falha sempre; ``erros`` é uma fila: cada chamada consome um erro e, quando
    a fila esvazia, o modelo responde (é assim que se testa um soluço passageiro).
    """

    resposta: str = "ok"
    erro: Any = None
    erros: list[Any] = []
    ferramentas_recebidas: list[list[str]] = []
    chamadas: int = 0

    @property
    def _llm_type(self) -> str:
        return "duble"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.chamadas += 1
        if self.erros:
            raise self.erros.pop(0)
        if self.erro is not None:
            raise self.erro
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.resposta))])

    def bind_tools(self, tools: Any, **kwargs: Any) -> ModeloDuble:  # type: ignore[override]
        nomes = [getattr(ferramenta, "name", str(ferramenta)) for ferramenta in tools]
        self.ferramentas_recebidas.append(nomes)
        return self


def erro_com_status(codigo: int) -> Exception:
    """Exceção genérica com ``status_code`` (como as bibliotecas de provedor)."""
    erro = Exception(f"HTTP {codigo}")
    erro.status_code = codigo  # type: ignore[attr-defined]
    return erro


class TestClassificacaoDeFalha:
    @pytest.mark.parametrize(
        "erro",
        [
            erro_com_status(429),
            erro_com_status(503),
            erro_com_status(404),
            erro_com_status(401),
            httpx.ConnectError("sem rota"),
            httpx.ReadTimeout("demorou"),
            TimeoutError("demorou"),
        ],
    )
    def test_falha_de_infraestrutura_pede_fallback(self, erro: Exception) -> None:
        assert e_falha_de_infra(erro)

    @pytest.mark.parametrize(
        "nome",
        ["GoogleRateLimitError", "OpenAIRateLimitError", "GoogleModelNotFoundError"],
    )
    def test_nome_da_excecao_do_provedor_conta(self, nome: str) -> None:
        erro = type(nome, (Exception,), {})("estourou")
        assert e_falha_de_infra(erro)

    def test_mensagem_de_quota_conta(self) -> None:
        assert e_falha_de_infra(Exception("RESOURCE_EXHAUSTED: you exceeded your quota"))

    @pytest.mark.parametrize("codigo", [400, 422])
    def test_falha_de_contrato_nao_troca_de_modelo(self, codigo: int) -> None:
        """400/422 é bug nosso (schema, argumento): trocar de modelo esconderia."""
        assert not e_falha_de_infra(erro_com_status(codigo))

    def test_erro_qualquer_nao_troca(self) -> None:
        assert not e_falha_de_infra(ValueError("argumento inválido"))


class TestCorrente:
    def test_primario_respondendo_nao_troca(self) -> None:
        corrente = CorrenteDeModelos(
            [ModeloDuble(resposta="do primário"), ModeloDuble(resposta="do secundário")],
            [PASSO_1, PASSO_2],
        )
        assert corrente.invoke([]).content == "do primário"
        assert corrente.trocas == 0
        assert corrente.rotulo_atual == "gemini:gemini-3.6-flash"
        assert corrente.falhas == []

    def test_quota_estourada_no_primario_cai_para_o_secundario(self) -> None:
        trocas: list[tuple[int, str]] = []
        corrente = CorrenteDeModelos(
            [ModeloDuble(erro=erro_com_status(429)), ModeloDuble(resposta="do secundário")],
            [PASSO_1, PASSO_2],
            ao_trocar=lambda indice, passo, _erro: trocas.append((indice, passo.rotulo)),
        )

        assert corrente.invoke([]).content == "do secundário"
        assert corrente.trocas == 1
        assert corrente.rotulo_atual == PASSO_2.rotulo
        assert trocas == [(0, PASSO_1.rotulo)]
        assert "HTTP 429" in corrente.falhas[0][1]

    def test_modelo_descontinuado_no_primario_cai_para_o_secundario(self) -> None:
        """Caso real: gemini-2.5-flash devolvendo 404 NOT_FOUND."""
        corrente = CorrenteDeModelos(
            [ModeloDuble(erro=erro_com_status(404)), ModeloDuble(resposta="segundo")],
            [PASSO_1, PASSO_2],
        )
        assert corrente.invoke([]).content == "segundo"

    def test_erro_de_contrato_propaga_sem_trocar(self) -> None:
        secundario = ModeloDuble(resposta="não deveria responder")
        corrente = CorrenteDeModelos(
            [ModeloDuble(erro=erro_com_status(400)), secundario], [PASSO_1, PASSO_2]
        )

        with pytest.raises(Exception, match="HTTP 400"):
            corrente.invoke([])
        assert secundario.erro is None  # nem foi chamado

    def test_todos_falhando_vira_erro_de_provedor(self) -> None:
        corrente = CorrenteDeModelos(
            [ModeloDuble(erro=erro_com_status(429)), ModeloDuble(erro=erro_com_status(503))],
            [PASSO_1, PASSO_2],
        )

        with pytest.raises(LLMProviderError) as capturado:
            corrente.invoke([])
        assert "gemini:gemini-3.6-flash" in str(capturado.value)
        assert "openrouter:google/gemma-4-26b-a4b-it:free" in str(capturado.value)
        assert len(corrente.falhas) == 2

    def test_bind_tools_alcanca_todos_os_modelos_da_cadeia(self) -> None:
        """Sem isso o secundário assumiria sem ferramenta e o agente ficaria mudo."""
        primario = ModeloDuble(resposta="p")
        secundario = ModeloDuble(resposta="s")
        corrente = CorrenteDeModelos([primario, secundario], [PASSO_1, PASSO_2])

        corrente_com_ferramentas = corrente.bind_tools([_FerramentaFalsa("consultar_limite")])
        corrente_com_ferramentas.invoke([])

        assert primario.ferramentas_recebidas == [["consultar_limite"]]
        assert secundario.ferramentas_recebidas == [["consultar_limite"]]

    def test_apos_fallback_a_cadeia_continua_no_secundario(self) -> None:
        """Já sabemos que o primário está fora: não insistimos a cada turno."""
        primario = ModeloDuble(erro=erro_com_status(429))
        secundario = ModeloDuble(resposta="s")
        corrente = CorrenteDeModelos([primario, secundario], [PASSO_1, PASSO_2])

        corrente.invoke([])
        corrente.invoke([])

        assert corrente.trocas == 1
        assert corrente.rotulo_atual == PASSO_2.rotulo

    def test_contador_sobrevive_ao_bind_tools_de_cada_turno(self) -> None:
        """O orquestrador re-binda as ferramentas a cada turno: o estado é o mesmo."""
        corrente = CorrenteDeModelos(
            [ModeloDuble(erro=erro_com_status(429)), ModeloDuble(resposta="s")],
            [PASSO_1, PASSO_2],
        )

        primeiro_turno = corrente.bind_tools([_FerramentaFalsa("consultar_limite")])
        primeiro_turno.invoke([])
        segundo_turno = primeiro_turno.bind_tools([_FerramentaFalsa("consultar_limite")])
        segundo_turno.invoke([])

        assert corrente.trocas == 1
        assert primeiro_turno.trocas == 1
        assert segundo_turno.trocas == 1
        assert len(corrente.falhas) == 1

    def test_cadeia_vazia_levanta(self) -> None:
        with pytest.raises(LLMProviderError):
            CorrenteDeModelos([], [])

    def test_modelos_e_passos_de_tamanhos_diferentes_levanta(self) -> None:
        with pytest.raises(LLMProviderError):
            CorrenteDeModelos([ModeloDuble()], [PASSO_1, PASSO_2])


class TestMontarCadeia:
    def test_um_slot_devolve_o_modelo_puro(self) -> None:
        modelo = ModeloDuble()
        assert montar_cadeia([modelo], [PASSO_1]) is modelo

    def test_dois_slots_devolvem_a_corrente(self) -> None:
        resultado = montar_cadeia([ModeloDuble(), ModeloDuble()], [PASSO_1, PASSO_2])
        assert isinstance(resultado, CorrenteDeModelos)


class TestCadeiaDaConfiguracao:
    """A cadeia vem do `.env`: primário + slots de fallback."""

    def test_so_primario_quando_nao_ha_fallback(self) -> None:
        settings = _settings(llm_provider="gemini", llm_model="gemini-3.6-flash")
        passos = passos_da_cadeia(settings)
        assert [passo.rotulo for passo in passos] == ["gemini:gemini-3.6-flash"]

    def test_le_os_dois_slots_de_fallback(self) -> None:
        settings = _settings(
            llm_provider="gemini",
            llm_model="gemini-3.6-flash",
            llm_fallback_1="openrouter:google/gemma-4-26b-a4b-it:free",
            llm_fallback_2="groq:llama-3.3-70b-versatile",
        )
        assert [passo.rotulo for passo in passos_da_cadeia(settings)] == [
            "gemini:gemini-3.6-flash",
            "openrouter:google/gemma-4-26b-a4b-it:free",
            "groq:llama-3.3-70b-versatile",
        ]

    def test_slot_vazio_e_ignorado(self) -> None:
        settings = _settings(
            llm_provider="groq",
            llm_model="llama-3.3-70b-versatile",
            llm_fallback_1="   ",
            llm_fallback_2="openai:gpt-4o-mini",
        )
        assert len(passos_da_cadeia(settings)) == 2

    def test_um_slot_devolve_modelo_simples(self) -> None:
        settings = _settings(llm_provider="gemini", gemini_api_key="chave-falsa")
        modelo = construir_cadeia(settings, passos=[PassoDaCadeia("gemini", "gemini-3.6-flash")])
        assert not isinstance(modelo, CorrenteDeModelos)

    def test_dois_slots_devolvem_corrente(self) -> None:
        settings = _settings(
            llm_provider="gemini", gemini_api_key="chave-falsa", openrouter_api_key="outra-falsa"
        )
        modelo = construir_cadeia(
            settings,
            passos=[
                PassoDaCadeia("gemini", "gemini-3.6-flash"),
                PassoDaCadeia("openrouter", "google/gemma-x"),
            ],
        )
        assert isinstance(modelo, CorrenteDeModelos)
        assert [passo.provedor for passo in modelo.passos] == ["gemini", "openrouter"]


class TestPassoDaCadeia:
    def test_tentativas_por_modelo_vem_da_configuracao(self) -> None:
        settings = _settings(
            llm_provider="gemini",
            gemini_api_key="chave-falsa",
            openrouter_api_key="outra-falsa",
            llm_tentativas_por_modelo=1,
        )
        modelo = construir_cadeia(
            settings,
            passos=[
                PassoDaCadeia("gemini", "gemini-3.6-flash"),
                PassoDaCadeia("openrouter", "google/gemma-x"),
            ],
        )

        assert isinstance(modelo, CorrenteDeModelos)
        assert modelo.tentativas_por_modelo == 1

    def test_le_modelo_com_barra_e_dois_pontos(self) -> None:
        passo = PassoDaCadeia.de_texto("openrouter:google/gemma-4-26b-a4b-it:free")
        assert passo.provedor == "openrouter"
        assert passo.modelo == "google/gemma-4-26b-a4b-it:free"
        assert passo.para_texto() == "openrouter:google/gemma-4-26b-a4b-it:free"

    @pytest.mark.parametrize("texto", ["", "gemini", ":modelo", "provedor:"])
    def test_texto_invalido_levanta(self, texto: str) -> None:
        with pytest.raises(LLMProviderError):
            PassoDaCadeia.de_texto(texto)


class TestErroDeProvedorEmbrulhado:
    """Erros reais que chegam dentro de um ValueError genérico (visto ao vivo).

    O OpenRouter devolveu 502 `Upstream error ... Service temporarily overloaded` e o
    LangChain levantou um ValueError com o payload em texto. Se isso passar por "erro de
    contrato", o fallback não dispara e o cliente recebe "tive um problema".
    """

    ERRO_502_REAL = ValueError(
        "{'message': 'Upstream error from Nvidia: Service temporarily overloaded', "
        "'code': 502, 'metadata': {'error_type': 'provider_unavailable'}}"
    )

    def test_502_embrulhado_e_falha_de_infra(self) -> None:
        assert e_falha_de_infra(self.ERRO_502_REAL)

    def test_502_embrulhado_e_transitorio(self) -> None:
        assert e_falha_transitoria(self.ERRO_502_REAL)

    def test_400_embrulhado_continua_sendo_contrato(self) -> None:
        erro = ValueError("{'message': 'invalid request', 'code': 400}")
        assert not e_falha_de_infra(erro)

    def test_422_embrulhado_continua_sendo_contrato(self) -> None:
        erro = ValueError("{'message': 'unprocessable', 'status': 422}")
        assert not e_falha_de_infra(erro)

    def test_400_com_status_code_nao_troca_de_modelo(self) -> None:
        assert not e_falha_de_infra(erro_com_status(400))

    def test_cadeia_sobrevive_ao_502_do_provedor(self) -> None:
        """Cenário ao vivo: primário em 502, secundário saudável."""
        primario = ModeloDuble(erro=self.ERRO_502_REAL)
        secundario = ModeloDuble(resposta="do secundário")
        corrente = montar_cadeia([primario, secundario], [PASSO_1, PASSO_2])

        resposta = corrente.invoke("oi")

        assert resposta.content == "do secundário"
        assert isinstance(corrente, CorrenteDeModelos)
        assert corrente.trocas == 1


class TestSoluçoPassageiro:
    """5xx e timeout valem nova tentativa no mesmo modelo; quota não."""

    def test_503_absorvido_sem_trocar_de_modelo(self) -> None:
        primario = ModeloDuble(erros=[erro_com_status(503)], resposta="do primário")
        secundario = ModeloDuble(resposta="do secundário")
        corrente = montar_cadeia([primario, secundario], [PASSO_1, PASSO_2])

        resposta = corrente.invoke("oi")

        assert resposta.content == "do primário"
        assert primario.chamadas == 2  # tentou de novo antes de trocar
        assert secundario.chamadas == 0
        assert isinstance(corrente, CorrenteDeModelos)
        assert corrente.trocas == 0  # não houve troca de modelo
        assert corrente.falhas == []  # soluço absorvido não vira falha registrada

    def test_timeout_absorvido(self) -> None:
        primario = ModeloDuble(erros=[httpx.ReadTimeout("demorou")], resposta="do primário")
        corrente = montar_cadeia([primario, ModeloDuble()], [PASSO_1, PASSO_2])

        assert corrente.invoke("oi").content == "do primário"

    def test_quota_nao_repete_a_chamada_no_mesmo_modelo(self) -> None:
        """Repetir uma chamada sem quota é só gastar latência."""
        primario = ModeloDuble(erro=erro_com_status(429))
        secundario = ModeloDuble(resposta="do secundário")
        corrente = montar_cadeia([primario, secundario], [PASSO_1, PASSO_2])

        assert corrente.invoke("oi").content == "do secundário"
        assert primario.chamadas == 1

    def test_erro_de_contrato_nao_tenta_de_novo(self) -> None:
        primario = ModeloDuble(erro=erro_com_status(400))
        corrente = montar_cadeia([primario, ModeloDuble()], [PASSO_1, PASSO_2])

        with pytest.raises(Exception, match="HTTP 400"):
            corrente.invoke("oi")
        assert primario.chamadas == 1


class _FerramentaFalsa:
    def __init__(self, nome: str) -> None:
        self.name = nome
