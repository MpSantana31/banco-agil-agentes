"""Autenticação do cliente contra ``clientes.csv``."""

from __future__ import annotations

import datetime as dt

from banco_agil.domain.modelos import Cliente
from banco_agil.errors import AuthenticationError, DataStoreError
from banco_agil.tools.csv_repo import CsvRepository
from banco_agil.tools.validators import mascarar_cpf, parse_data_nascimento, validar_cpf

__all__ = ["ServicoAutenticacao"]


class ServicoAutenticacao:
    """Confere CPF + data de nascimento contra a base de clientes."""

    def __init__(self, repo_clientes: CsvRepository) -> None:
        self._clientes = repo_clientes

    def autenticar(self, cpf: object, data_nascimento: object) -> Cliente:
        """Devolve o ``Cliente`` autenticado ou levanta ``AuthenticationError``.

        A mensagem é sempre a mesma para "CPF inexistente" e "data não confere":
        assim a conversa não revela a um terceiro se um CPF está na base
        (e o Agente de Triagem nunca diferencia os dois casos para o cliente).
        """
        cpf_normalizado = validar_cpf(cpf)  # ValidationError: formato/dígito
        nascimento = parse_data_nascimento(data_nascimento)  # ValidationError: data

        linha = self._clientes.buscar(cpf_normalizado)
        if linha is None:
            raise AuthenticationError(
                f"Cliente não localizado para o CPF {mascarar_cpf(cpf_normalizado)}."
            )

        cliente = Cliente.de_linha(linha)
        if cliente.data_nascimento != nascimento:
            raise AuthenticationError(
                f"Data de nascimento não confere para o CPF {mascarar_cpf(cpf_normalizado)}."
            )

        return cliente

    @staticmethod
    def idade(cliente: Cliente, hoje: dt.date | None = None) -> int:
        """Idade em anos completos (apoio à conversa, não regra de negócio)."""
        hoje = hoje or dt.date.today()
        anos = hoje.year - cliente.data_nascimento.year
        aniversario_passou = (hoje.month, hoje.day) >= (
            cliente.data_nascimento.month,
            cliente.data_nascimento.day,
        )
        return anos if aniversario_passou else anos - 1

    def recarregar(self, cpf: str) -> Cliente:
        """Lê o cliente novamente (após a entrevista ter gravado novo score)."""
        linha = self._clientes.buscar(cpf)
        if linha is None:
            raise DataStoreError(f"Cliente {mascarar_cpf(cpf)} desapareceu da base.")
        return Cliente.de_linha(linha)
