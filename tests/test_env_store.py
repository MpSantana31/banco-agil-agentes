"""Testes da escrita no `.env` — o ponto mais sensível do sistema.

Prova-se aqui que **nenhuma** escrita acontece sem confirmação explícita.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from banco_agil.env_store import (
    CHAVES_GERENCIADAS,
    gravar_env,
    ler_env,
    mascarar_valor,
    planejar_alteracoes,
)
from banco_agil.errors import ConfirmacaoNecessaria, ValidationError

CONTEUDO = """\
# Configuração do Banco Ágil
LLM_PROVIDER=gemini
LLM_MODEL=gemini-3.6-flash
GEMINI_API_KEY=AIzaSyChaveAntigaDoGemini123

# variável de outro sistema — não pode ser tocada
TELEGRAM_BOT_TOKEN=123456:AAbb
DATA_DIR=./data
"""


@pytest.fixture
def env(tmp_path: Path) -> Path:
    caminho = tmp_path / ".env"
    caminho.write_text(CONTEUDO, encoding="utf-8")
    return caminho


class TestMascaramento:
    def test_segredo_aparece_so_pelas_pontas(self) -> None:
        assert mascarar_valor("GEMINI_API_KEY", "AIzaSyChaveAntigaDoGemini123") == "AIzaSy…i123"

    def test_segredo_curto_nao_vaza_tamanho_exato(self) -> None:
        assert mascarar_valor("GROQ_API_KEY", "curta") == "*****"

    def test_valor_comum_aparece_inteiro(self) -> None:
        assert mascarar_valor("LLM_MODEL", "gemini-3.6-flash") == "gemini-3.6-flash"

    def test_vazio_fica_vazio(self) -> None:
        assert mascarar_valor("GEMINI_API_KEY", "") == ""


class TestLeitura:
    def test_le_variaveis_e_ignora_comentarios(self, env: Path) -> None:
        valores = ler_env(env)
        assert valores["LLM_PROVIDER"] == "gemini"
        assert "TELEGRAM_BOT_TOKEN" in valores
        assert "# Configuração do Banco Ágil" not in valores

    def test_arquivo_inexistente_devolve_vazio(self, tmp_path: Path) -> None:
        assert ler_env(tmp_path / "nao_existe.env") == {}


class TestPlanejamento:
    def test_mostra_somente_o_que_muda(self, env: Path) -> None:
        alteracoes = planejar_alteracoes(
            env, {"LLM_MODEL": "gemini-3.6-flash", "LLM_FALLBACK_1": "groq:llama-3.3-70b"}
        )
        assert [alteracao.chave for alteracao in alteracoes] == ["LLM_FALLBACK_1"]
        assert alteracoes[0].valor_atual == ""
        assert alteracoes[0].valor_novo == "groq:llama-3.3-70b"

    def test_descricao_nao_expoe_segredo(self, env: Path) -> None:
        alteracoes = planejar_alteracoes(env, {"GEMINI_API_KEY": "AIzaNovaChaveMuitoLonga999"})

        assert alteracoes[0].valor_atual == "AIzaSyChaveAntigaDoGemini123"  # dado cru (uso interno)
        descricao = alteracoes[0].descricao()
        assert "AIzaSyChaveAntigaDoGemini123" not in descricao
        assert "AIzaNovaChaveMuitoLonga999" not in descricao
        assert descricao == "GEMINI_API_KEY: AIzaSy…i123 → AIzaNo…a999"

    def test_chave_fora_da_lista_gerenciada_levanta(self, env: Path) -> None:
        with pytest.raises(ValidationError, match="fora da lista"):
            planejar_alteracoes(env, {"TELEGRAM_BOT_TOKEN": "outro"})

    def test_lista_de_chaves_gerenciadas_inclui_os_slots(self) -> None:
        assert {"LLM_PROVIDER", "LLM_MODEL", "LLM_FALLBACK_1", "LLM_FALLBACK_2"} <= set(
            CHAVES_GERENCIADAS
        )


class TestGravacao:
    def test_sem_confirmacao_nao_escreve_nada(self, env: Path) -> None:
        antes = env.read_text(encoding="utf-8")

        with pytest.raises(ConfirmacaoNecessaria):
            gravar_env(env, {"LLM_MODEL": "outro-modelo"}, confirmado=False)

        assert env.read_text(encoding="utf-8") == antes
        assert list(env.parent.glob("*.bak-*")) == []

    def test_com_confirmacao_grava_e_preserva_o_resto(self, env: Path) -> None:
        gravar_env(
            env,
            {"LLM_MODEL": "gemini-3.6-flash", "LLM_FALLBACK_1": "openrouter:google/gemma-x"},
            confirmado=True,
            quando=dt.datetime(2026, 9, 14, 13, 45, 0),
        )
        conteudo = env.read_text(encoding="utf-8")

        assert "LLM_FALLBACK_1=openrouter:google/gemma-x" in conteudo
        assert "# Configuração do Banco Ágil" in conteudo
        assert "TELEGRAM_BOT_TOKEN=123456:AAbb" in conteudo  # outro sistema intacto
        assert "DATA_DIR=./data" in conteudo
        assert conteudo.count("LLM_MODEL=") == 1

    def test_cria_backup_com_o_conteudo_anterior(self, env: Path) -> None:
        backup = gravar_env(
            env,
            {"LLM_PROVIDER": "openrouter"},
            confirmado=True,
            quando=dt.datetime(2026, 9, 14, 13, 45, 0),
        )

        assert backup.name == ".env.bak-20260914-134500"
        assert "LLM_PROVIDER=gemini" in backup.read_text(encoding="utf-8")
        assert "LLM_PROVIDER=openrouter" in env.read_text(encoding="utf-8")

    def test_valor_vazio_remove_a_linha(self, env: Path) -> None:
        gravar_env(env, {"LLM_FALLBACK_1": ""}, confirmado=True)
        assert "LLM_FALLBACK_1" not in ler_env(env)

    def test_none_remove_a_linha_existente(self, env: Path) -> None:
        gravar_env(env, {"LLM_MODEL": None}, confirmado=True)
        assert "LLM_MODEL" not in ler_env(env)

    def test_chave_nova_vai_para_o_fim_com_cabecalho(self, env: Path) -> None:
        gravar_env(env, {"LLM_FALLBACK_2": "groq:llama-3.3-70b"}, confirmado=True)
        linhas = env.read_text(encoding="utf-8").splitlines()
        assert linhas[-1] == "LLM_FALLBACK_2=groq:llama-3.3-70b"
        assert any(linha.startswith("# --- cadeia de modelos") for linha in linhas)

    def test_arquivo_fica_com_permissao_restrita(self, env: Path) -> None:
        gravar_env(env, {"LLM_PROVIDER": "groq"}, confirmado=True)
        assert os.stat(env).st_mode & 0o777 == 0o600

    def test_cria_arquivo_quando_nao_existe(self, tmp_path: Path) -> None:
        caminho = tmp_path / "novo" / ".env"
        gravar_env(caminho, {"LLM_PROVIDER": "fake"}, confirmado=True)
        assert ler_env(caminho) == {"LLM_PROVIDER": "fake"}
