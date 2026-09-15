"""Fixtures compartilhadas: serviços isolados em tmp_path, sem rede e sem LLM."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from banco_agil.config import Settings
from banco_agil.domain.modelos import Cotacao
from banco_agil.orchestrator.atendimento import Atendimento
from banco_agil.servicos import Servicos, construir_servicos
from tests.fakes import ScriptedChatModel

RAIZ = Path(__file__).resolve().parents[1]

CPF_MARIA = "52998224725"  # score 650, limite 3000.00 -> pode ir até 8000
NASCIMENTO_MARIA = "12/05/1990"
CPF_CARLOS = "97158324079"  # score 930, limite 20000.00 -> pode ir até 20000
NASCIMENTO_CARLOS = "19/07/1979"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings apontando para um diretório de dados descartável."""
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        llm_provider="fake",
        fx_cache_ttl_segundos=0,
    )


@pytest.fixture
def servicos(settings: Settings) -> Servicos:
    """Serviços reais sobre os CSVs de seed do repositório, copiados para tmp."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(RAIZ / "data" / "clientes.csv", settings.caminho_clientes)
    shutil.copy(RAIZ / "data" / "score_limite.csv", settings.caminho_score_limite)
    return construir_servicos(settings)


class CambioFalso:
    """Substituto do serviço de câmbio: sem rede, resultado controlado."""

    def __init__(self, cotacao: Cotacao | Exception) -> None:
        self.cotacao_configurada = cotacao
        self.chamadas: list[str] = []

    def cotacao(self, moeda: object) -> Cotacao:
        self.chamadas.append(str(moeda))
        if isinstance(self.cotacao_configurada, Exception):
            raise self.cotacao_configurada
        return self.cotacao_configurada


@pytest.fixture
def cambio_falso() -> Callable[..., CambioFalso]:
    def criar(
        valor: float = 5.43,
        moeda: str = "USD",
        falha: Exception | None = None,
    ) -> CambioFalso:
        if falha is not None:
            return CambioFalso(falha)
        return CambioFalso(
            Cotacao(
                moeda=moeda,
                nome="Dólar americano",
                valor=valor,
                atualizado_em="2026-09-14 12:00:00",
                fonte="AwesomeAPI (Banco Central)",
            )
        )

    return criar


@pytest.fixture
def modelo_roteirizado() -> Callable[..., ScriptedChatModel]:
    def criar(*respostas: object) -> ScriptedChatModel:
        return ScriptedChatModel(roteiro=list(respostas))  # type: ignore[arg-type]

    return criar


@pytest.fixture
def atendimento_factory(
    servicos: Servicos,
) -> Callable[[ScriptedChatModel], Atendimento]:
    def criar(modelo: ScriptedChatModel) -> Atendimento:
        return Atendimento(modelo, servicos)

    return criar
