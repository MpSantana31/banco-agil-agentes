"""Testes de autenticação e das regras de crédito.

A regra de decisão (aprovado/rejeitado) é testada aqui, sem LLM: é o que
garante que o modelo nunca "inventa" uma aprovação.
"""

from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path

import pytest

from banco_agil.domain.modelos import STATUS_APROVADO, STATUS_PENDENTE, STATUS_REJEITADO
from banco_agil.errors import AuthenticationError, DataStoreError, ValidationError
from banco_agil.schema import (
    COLUNAS_CLIENTES,
    COLUNAS_SCORE_LIMITE,
    COLUNAS_SOLICITACOES,
)
from banco_agil.tools.auth import ServicoAutenticacao
from banco_agil.tools.credit import ServicoCredito, carregar_faixas
from banco_agil.tools.csv_repo import CsvRepository

CPF_MARIA = "52998224725"  # score 650, limite 3000
CPF_CARLOS = "97158324079"  # score 930, limite 20000
NASCIMENTO_MARIA = "1990-05-12"
NASCIMENTO_CARLOS = "1979-07-19"

FAIXAS = [(0, 199, 500), (200, 399, 1500), (400, 599, 3000), (600, 799, 8000), (800, 1000, 20000)]

RELATORIO = "solicitacoes_aumento_limite.csv"


@pytest.fixture
def repos(tmp_path: Path) -> tuple[CsvRepository, CsvRepository, CsvRepository]:
    clientes = CsvRepository(tmp_path / "clientes.csv", colunas=COLUNAS_CLIENTES, chave="cpf")
    clientes.anexar_varias(
        [
            {
                "cpf": CPF_MARIA,
                "nome": "Maria Souza",
                "data_nascimento": NASCIMENTO_MARIA,
                "limite_credito": "3000.00",
                "score": "650",
            },
            {
                "cpf": CPF_CARLOS,
                "nome": "Carlos Eduardo Lima",
                "data_nascimento": NASCIMENTO_CARLOS,
                "limite_credito": "20000.00",
                "score": "930",
            },
        ]
    )
    score_limite = CsvRepository(tmp_path / "score_limite.csv", colunas=COLUNAS_SCORE_LIMITE)
    score_limite.anexar_varias(
        [
            {"score_min": str(minimo), "score_max": str(maximo), "limite_maximo": str(limite)}
            for minimo, maximo, limite in FAIXAS
        ]
    )
    solicitacoes = CsvRepository(tmp_path / RELATORIO, colunas=COLUNAS_SOLICITACOES)
    return clientes, score_limite, solicitacoes


@pytest.fixture
def autenticacao(repos: tuple[CsvRepository, CsvRepository, CsvRepository]) -> ServicoAutenticacao:
    return ServicoAutenticacao(repos[0])


@pytest.fixture
def credito(repos: tuple[CsvRepository, CsvRepository, CsvRepository]) -> ServicoCredito:
    clientes, score_limite, solicitacoes = repos
    relogio = lambda: dt.datetime(2026, 9, 14, 15, 30, tzinfo=dt.UTC)  # noqa: E731
    return ServicoCredito(clientes, solicitacoes, carregar_faixas(score_limite), relogio=relogio)


class TestAutenticacao:
    def test_autentica_com_cpf_e_nascimento_corretos(
        self, autenticacao: ServicoAutenticacao
    ) -> None:
        cliente = autenticacao.autenticar(CPF_MARIA, NASCIMENTO_MARIA)
        assert cliente.nome == "Maria Souza"
        assert cliente.score == 650
        assert cliente.limite_credito == pytest.approx(3000.0)

    def test_aceita_cpf_formatado_e_data_brasileira(
        self, autenticacao: ServicoAutenticacao
    ) -> None:
        cliente = autenticacao.autenticar("529.982.247-25", "12/05/1990")
        assert cliente.cpf == CPF_MARIA

    def test_cpf_inexistente_levanta(self, autenticacao: ServicoAutenticacao) -> None:
        with pytest.raises(AuthenticationError):
            autenticacao.autenticar("11144477735", NASCIMENTO_MARIA)

    def test_nascimento_errado_levanta(self, autenticacao: ServicoAutenticacao) -> None:
        with pytest.raises(AuthenticationError):
            autenticacao.autenticar(CPF_MARIA, "1991-05-12")

    def test_cpf_invalido_e_input_invalido_nao_autenticacao(
        self, autenticacao: ServicoAutenticacao
    ) -> None:
        """Formato errado é erro de entrada (não consome tentativa de autenticação)."""
        with pytest.raises(ValidationError):
            autenticacao.autenticar("123", NASCIMENTO_MARIA)
        with pytest.raises(ValidationError):
            autenticacao.autenticar(CPF_MARIA, "31/02/1990")

    def test_erro_nao_revela_se_o_cpf_existe(self, autenticacao: ServicoAutenticacao) -> None:
        with pytest.raises(AuthenticationError) as inexistente:
            autenticacao.autenticar("11144477735", "1980-01-01")
        with pytest.raises(AuthenticationError) as senha_errada:
            autenticacao.autenticar(CPF_MARIA, "1991-05-12")
        assert type(inexistente.value) is type(senha_errada.value)

    def test_recarregar_traz_score_atualizado(
        self, autenticacao: ServicoAutenticacao, repos: tuple[CsvRepository, ...]
    ) -> None:
        repos[0].atualizar(CPF_MARIA, score="820")
        assert autenticacao.recarregar(CPF_MARIA).score == 820


class TestFaixas:
    def test_carrega_e_ordena(
        self, repos: tuple[CsvRepository, CsvRepository, CsvRepository]
    ) -> None:
        faixas = carregar_faixas(repos[1])
        assert [faixa.score_min for faixa in faixas] == [0, 200, 400, 600, 800]

    def test_tabela_vazia_levanta(self, tmp_path: Path) -> None:
        vazio = CsvRepository(tmp_path / "score_limite.csv", colunas=COLUNAS_SCORE_LIMITE)
        with pytest.raises(DataStoreError):
            carregar_faixas(vazio)

    def test_faixas_sobrepostas_levantam(self, tmp_path: Path) -> None:
        repo = CsvRepository(tmp_path / "score_limite.csv", colunas=COLUNAS_SCORE_LIMITE)
        repo.anexar_varias(
            [
                {"score_min": "0", "score_max": "500", "limite_maximo": "1000"},
                {"score_min": "400", "score_max": "900", "limite_maximo": "5000"},
            ]
        )
        with pytest.raises(DataStoreError):
            carregar_faixas(repo)


class TestConsultaDeLimite:
    def test_limite_maximo_por_score(self, credito: ServicoCredito) -> None:
        assert credito.limite_maximo_para(650) == pytest.approx(8000.0)
        assert credito.limite_maximo_para(930) == pytest.approx(20000.0)
        assert credito.limite_maximo_para(0) == pytest.approx(500.0)
        assert credito.limite_maximo_para(1000) == pytest.approx(20000.0)

    def test_score_fora_da_tabela_levanta(self, credito: ServicoCredito) -> None:
        with pytest.raises(DataStoreError):
            credito.limite_maximo_para(1001)


class TestSolicitacaoDeAumento:
    def test_pedido_dentro_da_faixa_e_aprovado(self, credito: ServicoCredito) -> None:
        solicitacao = credito.solicitar_aumento(CPF_MARIA, 8000)
        assert solicitacao.status == STATUS_APROVADO
        assert solicitacao.aprovada
        assert solicitacao.limite_atual == pytest.approx(3000.0)
        assert solicitacao.score_no_pedido == 650

    def test_aprovacao_atualiza_o_limite_no_clientes_csv(self, credito: ServicoCredito) -> None:
        credito.solicitar_aumento(CPF_MARIA, 8000)
        assert credito.cliente(CPF_MARIA).limite_credito == pytest.approx(8000.0)

    def test_pedido_acima_da_faixa_e_rejeitado(self, credito: ServicoCredito) -> None:
        solicitacao = credito.solicitar_aumento(CPF_MARIA, 20000)
        assert solicitacao.status == STATUS_REJEITADO
        assert not solicitacao.aprovada
        assert "8000" in solicitacao.motivo

    def test_rejeicao_nao_altera_o_limite(self, credito: ServicoCredito) -> None:
        credito.solicitar_aumento(CPF_MARIA, 20000)
        assert credito.cliente(CPF_MARIA).limite_credito == pytest.approx(3000.0)

    def test_limite_exatamente_na_borda_e_aprovado(self, credito: ServicoCredito) -> None:
        assert credito.solicitar_aumento(CPF_MARIA, 8000).aprovada

    def test_um_centavo_acima_da_borda_e_rejeitado(self, credito: ServicoCredito) -> None:
        assert not credito.solicitar_aumento(CPF_MARIA, 8000.01).aprovada

    def test_valor_menor_que_o_atual_levanta(self, credito: ServicoCredito) -> None:
        with pytest.raises(ValidationError):
            credito.solicitar_aumento(CPF_MARIA, 1000)

    def test_valor_igual_ao_atual_levanta(self, credito: ServicoCredito) -> None:
        with pytest.raises(ValidationError):
            credito.solicitar_aumento(CPF_MARIA, 3000)

    def test_valor_invalido_levanta(self, credito: ServicoCredito) -> None:
        with pytest.raises(ValidationError):
            credito.solicitar_aumento(CPF_MARIA, "-5000")

    def test_cliente_inexistente_levanta(self, credito: ServicoCredito) -> None:
        with pytest.raises(DataStoreError):
            credito.solicitar_aumento("11144477735", 5000)

    def test_registra_data_hora_iso_8601(self, credito: ServicoCredito) -> None:
        solicitacao = credito.solicitar_aumento(CPF_MARIA, 8000)
        momento = dt.datetime.fromisoformat(solicitacao.data_hora)
        assert momento.tzinfo is not None
        assert momento == dt.datetime(2026, 9, 14, 15, 30, tzinfo=dt.UTC)


class TestHistoricoEArquivoDeSolicitacoes:
    def test_persiste_uma_linha_por_pedido(
        self, credito: ServicoCredito, repos: tuple[CsvRepository, CsvRepository, CsvRepository]
    ) -> None:
        credito.solicitar_aumento(CPF_MARIA, 8000)
        credito.solicitar_aumento(CPF_MARIA, 20000)
        assert len(repos[2].ler_todos()) == 2

    def test_colunas_do_arquivo_batem_com_o_enunciado(
        self, credito: ServicoCredito, repos: tuple[CsvRepository, CsvRepository, CsvRepository]
    ) -> None:
        credito.solicitar_aumento(CPF_MARIA, 8000)
        linha = repos[2].ler_todos()[0]
        # Nomes exatamente como o enunciado exige.
        exigidas = [
            "cpf_cliente",
            "data_hora_solicitacao",
            "limite_atual",
            "novo_limite_solicitado",
            "status_pedido",
        ]
        assert all(coluna in linha for coluna in exigidas)
        assert COLUNAS_SOLICITACOES[:5] == exigidas
        assert linha["cpf_cliente"] == CPF_MARIA
        assert linha["status_pedido"] == STATUS_APROVADO
        assert linha["limite_atual"] == "3000.00"
        assert linha["novo_limite_solicitado"] == "8000.00"

    def test_pedido_passa_por_pendente_antes_do_status_final(
        self, repos: tuple[CsvRepository, CsvRepository, CsvRepository]
    ) -> None:
        """O enunciado descreve duas etapas: registra o pedido, depois decide."""
        eventos: list[tuple[str, str]] = []

        class RepoEspiao:
            """Registra a ordem das operações e delega para o repositório real."""

            def __init__(self, real: CsvRepository) -> None:
                self._real = real

            def __getattr__(self, nome: str) -> object:
                return getattr(self._real, nome)

            def anexar(self, linha: dict[str, object]) -> None:
                eventos.append(("anexar", str(linha.get("status_pedido"))))
                self._real.anexar(linha)

            def atualizar_ultima_ocorrencia(
                self, filtros: dict[str, object], **campos: object
            ) -> dict[str, str]:
                eventos.append(("atualizar", str(campos.get("status_pedido"))))
                return self._real.atualizar_ultima_ocorrencia(filtros, **campos)

        credito = ServicoCredito(  # type: ignore[arg-type]
            repos[0],
            RepoEspiao(repos[2]),
            carregar_faixas(repos[1]),
            relogio=lambda: dt.datetime(2026, 9, 14, 15, 30, tzinfo=dt.UTC),
        )

        resultado = credito.solicitar_aumento(CPF_MARIA, 8000)

        assert eventos == [("anexar", STATUS_PENDENTE), ("atualizar", STATUS_APROVADO)]
        assert resultado.status == STATUS_APROVADO
        # Uma única linha no arquivo: o pedido é resolvido, não duplicado.
        assert len(repos[2].ler_todos()) == 1

    def test_historico_do_cliente_em_ordem_reversa(
        self, credito: ServicoCredito, repos: tuple[CsvRepository, CsvRepository, CsvRepository]
    ) -> None:
        credito.solicitar_aumento(CPF_MARIA, 4000)
        credito.solicitar_aumento(CPF_MARIA, 20000)
        credito.solicitar_aumento(CPF_CARLOS, 30000)
        historico = credito.historico(CPF_MARIA)
        assert len(historico) == 2
        assert historico[0].novo_limite == pytest.approx(20000.0)

    def test_reanalise_apos_entrevista_pode_aprovar(
        self, credito: ServicoCredito, repos: tuple[CsvRepository, CsvRepository, CsvRepository]
    ) -> None:
        """Fluxo do enunciado: pedido rejeitado -> entrevista sobe o score -> aprova."""
        assert not credito.solicitar_aumento(CPF_MARIA, 20000).aprovada

        repos[0].atualizar(CPF_MARIA, score="930")
        assert credito.solicitar_aumento(CPF_MARIA, 20000).aprovada
        assert credito.cliente(CPF_MARIA).limite_credito == pytest.approx(20000.0)
        assert [s.status for s in credito.historico(CPF_MARIA)] == [
            STATUS_APROVADO,
            STATUS_REJEITADO,
        ]


class TestPedidoConcorrente:
    """Duas sessões do mesmo CPF no mesmo segundo não podem trocar de linha."""

    @pytest.mark.parametrize("rodada", range(6))
    def test_dois_pedidos_no_mesmo_segundo_resolvem_a_propria_linha(
        self,
        credito: ServicoCredito,
        repos: tuple[CsvRepository, CsvRepository, CsvRepository],
        rodada: int,
    ) -> None:
        """Repete a corrida: sem o lock em volta de criar+resolver, a resolução de um
        pedido casa com a linha do outro (as chaves `cpf` + `data_hora` não são únicas).

        O relógio da fixture `credito` é congelado, então os dois pedidos compartilham
        `data_hora_solicitacao` — o cenário de duas abas do Streamlit no mesmo segundo.
        """
        barreira = threading.Barrier(2)
        retorno: dict[float, str] = {}
        erros: list[BaseException] = []

        def pedir(valor: float) -> None:
            barreira.wait()
            try:
                retorno[valor] = credito.solicitar_aumento(CPF_MARIA, valor).status
            except BaseException as erro:  # noqa: BLE001 - guardado para o assert
                erros.append(erro)

        threads = [threading.Thread(target=pedir, args=(valor,)) for valor in (8000, 20000)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not erros, erros
        por_valor = {
            linha["novo_limite_solicitado"]: linha["status_pedido"]
            for linha in repos[2].ler_todos()
        }
        # cada linha tem de terminar com a decisão que o próprio serviço devolveu
        assert por_valor["8000.00"] == retorno[8000] == STATUS_APROVADO
        assert por_valor["20000.00"] == retorno[20000] == STATUS_REJEITADO
        assert credito.cliente(CPF_MARIA).limite_credito == pytest.approx(8000.0)
