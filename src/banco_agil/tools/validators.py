"""Validação e normalização de entrada do cliente.

Regra do projeto: **a LLM nunca decide formato**. Ela repassa o texto cru do
cliente; normalizar/validar é responsabilidade desta camada, que é pura e
testável sem rede e sem modelo.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import unicodedata

from banco_agil.errors import ValidationError

__all__ = [
    "mascarar_cpf",
    "normalizar_cpf",
    "validar_cpf",
    "parse_data_nascimento",
    "parse_valor_monetario",
    "normalizar_emprego",
    "normalizar_dependentes",
    "normalizar_tem_dividas",
]

_CPF_TAMANHO = 11
_FORMATOS_DATA = (
    re.compile(r"^(?P<dia>\d{1,2})[/\-.](?P<mes>\d{1,2})[/\-.](?P<ano>\d{4})$"),
    re.compile(r"^(?P<ano>\d{4})[/\-.](?P<mes>\d{1,2})[/\-.](?P<dia>\d{1,2})$"),
)
_ANO_MINIMO = 1900
_NUMEROS_ESCRITOS = {
    "zero": 0,
    "nenhum": 0,
    "um": 1,
    "uma": 1,
    "dois": 2,
    "duas": 2,
    "tres": 3,
    "quatro": 4,
    "cinco": 5,
}
_MAX_DEPENDENTES = 3


def _sem_acento(texto: str) -> str:
    """Remove acentos e caixa para comparações de palavra-chave."""
    decomposto = unicodedata.normalize("NFKD", texto)
    sem_marcas = "".join(c for c in decomposto if not unicodedata.combining(c))
    return sem_marcas.strip().lower()


def normalizar_cpf(valor: object) -> str:
    """Devolve apenas os dígitos do CPF (aceita máscara e espaços)."""
    return re.sub(r"\D", "", str(valor or ""))


def mascarar_cpf(cpf: object) -> str:
    """Mascara o miolo do documento — usado em log e em mensagem de erro."""
    digitos = normalizar_cpf(cpf)
    if len(digitos) != _CPF_TAMANHO:
        return "***"
    return f"{digitos[:3]}.***.***-{digitos[-2:]}"


def _digito_verificador(digitos: str) -> str:
    pesos = range(len(digitos) + 1, 1, -1)
    soma = sum(int(d) * peso for d, peso in zip(digitos, pesos, strict=True))
    resto = soma % 11
    return "0" if resto < 2 else str(11 - resto)


def validar_cpf(valor: object) -> str:
    """Valida CPF (tamanho, repetição e dígitos verificadores).

    Levanta ``ValidationError`` com o documento mascarado na mensagem —
    o CPF completo nunca aparece em log ou traceback.
    """
    digitos = normalizar_cpf(valor)

    if len(digitos) != _CPF_TAMANHO:
        raise ValidationError(
            "CPF precisa ter exatamente 11 dígitos "
            f"(recebido: {len(digitos) if digitos else 0} dígito(s))."
        )

    if digitos == digitos[0] * _CPF_TAMANHO:
        raise ValidationError(f"CPF inválido: {mascarar_cpf(digitos)}.")

    if digitos[:9] + _digito_verificador(digitos[:9]) != digitos[:10] or (
        digitos[:10] + _digito_verificador(digitos[:10]) != digitos
    ):
        raise ValidationError(f"CPF inválido: {mascarar_cpf(digitos)} (dígitos verificadores).")

    return digitos


def parse_data_nascimento(valor: object) -> dt.date:
    """Interpreta a data de nascimento em formatos comuns (BR e ISO)."""
    texto = str(valor or "").strip()
    if not texto:
        raise ValidationError("Data de nascimento vazia.")

    for formato in _FORMATOS_DATA:
        casado = formato.match(texto)
        if not casado:
            continue
        try:
            data = dt.date(
                int(casado.group("ano")),
                int(casado.group("mes")),
                int(casado.group("dia")),
            )
        except ValueError as erro:
            raise ValidationError(f"Data de nascimento inexistente: {texto!r}.") from erro
        break
    else:
        raise ValidationError(
            f"Data de nascimento não reconhecida: {texto!r}. Use dd/mm/aaaa (ex.: 12/05/1990)."
        )

    if data > dt.date.today():
        raise ValidationError("Data de nascimento no futuro.")
    if data.year < _ANO_MINIMO:
        raise ValidationError(f"Ano de nascimento anterior a {_ANO_MINIMO}.")

    return data


def parse_valor_monetario(valor: object) -> float:
    """Interpreta valor monetário em formato BR ou US, sempre não negativo."""
    if isinstance(valor, bool):
        raise ValidationError("Valor monetário não pode ser booleano.")

    if isinstance(valor, (int, float)):
        numero = float(valor)
        if numero < 0:
            raise ValidationError("Valor monetário não pode ser negativo.")
        return round(numero, 2)

    texto = str(valor or "").strip().replace("@", "")
    texto = re.sub(r"(?i)r\$", "", texto).replace("\u00a0", "").replace(" ", "")
    if not texto:
        raise ValidationError("Valor monetário vazio.")
    if texto.startswith("-"):
        raise ValidationError("Valor monetário não pode ser negativo.")

    if "," in texto and "." in texto:
        separador_decimal = "," if texto.rfind(",") > texto.rfind(".") else "."
        separador_milhar = "." if separador_decimal == "," else ","
        texto = texto.replace(separador_milhar, "").replace(separador_decimal, ".")
    elif "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif "." in texto:
        partes = texto.split(".")
        # "8.000" (BR) é milhar; "8000.50" é decimal. Três casas => milhar.
        if len(partes) > 2 or len(partes[1]) == 3:
            texto = "".join(partes)

    try:
        numero = float(texto)
    except ValueError as erro:
        raise ValidationError(f"Valor monetário não reconhecido: {valor!r}.") from erro

    if not math.isfinite(numero):
        raise ValidationError(f"Valor monetário não é um número finito: {valor!r}.")
    if numero < 0:
        raise ValidationError("Valor monetário não pode ser negativo.")
    return round(numero, 2)


def normalizar_emprego(valor: object) -> str:
    """Mapeia o texto do cliente para ``formal``, ``autonomo`` ou ``desempregado``."""
    texto = _sem_acento(str(valor or ""))

    if any(
        chave in texto for chave in ("desempregad", "sem emprego", "sem trabalho", "nao trabalho")
    ):
        return "desempregado"
    if any(
        chave in texto
        for chave in ("autonomo", "autonoma", "mei", "freelanc", "conta propria", "pj", "informal")
    ):
        return "autonomo"
    if any(
        chave in texto
        for chave in ("formal", "clt", "carteira assinada", "registrad", "efetivo", "servidor")
    ):
        return "formal"

    raise ValidationError(
        f"Tipo de emprego não reconhecido: {valor!r}. Use formal, autônomo ou desempregado."
    )


def normalizar_dependentes(valor: object) -> int:
    """Normaliza o número de dependentes para 0..3 (``3+`` satura em 3)."""
    if isinstance(valor, bool):
        raise ValidationError("Número de dependentes não pode ser booleano.")

    if isinstance(valor, int):
        numero = valor
    elif isinstance(valor, float) and valor.is_integer():
        numero = int(valor)
    else:
        texto = _sem_acento(str(valor or ""))
        if texto.endswith("+"):
            return _MAX_DEPENDENTES
        digitos = re.search(r"\d+", texto)
        if digitos:
            numero = int(digitos.group())
        elif texto in _NUMEROS_ESCRITOS:
            numero = _NUMEROS_ESCRITOS[texto]
        else:
            raise ValidationError(f"Número de dependentes não reconhecido: {valor!r}.")

    if numero < 0:
        raise ValidationError("Número de dependentes não pode ser negativo.")
    return min(numero, _MAX_DEPENDENTES)


def normalizar_tem_dividas(valor: object) -> bool:
    """Normaliza a existência de dívidas ativas (``sim``/``não``) para booleano."""
    if isinstance(valor, bool):
        return valor

    texto = _sem_acento(str(valor or ""))
    if not texto:
        raise ValidationError("Informação sobre dívidas vazia.")

    if any(chave in texto for chave in ("nao", "nenhum", "sem ", "quite", "quitad")):
        return False
    if any(chave in texto for chave in ("sim", "tenho", "possui", "devendo", "possuo")):
        return True

    raise ValidationError(f"Resposta sobre dívidas não reconhecida: {valor!r}. Use sim ou não.")
