"""Testes do repositório CSV: escrita atômica, schema e concorrência.

Nenhum teste toca `data/` versionado — tudo em tmp_path.
"""

from __future__ import annotations

import csv
import threading
from pathlib import Path

import pytest

from banco_agil.errors import DataStoreError
from banco_agil.tools.csv_repo import CsvRepository

COLUNAS = ["cpf", "nome", "data_nascimento", "limite_credito", "score"]


@pytest.fixture
def repo(tmp_path: Path) -> CsvRepository:
    return CsvRepository(tmp_path / "clientes.csv", colunas=COLUNAS, chave="cpf")


def _linha(cpf: str, nome: str = "Maria Souza") -> dict[str, str]:
    return {
        "cpf": cpf,
        "nome": nome,
        "data_nascimento": "1990-05-12",
        "limite_credito": "3000.00",
        "score": "650",
    }


class TestCriacaoELeitura:
    def test_cria_arquivo_com_cabecalho_quando_ausente(self, repo: CsvRepository) -> None:
        assert repo.ler_todos() == []
        assert repo.caminho.read_text(encoding="utf-8").strip() == ",".join(COLUNAS)

    def test_anexa_e_le_preservando_ordem_das_colunas(self, repo: CsvRepository) -> None:
        repo.anexar(_linha("52998224725"))
        linhas = repo.ler_todos()
        assert len(linhas) == 1
        assert list(linhas[0].keys()) == COLUNAS
        assert linhas[0]["nome"] == "Maria Souza"

    def test_preserva_acentuacao(self, repo: CsvRepository) -> None:
        repo.anexar(_linha("52998224725", nome="João Conceição"))
        assert repo.ler_todos()[0]["nome"] == "João Conceição"

    def test_anexa_varias_linhas_em_ordem(self, repo: CsvRepository) -> None:
        repo.anexar(_linha("52998224725", "Primeira"))
        repo.anexar(_linha("11144477735", "Segunda"))
        nomes = [linha["nome"] for linha in repo.ler_todos()]
        assert nomes == ["Primeira", "Segunda"]

    def test_campo_ausente_vira_string_vazia(self, repo: CsvRepository) -> None:
        linha = _linha("52998224725")
        linha.pop("score")
        repo.anexar(linha)
        assert repo.ler_todos()[0]["score"] == ""


class TestBuscaEAtualizacao:
    def test_busca_por_chave(self, repo: CsvRepository) -> None:
        repo.anexar(_linha("52998224725"))
        assert repo.buscar("52998224725") is not None

    def test_busca_inexistente_retorna_none(self, repo: CsvRepository) -> None:
        repo.anexar(_linha("52998224725"))
        assert repo.buscar("11144477735") is None

    def test_atualiza_campo_preservando_os_demais(self, repo: CsvRepository) -> None:
        repo.anexar(_linha("52998224725"))
        atualizado = repo.atualizar("52998224725", score="720", limite_credito="8000.00")
        assert atualizado["score"] == "720"
        assert atualizado["limite_credito"] == "8000.00"
        assert atualizado["nome"] == "Maria Souza"

        persistido = repo.buscar("52998224725")
        assert persistido is not None
        assert persistido["score"] == "720"
        assert len(repo.ler_todos()) == 1

    def test_atualiza_chave_inexistente_levanta(self, repo: CsvRepository) -> None:
        with pytest.raises(DataStoreError):
            repo.atualizar("11144477735", score="720")

    def test_atualiza_coluna_desconhecida_levanta(self, repo: CsvRepository) -> None:
        repo.anexar(_linha("52998224725"))
        with pytest.raises(DataStoreError):
            repo.atualizar("52998224725", saldo="10")


class TestIntegridadeDoArquivo:
    def test_coluna_desconhecida_no_anexo_levanta(self, repo: CsvRepository) -> None:
        linha = _linha("52998224725")
        linha["agencia"] = "0001"
        with pytest.raises(DataStoreError):
            repo.anexar(linha)

    def test_cabecalho_divergente_levanta(self, tmp_path: Path) -> None:
        caminho = tmp_path / "clientes.csv"
        caminho.write_text("cpf,nome\n52998224725,Maria\n", encoding="utf-8")
        repo = CsvRepository(caminho, colunas=COLUNAS, chave="cpf")
        with pytest.raises(DataStoreError):
            repo.ler_todos()

    def test_csv_vazio_levanta(self, tmp_path: Path) -> None:
        caminho = tmp_path / "clientes.csv"
        caminho.write_text("", encoding="utf-8")
        repo = CsvRepository(caminho, colunas=COLUNAS, chave="cpf")
        with pytest.raises(DataStoreError):
            repo.ler_todos()

    def test_operacao_com_chave_sem_chave_definida_levanta(self, tmp_path: Path) -> None:
        repo = CsvRepository(tmp_path / "x.csv", colunas=COLUNAS)
        with pytest.raises(DataStoreError):
            repo.buscar("52998224725")


class TestEscritaAtomica:
    def test_nao_deixa_arquivo_temporario(self, repo: CsvRepository, tmp_path: Path) -> None:
        repo.anexar(_linha("52998224725"))
        assert [p.name for p in tmp_path.iterdir()] == ["clientes.csv"]

    def test_arquivo_sempre_parseavel(self, repo: CsvRepository) -> None:
        for i in range(20):
            repo.anexar(_linha("52998224725", nome=f"Cliente {i}"))
        with repo.caminho.open(encoding="utf-8", newline="") as arquivo:
            assert len(list(csv.DictReader(arquivo))) == 20

    def test_anexos_concorrentes_nao_perdem_linha(self, repo: CsvRepository) -> None:
        def escrever(indice: int) -> None:
            CsvRepository(repo.caminho, colunas=COLUNAS, chave="cpf").anexar(
                _linha("52998224725", nome=f"Cliente {indice}")
            )

        threads = [threading.Thread(target=escrever, args=(i,)) for i in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(repo.ler_todos()) == 40
