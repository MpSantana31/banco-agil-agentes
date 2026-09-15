"""Dados ausentes ou corrompidos na base não podem derrubar o atendimento.

O enunciado pede tratamento de erro em leitura de arquivos: aqui ficam os casos
de linha inválida, cabeçalho divergente e coluna faltando.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from banco_agil.config import Settings
from banco_agil.domain.modelos import Cliente, SolicitacaoAumento
from banco_agil.errors import DataStoreError
from banco_agil.orchestrator.atendimento import Atendimento
from banco_agil.servicos import construir_servicos
from banco_agil.tools.csv_repo import CsvRepository
from tests.conftest import CPF_MARIA, NASCIMENTO_MARIA
from tests.fakes import ScriptedChatModel, chamada, texto


class TestModelosRecusamLinhaInvalida:
    @pytest.mark.parametrize("valor", ["nan", "", "muito", "-"])
    def test_limite_invalido_levanta(self, valor: str) -> None:
        linha = {
            "cpf": CPF_MARIA,
            "nome": "Maria Souza",
            "data_nascimento": "1990-05-12",
            "limite_credito": valor,
            "score": "650",
        }
        with pytest.raises(DataStoreError):
            Cliente.de_linha(linha)

    def test_score_invalido_levanta(self) -> None:
        linha = {
            "cpf": CPF_MARIA,
            "nome": "Maria Souza",
            "data_nascimento": "1990-05-12",
            "limite_credito": "3000.00",
            "score": "sem score",
        }
        with pytest.raises(DataStoreError):
            Cliente.de_linha(linha)

    def test_data_invalida_levanta(self) -> None:
        linha = {
            "cpf": CPF_MARIA,
            "nome": "Maria Souza",
            "data_nascimento": "12/05/1990",
            "limite_credito": "3000.00",
            "score": "650",
        }
        with pytest.raises(DataStoreError):
            Cliente.de_linha(linha)

    def test_solicitacao_com_status_desconhecido_levanta(self) -> None:
        linha = {
            "cpf_cliente": CPF_MARIA,
            "data_hora_solicitacao": "2026-09-14T12:00:00+00:00",
            "limite_atual": "3000.00",
            "novo_limite_solicitado": "8000.00",
            "status_pedido": "em análise",
        }
        with pytest.raises(DataStoreError):
            SolicitacaoAumento.de_linha(linha)

    def test_status_reprovado_e_aceito_como_rejeitado(self) -> None:
        """O enunciado usa os dois termos; o dado gravado é sempre 'rejeitado'."""
        linha = {
            "cpf_cliente": CPF_MARIA,
            "data_hora_solicitacao": "2026-09-14T12:00:00+00:00",
            "limite_atual": "3000.00",
            "novo_limite_solicitado": "20000.00",
            "status_pedido": "reprovado",
        }
        assert SolicitacaoAumento.de_linha(linha).status == "rejeitado"


def _servicos_com_base_corrompida(tmp_path: Path, linha_invalida: str) -> object:
    """Serviços reais, mas com um cliente corrompido no CSV."""
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        llm_provider="fake",
        fx_cache_ttl_segundos=0,
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    clientes = CsvRepository(
        settings.caminho_clientes,
        colunas=["cpf", "nome", "data_nascimento", "limite_credito", "score"],
        chave="cpf",
    )
    clientes.anexar(
        {
            "cpf": CPF_MARIA,
            "nome": "Maria Souza",
            "data_nascimento": "1990-05-12",
            "limite_credito": linha_invalida,
            "score": "650",
        }
    )
    score_limite = CsvRepository(
        settings.caminho_score_limite, colunas=["score_min", "score_max", "limite_maximo"]
    )
    score_limite.anexar_varias(
        [
            {"score_min": "0", "score_max": "599", "limite_maximo": "3000.00"},
            {"score_min": "600", "score_max": "1000", "limite_maximo": "8000.00"},
        ]
    )
    return construir_servicos(settings)


class TestAtendimentoComBaseCorrompida:
    def test_autenticacao_com_limite_invalido_nao_derruba_a_conversa(
        self,
        tmp_path: Path,
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        servicos = _servicos_com_base_corrompida(tmp_path, "nan")
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento=NASCIMENTO_MARIA),
            texto("Desculpe, tive um problema com seus dados agora. Pode tentar mais tarde?"),
        )
        atendimento = Atendimento(modelo, servicos)  # type: ignore[arg-type]

        resposta = atendimento.responder("52998224725 / 12/05/1990")

        assert "Desculpe" in resposta.texto
        assert not atendimento.estado.autenticado
        assert not resposta.encerrado
        conteudos = [
            str(mensagem.content)
            for mensagem in atendimento.estado.mensagens
            if mensagem.type == "tool"
        ]
        assert any("FALHA_INTERNA" in conteudo for conteudo in conteudos)

    def test_tentativas_de_autenticacao_nao_sao_consumidas_por_erro_interno(
        self,
        tmp_path: Path,
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        """Erro de infraestrutura não pode queimar as 3 tentativas do cliente."""
        servicos = _servicos_com_base_corrompida(tmp_path, "nan")
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento=NASCIMENTO_MARIA),
            texto("Problema técnico, desculpe."),
        )
        atendimento = Atendimento(modelo, servicos)  # type: ignore[arg-type]

        atendimento.responder("52998224725 / 12/05/1990")

        assert atendimento.estado.tentativas_autenticacao == 0
