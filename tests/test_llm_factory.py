"""Testes da fábrica de modelos — o diferencial de "aceitar diferentes APIs".

Nenhum teste faz chamada de rede: construímos o modelo e inspecionamos a
configuração. O que se prova aqui é que trocar de provedor é só configuração.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage

from banco_agil.config import Settings
from banco_agil.errors import LLMProviderError
from banco_agil.llm.factory import MODELOS_PADRAO, construir_modelo, modelo_padrao_do_provedor


def _settings(**kwargs: object) -> Settings:
    """Settings isolado: ignora `.env` e o ambiente do desenvolvedor."""
    base: dict[str, object] = {"_env_file": None}
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


class TestSelecaoDeProvedor:
    def test_provedor_desconhecido_levanta_com_a_lista(self) -> None:
        with pytest.raises(LLMProviderError) as exc:
            construir_modelo(_settings(llm_provider="banco-central"))
        assert "gemini" in str(exc.value)

    def test_provedor_em_branco_levanta(self) -> None:
        with pytest.raises(LLMProviderError):
            construir_modelo(_settings(llm_provider="  "))

    @pytest.mark.parametrize("provedor", sorted(MODELOS_PADRAO))
    def test_todo_provedor_do_mapa_tem_construtor(self, provedor: str) -> None:
        # Sem chave: constrói (fake/ollama) ou reclama de credencial, nunca "desconhecido".
        try:
            construir_modelo(_settings(llm_provider=provedor))
        except LLMProviderError as erro:
            assert "desconhecido" not in str(erro).lower()

    def test_modelo_padrao_de_provedor_desconhecido_levanta(self) -> None:
        with pytest.raises(LLMProviderError):
            modelo_padrao_do_provedor("nao-existe")


class TestResolucaoDeModelo:
    def test_usa_default_do_provedor_quando_llm_model_vazio(self) -> None:
        modelo = construir_modelo(_settings(llm_provider="openai", openai_api_key="sk-teste"))
        assert modelo.model_name == MODELOS_PADRAO["openai"]

    def test_llm_model_sobrescreve_o_default(self) -> None:
        modelo = construir_modelo(
            _settings(llm_provider="openai", llm_model="gpt-4o", openai_api_key="sk-teste")
        )
        assert modelo.model_name == "gpt-4o"

    def test_parametro_explicito_vence_a_configuracao(self) -> None:
        modelo = construir_modelo(
            _settings(llm_provider="openai", openai_api_key="sk-teste"), modelo="gpt-3.5-turbo"
        )
        assert modelo.model_name == "gpt-3.5-turbo"


class TestCredenciais:
    def test_gemini_sem_chave_levanta(self) -> None:
        with pytest.raises(LLMProviderError) as exc:
            construir_modelo(_settings(llm_provider="gemini", gemini_api_key=None))
        assert "GEMINI_API_KEY" not in str(exc.value)  # fala o nome, nunca o valor
        assert "chave" in str(exc.value).lower()

    def test_openrouter_sem_chave_levanta(self) -> None:
        with pytest.raises(LLMProviderError):
            construir_modelo(_settings(llm_provider="openrouter", openrouter_api_key=None))

    def test_chave_em_branco_conta_como_ausente(self) -> None:
        with pytest.raises(LLMProviderError):
            construir_modelo(_settings(llm_provider="openai", openai_api_key="   "))

    def test_erro_de_credencial_nao_vaza_a_chave(self) -> None:
        with pytest.raises(LLMProviderError) as exc:
            construir_modelo(_settings(llm_provider="gemini", gemini_api_key=""))
        assert "AIza" not in str(exc.value)


class TestProvedoresCompativeisComOpenAI:
    def test_openrouter_usa_endpoint_proprio(self) -> None:
        modelo = construir_modelo(
            _settings(llm_provider="openrouter", openrouter_api_key="sk-or-teste")
        )
        assert "openrouter.ai" in str(modelo.openai_api_base)

    def test_groq_usa_endpoint_proprio(self) -> None:
        modelo = construir_modelo(_settings(llm_provider="groq", groq_api_key="gsk-teste"))
        assert "groq.com" in str(modelo.openai_api_base)

    def test_ollama_local_dispensa_chave(self) -> None:
        modelo = construir_modelo(_settings(llm_provider="ollama", ollama_api_key=None))
        assert "11434" in str(modelo.openai_api_base)

    def test_provider_compativel_exige_base_url(self) -> None:
        with pytest.raises(LLMProviderError) as exc:
            construir_modelo(_settings(llm_provider="compativel", openai_api_key="sk-x"))
        assert "LLM_BASE_URL" in str(exc.value)

    def test_provider_compativel_aceita_endpoint_proprio(self) -> None:
        modelo = construir_modelo(
            _settings(
                llm_provider="compativel",
                llm_base_url="https://minha-inferencia.local/v1",
                openai_api_key="sk-x",
                llm_model="meu-modelo",
            )
        )
        assert "minha-inferencia.local" in str(modelo.openai_api_base)
        assert modelo.model_name == "meu-modelo"


class TestProvedorFake:
    def test_constroi_sem_chave(self) -> None:
        assert isinstance(construir_modelo(_settings(llm_provider="fake")), BaseChatModel)

    def test_responde_sem_rede(self) -> None:
        modelo = construir_modelo(_settings(llm_provider="fake"))
        resposta = modelo.invoke("olá")
        assert isinstance(resposta, AIMessage)
        assert resposta.content

    def test_aceita_ferramentas(self) -> None:
        """Sem isso o atendimento não funciona com `fake`: bind_tools não pode estourar."""
        modelo = construir_modelo(_settings(llm_provider="fake"))
        assert modelo.bind_tools([]) is modelo

    def test_bind_tools_no_atendimento_nao_quebra(self) -> None:
        modelo = construir_modelo(_settings(llm_provider="fake"))
        ligado = modelo.bind_tools([], tool_choice=None)
        assert isinstance(ligado.invoke("olá"), AIMessage)
