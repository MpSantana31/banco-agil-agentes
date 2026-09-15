"""Testes do catálogo de modelos e do teste de conexão por slot."""

from __future__ import annotations

import httpx
import pytest
import respx

from banco_agil.config import Settings
from banco_agil.llm.catalogo import (
    ORIGEM_API,
    ORIGEM_CURADA,
    ResultadoDoTeste,
    listar_modelos,
)
from banco_agil.llm.catalogo import testar_passo as aferir_slot
from banco_agil.llm.corrente import PassoDaCadeia

OPENROUTER = "https://openrouter.ai/api/v1/models"
GROQ = "https://api.groq.com/openai/v1/models"
GEMINI = "https://generativelanguage.googleapis.com/v1beta/models"


def _settings(**campos: object) -> Settings:
    return Settings(_env_file=None, **campos)  # type: ignore[arg-type]


class TestListagemDeModelos:
    @respx.mock
    def test_openrouter_devolve_a_lista_real(self) -> None:
        respx.get(OPENROUTER).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "google/gemma-4-26b-a4b-it:free"},
                        {"id": "nvidia/nemotron-3-super-120b-a12b:free"},
                    ]
                },
            )
        )
        modelos, origem = listar_modelos("openrouter", _settings(openrouter_api_key="chave-falsa"))

        assert origem == ORIGEM_API
        assert modelos == [
            "google/gemma-4-26b-a4b-it:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
        ]

    @respx.mock
    def test_groq_manda_a_chave_no_cabecalho(self) -> None:
        rota = respx.get(GROQ).mock(
            return_value=httpx.Response(200, json={"data": [{"id": "llama-3.3-70b-versatile"}]})
        )
        listar_modelos("groq", _settings(groq_api_key="chave-groq-falsa"))

        assert rota.calls[0].request.headers["authorization"] == "Bearer chave-groq-falsa"

    @respx.mock
    def test_gemini_tira_o_prefixo_models_e_o_que_nao_gera_texto(self) -> None:
        respx.get(GEMINI).mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-3.6-flash",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            "name": "models/embedding-001",
                            "supportedGenerationMethods": ["embedContent"],
                        },
                    ]
                },
            )
        )
        modelos, origem = listar_modelos("gemini", _settings(gemini_api_key="chave-falsa"))

        assert origem == ORIGEM_API
        assert modelos == ["gemini-3.6-flash"]

    @respx.mock
    def test_api_fora_do_ar_cai_na_lista_curada(self) -> None:
        respx.get(OPENROUTER).mock(return_value=httpx.Response(500))
        modelos, origem = listar_modelos("openrouter", _settings(openrouter_api_key="chave-falsa"))

        assert origem == ORIGEM_CURADA
        assert "google/gemma-4-26b-a4b-it:free" in modelos

    def test_erro_de_rede_cai_na_lista_curada(self) -> None:
        with respx.mock:
            respx.get(GROQ).mock(side_effect=httpx.ConnectError("sem rota"))
            _modelos, origem = listar_modelos("groq", _settings(groq_api_key="x"))

        assert origem == ORIGEM_CURADA

    def test_fake_nao_bate_na_rede(self) -> None:
        modelos, origem = listar_modelos("fake", _settings())
        assert (modelos, origem) == (["fake"], ORIGEM_CURADA)


class TestTesteDeConexao:
    def test_slot_ok_sem_rede_com_provedor_fake(self) -> None:
        """O provedor `fake` exercita o botão sem rede: prova ok=True e a latência."""
        resultado = aferir_slot(PassoDaCadeia("fake", "fake"), _settings())

        assert resultado.ok
        assert "credencial válidas" in resultado.detalhe
        assert resultado.descricao().startswith("fake:fake: ok")
        assert resultado.milissegundos >= 0

    def test_quota_estourada_e_reportada_como_tal(self) -> None:
        from banco_agil.llm.catalogo import _classificar_falha

        erro = Exception("Error code: 429 - {'error': {'message': 'You exceeded your quota'}}")
        erro.status_code = 429  # type: ignore[attr-defined]

        detalhe = _classificar_falha(erro)
        assert "quota" in detalhe
        assert "chave-falsa" not in detalhe  # nunca ecoa credencial

    def test_modelo_descontinuado_e_reportado_como_tal(self) -> None:
        from banco_agil.llm.catalogo import _classificar_falha

        class GoogleModelNotFoundError(Exception):
            status_code = 404

        assert "inexistente" in _classificar_falha(GoogleModelNotFoundError("404 NOT_FOUND"))

    def test_chave_ausente_nao_derruba_a_tela(self) -> None:
        resultado = aferir_slot(PassoDaCadeia("gemini", "gemini-3.6-flash"), _settings())

        assert not resultado.ok
        assert isinstance(resultado, ResultadoDoTeste)
        assert "chave" in resultado.detalhe.lower() or "credencial" in resultado.detalhe.lower()


class TestClassificacaoDeFalha:
    @pytest.mark.parametrize(
        ("mensagem", "esperado"),
        [
            ("429 quota exceeded", "quota"),
            ("404 model not found", "inexistente"),
        ],
    )
    def test_classifica_pelo_texto_do_erro(self, mensagem: str, esperado: str) -> None:
        from banco_agil.llm.catalogo import _classificar_falha

        erro = Exception(mensagem)
        erro.status_code = int(mensagem.split()[0])  # type: ignore[attr-defined]
        assert esperado in _classificar_falha(erro)
