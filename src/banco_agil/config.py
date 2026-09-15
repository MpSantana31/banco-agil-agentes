"""Configuração central (pydantic-settings), lida do ambiente/`.env`.

Nenhuma credencial tem default no código: chave ausente vira erro explícito
no momento de montar o modelo, nunca um valor mudo.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "obter_settings", "PROVEDORES_SUPORTADOS"]

PROVEDORES_SUPORTADOS: dict[str, str] = {
    "gemini": "Google Gemini (langchain-google-genai)",
    "openai": "OpenAI",
    "openrouter": "OpenRouter (compatível com OpenAI)",
    "groq": "Groq (compatível com OpenAI)",
    "togetherai": "Together AI (compatível com OpenAI)",
    "ollama": "Ollama local (compatível com OpenAI)",
    "compativel": "Qualquer endpoint compatível com OpenAI via LLM_BASE_URL",
    "fake": "Modelo de mentira, determinístico — usado em testes e demo sem chave",
}

_RAIZ_PROJETO = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Configuração do sistema. Tudo sobrescrevível por variável de ambiente."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    llm_provider: str = Field(default="gemini", description="Provedor de LLM ativo.")
    llm_model: str = Field(default="", description="Modelo; vazio usa o default do provedor.")
    llm_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    llm_timeout: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_base_url: str = Field(default="", description="Endpoint para provider 'compativel'.")

    llm_fallback_1: str = Field(default="", description="Segundo modelo da cadeia.")
    llm_fallback_2: str = Field(default="", description="Terceiro modelo da cadeia.")
    llm_tentativas_por_modelo: int = Field(
        default=2,
        ge=1,
        le=5,
        description="Tentativas por modelo antes de trocar (só para soluço passageiro).",
    )

    gemini_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY")
    )
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    openrouter_api_key: SecretStr | None = Field(default=None, alias="OPENROUTER_API_KEY")
    groq_api_key: SecretStr | None = Field(default=None, alias="GROQ_API_KEY")
    togetherai_api_key: SecretStr | None = Field(default=None, alias="TOGETHER_API_KEY")
    ollama_api_key: SecretStr | None = Field(default=None, alias="OLLAMA_API_KEY")

    data_dir: Path = Field(default=_RAIZ_PROJETO / "data", description="Diretório dos CSVs.")
    log_dir: Path = Field(default=_RAIZ_PROJETO / "logs")
    log_level: str = Field(default="INFO")

    max_tentativas_autenticacao: int = Field(default=3, ge=1, le=10)
    fx_timeout: float = Field(default=8.0, gt=0)
    fx_cache_ttl_segundos: int = Field(default=300, ge=0)

    @property
    def caminho_clientes(self) -> Path:
        return self.data_dir / "clientes.csv"

    @property
    def caminho_score_limite(self) -> Path:
        return self.data_dir / "score_limite.csv"

    @property
    def caminho_solicitacoes(self) -> Path:
        return self.data_dir / "solicitacoes_aumento_limite.csv"

    @property
    def caminho_erros(self) -> Path:
        return self.log_dir / "erros.log"

    def chave_do_provedor(self, provedor: str | None = None) -> SecretStr | None:
        """Chave configurada para o provedor (``None`` quando não há)."""
        mapa = {
            "gemini": self.gemini_api_key,
            "openai": self.openai_api_key,
            "openrouter": self.openrouter_api_key,
            "groq": self.groq_api_key,
            "togetherai": self.togetherai_api_key,
            "ollama": self.ollama_api_key,
            "compativel": self.openai_api_key,
        }
        return mapa.get(provedor or self.llm_provider)


@lru_cache(maxsize=1)
def obter_settings() -> Settings:
    """Instância única de ``Settings`` (cacheada por processo)."""
    return Settings()
