"""Log técnico: o CPF não pode sair inteiro por nenhum caminho.

O módulo promete mascarar dado pessoal antes de gravar. O caminho fácil de
esquecer é o rastro de exceção inesperada em nível ``DEBUG``: se o traceback for
entregue cru ao handler, o CPF vai em claro para ``logs/erros.log``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from banco_agil.observability.log import (
    mascarar_dados_pessoais,
    obter_logger,
    registrar_falha,
)

CPF = "52998224725"


class TestMascara:
    def test_mascara_cpf_com_e_sem_pontuacao(self) -> None:
        assert "52998224725" not in mascarar_dados_pessoais(f"cpf lido {CPF}")
        assert mascarar_dados_pessoais("cpf 529.982.247-25") == "cpf ***25"

    def test_mascara_par_chave_valor(self) -> None:
        texto = mascarar_dados_pessoais("api_key=sk-or-v1-abc123 chamando o provedor")
        assert "sk-or-v1-abc123" not in texto
        assert "api_key=***" in texto

    def test_mascara_cpf_colado_a_alfanumerico(self) -> None:
        """Sem delimitador (`user52998224725`) o CPF ainda tem de sair mascarado."""
        assert "52998224725" not in mascarar_dados_pessoais("user52998224725")
        assert mascarar_dados_pessoais("id52998224725x") == "id***25x"

    def test_nao_mascara_numero_maior_que_um_cpf(self) -> None:
        """Sequência de 12+ dígitos não é CPF: nada de mascarar pedaço dela."""
        assert mascarar_dados_pessoais("protocolo 123456789012") == "protocolo 123456789012"

    def test_texto_sem_dado_pessoal_passa_intacto(self) -> None:
        assert mascarar_dados_pessoais("score 650 permite até 8000.00") == (
            "score 650 permite até 8000.00"
        )


def _logar_falha(caminho: Path) -> str:
    logger = obter_logger()
    handler = logging.FileHandler(caminho, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
    nivel_anterior = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:

        def estoura() -> None:
            raise RuntimeError(f"payload recusado: {{'cpf': '{CPF}'}}")

        try:
            estoura()
        except RuntimeError as erro:
            registrar_falha("llm", erro)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(nivel_anterior)
        handler.close()
    return caminho.read_text(encoding="utf-8")


class TestRastroDeExcecao:
    def test_traceback_em_debug_nao_vaza_cpf(self, tmp_path: Path) -> None:
        texto = _logar_falha(tmp_path / "erros.log")

        assert "traceback de erro inesperado" in texto
        assert CPF not in texto
        assert "***25" in texto

    def test_falha_de_dominio_nao_escreve_rastro(self, tmp_path: Path) -> None:
        """Erro esperado (BancoAgilError) não precisa de stack trace no log."""
        from banco_agil.errors import ValidationError

        logger = obter_logger()
        caminho = tmp_path / "erros.log"
        handler = logging.FileHandler(caminho, encoding="utf-8")
        nivel_anterior = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            registrar_falha("ferramenta:consultar_limite", ValidationError("dado inválido"))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(nivel_anterior)
            handler.close()

        texto = caminho.read_text(encoding="utf-8")
        assert "ValidationError: dado inválido" in texto
        assert "traceback" not in texto
