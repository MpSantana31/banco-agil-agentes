"""Exceções do Banco Ágil.

Uma exceção por *natureza de falha* — a camada de conversa decide a mensagem
ao cliente olhando o tipo, nunca o texto da exceção (que é técnico).
"""

from __future__ import annotations


class BancoAgilError(Exception):
    """Erro base do sistema. Toda exceção levantada aqui é previsível."""


class ValidationError(BancoAgilError):
    """Dado de entrada inválido (CPF, data, valor, enum fora do domínio)."""


class AuthenticationError(BancoAgilError):
    """Cliente não localizado na base ou credenciais que não conferem."""


class DataStoreError(BancoAgilError):
    """Falha de contrato/IO nos arquivos CSV (schema divergente, IO inválido)."""


class ExternalAPIError(BancoAgilError):
    """Falha ao consultar serviço externo (cotação de moedas)."""


class LLMProviderError(BancoAgilError):
    """Provedor de LLM indisponível, sem chave ou com modelo desconhecido."""


class ConfirmacaoNecessaria(BancoAgilError):
    """Operação destrutiva (sobrescrever o `.env`) sem confirmação explícita.

    Existe para tornar impossível, por acidente, uma escrita que apaga
    credenciais — o código se recusa a escrever e exige ``confirmado=True``.
    """
