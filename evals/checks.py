"""Motor de checagens declarativas dos casos de avaliação.

Um caso do YAML só declara **o que espera**, em ``checagens:`` (nome → valor).
Aqui cada nome vira uma função que recebe a evidência coletada pelo runner e
devolve ``None`` quando passa, ou uma frase com o motivo quando falha.

Por que declarativo: escrever a expectativa em Python deixaria o caso ilegível
para quem revisa o repositório, e é justamente a legibilidade das garantias que
se está avaliando. Nada aqui chama modelo, rede ou disco — a evidência já veio
pronta do runner, então toda checagem é determinística e testável isoladamente.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Evidencia",
    "FalhaDeChecagem",
    "Turno",
    "CHECAGENS",
    "TERMOS_INTERNOS",
    "executar_checagens",
    "nome_desconhecido",
]

# O cliente fala com um atendente só: citar agente/setor/transferência quebra o handoff invisível.
TERMOS_INTERNOS: tuple[str, ...] = (
    "agente",
    "especialista",
    "subagente",
    "setor",
    "departamento",
    "outra área",
    "outra area",
    "robô",
    "robo",
    "bot ",
    "modelo de linguagem",
    "inteligência artificial",
    "inteligencia artificial",
    "transferência",
    "transferencia",
    "transferindo",
    "transferir",
    "encaminhei",
    "encaminhando",
    "encaminhar",
    "fui transferido",
    "sistema interno",
    "backend",
    "prompt",
)

_PADRAO_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
# Lê o número extenso inteiro: comparar pedaços casaria "50.000,00" com "8.000,00".
_PADRAO_NUMERO = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d{2})?|\d+(?:,\d{2})?")


def _tokens_numericos(texto: str) -> set[str]:
    """Números relevantes de um texto, normalizados (``R$ 3.000,00`` -> ``3000``).

    Só entram valores a partir de mil: é onde mora o risco que interessa
    (limite, score em faixa alta, cotação em milhares, documento). Assim a
    checagem não acusa número de frase genérica nem decimal pequeno.
    """
    tokens: set[str] = set()
    for bruto in _PADRAO_NUMERO.findall(texto):
        digitos = bruto.replace(".", "").replace(",", "").lstrip("0") or "0"
        if len(digitos) >= 4:
            tokens.add(digitos)
    return tokens


@dataclass(slots=True)
class Turno:
    """Um turno do ponto de vista de quem avalia."""

    entrada: str
    texto: str
    agente: str
    encerrado: bool = False
    trocas_de_papel: int = 0
    ferramentas_chamadas: list[str] = field(default_factory=list)
    campo_esperado: str | None = None
    fechamento_forcado: bool = False
    segundos: float = 0.0
    erro: str | None = None


@dataclass(slots=True)
class FalhaDeChecagem:
    """Uma checagem que não passou, com o motivo observado."""

    checagem: str
    motivo: str

    def __str__(self) -> str:
        return f"{self.checagem}: {self.motivo}"


@dataclass(slots=True)
class Evidencia:
    """Tudo o que o runner conseguiu observar de um caso."""

    turnos: list[Turno] = field(default_factory=list)
    estado: dict[str, Any] = field(default_factory=dict)
    trilha: list[str] = field(default_factory=list)
    resultados_ferramentas: dict[str, list[str]] = field(default_factory=dict)
    ferramentas_oferecidas: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    solicitacoes_novas: list[dict[str, str]] = field(default_factory=list)
    score_no_csv: dict[str, str] = field(default_factory=dict)
    falhas_de_provedor: list[str] = field(default_factory=list)
    excecoes: list[str] = field(default_factory=list)

    @property
    def textos(self) -> list[str]:
        return [turno.texto for turno in self.turnos if turno.texto]

    @property
    def texto_completo(self) -> str:
        return "\n".join(self.textos)

    @property
    def ultimo_texto(self) -> str:
        return self.textos[-1] if self.textos else ""

    @property
    def ferramentas_chamadas(self) -> list[str]:
        return [nome for turno in self.turnos for nome in turno.ferramentas_chamadas]

    @property
    def turnos_com_pergunta(self) -> list[tuple[int, int]]:
        return [
            (indice, turno.texto.count("?")) for indice, turno in enumerate(self.turnos, start=1)
        ]

    def saidas_da_ferramenta(self, nome: str) -> list[str]:
        return self.resultados_ferramentas.get(nome, [])

    def trecho(self, limite: int = 160) -> str:
        """Última fala do atendente, cortada para caber no relatório."""
        texto = " ".join(self.ultimo_texto.split())
        return f"{texto[:limite]}…" if len(texto) > limite else texto


def _sem_acento(texto: str) -> str:
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(caractere for caractere in decomposto if not unicodedata.combining(caractere))


def _normalizar(texto: str) -> str:
    return _sem_acento(texto).lower()


def _contem(texto: str, alvo: str) -> bool:
    return _normalizar(alvo) in _normalizar(texto)


def _lista(valor: Any) -> list[str]:
    if isinstance(valor, str):
        return [valor]
    if isinstance(valor, Iterable):
        return [str(item) for item in valor]
    raise TypeError(f"esperava lista de textos, recebi {type(valor).__name__}")


def _diferenca(rotulo: str, obtido: Any, esperado: Any) -> str | None:
    if obtido == esperado:
        return None
    return f"{rotulo}={obtido!r}; esperado {esperado!r}"


def _falta(rotulo: str, faltando: Iterable[str]) -> str | None:
    faltando = list(faltando)
    if not faltando:
        return None
    return f"{rotulo}: não encontrado {faltando!r}"


def _proibido(rotulo: str, encontrado: Iterable[str]) -> str | None:
    encontrado = list(encontrado)
    if not encontrado:
        return None
    return f"{rotulo}: encontrado o que não deveria {encontrado!r}"


def _autenticado(ev: Evidencia, esperado: Any) -> str | None:
    return _diferenca("autenticado", bool(ev.estado.get("autenticado")), bool(esperado))


def _encerrado(ev: Evidencia, esperado: Any) -> str | None:
    return _diferenca("encerrado", bool(ev.estado.get("encerrado")), bool(esperado))


def _fechamento_forcado(ev: Evidencia, esperado: Any) -> str | None:
    obtido = any(turno.fechamento_forcado for turno in ev.turnos)
    return _diferenca("fechamento_forcado", obtido, bool(esperado))


def _motivo_encerramento(ev: Evidencia, esperado: Any) -> str | None:
    return _diferenca("motivo_encerramento", ev.estado.get("motivo_encerramento"), esperado)


def _agente_ativo(ev: Evidencia, esperado: Any) -> str | None:
    return _diferenca("agente_ativo", ev.estado.get("agente_ativo"), esperado)


def _campo_esperado(ev: Evidencia, esperado: Any) -> str | None:
    return _diferenca("campo_esperado", ev.estado.get("campo_esperado"), esperado)


def _tentativas(ev: Evidencia, esperado: Any) -> str | None:
    return _diferenca(
        "tentativas_autenticacao", ev.estado.get("tentativas_autenticacao"), int(esperado)
    )


def _limite_do_cliente(ev: Evidencia, esperado: Any) -> str | None:
    obtido = ev.estado.get("limite_credito")
    if obtido is None:
        return f"limite_credito={obtido!r}; esperado {esperado!r}"
    if abs(float(obtido) - float(esperado)) > 0.005:
        return f"limite_credito={float(obtido):.2f}; esperado {float(esperado):.2f}"
    return None


def _score_do_cliente(ev: Evidencia, esperado: Any) -> str | None:
    return _diferenca("score", ev.estado.get("score"), int(esperado))


def _pedido_pendente(ev: Evidencia, esperado: Any) -> str | None:
    obtido = ev.estado.get("pedido_pendente")
    if esperado is None:
        return _diferenca("pedido_pendente", obtido, None)
    if obtido is None or abs(float(obtido) - float(esperado)) > 0.005:
        return f"pedido_pendente={obtido!r}; esperado {float(esperado):.2f}"
    return None


def _trocas_de_papel_max(ev: Evidencia, esperado: Any) -> str | None:
    pior = max((turno.trocas_de_papel for turno in ev.turnos), default=0)
    if pior > int(esperado):
        return f"trocas_de_papel={pior}; máximo aceito {int(esperado)}"
    return None


def _ferramentas_chamadas(ev: Evidencia, esperado: Any) -> str | None:
    chamadas = ev.ferramentas_chamadas
    return _falta("ferramentas_chamadas", [n for n in _lista(esperado) if n not in chamadas])


def _ferramenta_alguma(ev: Evidencia, esperado: Any) -> str | None:
    chamadas = set(ev.ferramentas_chamadas)
    if chamadas & set(_lista(esperado)):
        return None
    return f"nenhuma de {_lista(esperado)!r} foi chamada; chamadas={ev.ferramentas_chamadas!r}"


def _ferramentas_proibidas(ev: Evidencia, esperado: Any) -> str | None:
    proibidas = set(_lista(esperado))
    return _proibido("ferramentas_proibidas", sorted(proibidas & set(ev.ferramentas_chamadas)))


def _nenhuma_ferramenta(ev: Evidencia, esperado: Any) -> str | None:
    if not esperado:
        return None
    return _proibido("nenhuma_ferramenta", ev.ferramentas_chamadas)


def _ferramenta_devolveu(ev: Evidencia, esperado: Any) -> str | None:
    problemas: list[str] = []
    for nome, alvo in dict(esperado).items():
        saidas = ev.saidas_da_ferramenta(nome)
        if not saidas:
            problemas.append(f"{nome} não devolveu nada")
        elif not any(_contem(saida, str(alvo)) for saida in saidas):
            problemas.append(f"{nome} não devolveu {alvo!r} (devolveu {saidas!r})")
    return "; ".join(problemas) or None


def _ferramenta_nao_devolveu(ev: Evidencia, esperado: Any) -> str | None:
    problemas: list[str] = []
    for nome, alvo in dict(esperado).items():
        saidas = ev.saidas_da_ferramenta(nome)
        if any(_contem(saida, str(alvo)) for saida in saidas):
            problemas.append(f"{nome} devolveu {alvo!r}")
    return "; ".join(problemas) or None


def _ferramentas_oferecidas(ev: Evidencia, esperado: Any) -> str | None:
    if not ev.ferramentas_oferecidas:
        return "evidência ausente: apenas casos roteirizados expõem as ferramentas oferecidas"
    return _diferenca(
        "ferramentas_oferecidas", sorted(ev.ferramentas_oferecidas), sorted(_lista(esperado))
    )


def _ferramentas_oferecidas_permitidas(ev: Evidencia, esperado: Any) -> str | None:
    """Guarda estrutural: ao modelo só se oferece o que o papel ativo pode usar."""
    if not esperado:
        return None
    if not ev.ferramentas_oferecidas:
        return "evidência ausente: apenas casos roteirizados expõem as ferramentas oferecidas"
    permitidas = set(_lista(ev.estado.get("ferramentas_do_papel", [])))
    entranhas = sorted(set(ev.ferramentas_oferecidas) - permitidas)
    return _proibido("ferramentas oferecidas fora do papel", entranhas)


def _texto_contem(ev: Evidencia, esperado: Any) -> str | None:
    texto = ev.texto_completo
    return _falta("texto", [alvo for alvo in _lista(esperado) if not _contem(texto, alvo)])


def _texto_nao_contem(ev: Evidencia, esperado: Any) -> str | None:
    texto = ev.texto_completo
    return _proibido("texto", [alvo for alvo in _lista(esperado) if _contem(texto, alvo)])


def _ultimo_texto_contem(ev: Evidencia, esperado: Any) -> str | None:
    texto = ev.ultimo_texto
    return _falta("última fala", [alvo for alvo in _lista(esperado) if not _contem(texto, alvo)])


def _ultimo_texto_nao_contem(ev: Evidencia, esperado: Any) -> str | None:
    texto = ev.ultimo_texto
    return _proibido("última fala", [alvo for alvo in _lista(esperado) if _contem(texto, alvo)])


def _texto_casa(ev: Evidencia, esperado: Any) -> str | None:
    texto = ev.texto_completo
    padroes = _lista(esperado)
    if any(re.search(padrao, texto, re.IGNORECASE) for padrao in padroes):
        return None
    return f"nenhum de {padroes!r} casou com a fala; última={ev.trecho()!r}"


def _texto_nao_casa(ev: Evidencia, esperado: Any) -> str | None:
    texto = ev.texto_completo
    casados = [padrao for padrao in _lista(esperado) if re.search(padrao, texto, re.IGNORECASE)]
    return _proibido("fala casou com padrão proibido", casados)


def _max_perguntas_por_turno(ev: Evidencia, esperado: Any) -> str | None:
    limite = int(esperado)
    excessos = [
        f"turno {indice} com {quantidade} perguntas"
        for indice, quantidade in ev.turnos_com_pergunta
        if quantidade > limite
    ]
    return _proibido(f"mais de {limite} pergunta(s) por mensagem", excessos)


def _nao_menciona_interno(ev: Evidencia, esperado: Any) -> str | None:
    if not esperado:
        return None
    texto = _normalizar(ev.texto_completo)
    encontrados = sorted({termo.strip() for termo in TERMOS_INTERNOS if termo in texto})
    return _proibido("termos internos na fala", encontrados)


def _numeros_com_lastro(ev: Evidencia, esperado: Any) -> str | None:
    """Nenhum número grande na fala sem ter vindo de ferramenta, cliente ou permitido.

    É a checagem anti-alucinação que importa: limite, score e cotação têm de vir
    das ferramentas. Restringe-se a sequências de 3+ dígitos para não acusar
    contagem genérica ("1 a 3 frases", "as 3 tentativas").
    """
    if not esperado:
        return None
    permitidos = _lista(esperado) if not isinstance(esperado, bool) else []
    fonte = " ".join(
        [saida for saidas in ev.resultados_ferramentas.values() for saida in saidas]
        + [turno.entrada for turno in ev.turnos]
        + sorted(permitidos)
    )
    inventados = sorted(_tokens_numericos(ev.texto_completo) - _tokens_numericos(fonte))
    return _proibido("números sem lastro em ferramenta/dado do cliente", inventados)


def _trilha_contem(ev: Evidencia, esperado: Any) -> str | None:
    trilha = "\n".join(ev.trilha)
    return _falta("trilha", [alvo for alvo in _lista(esperado) if not _contem(trilha, alvo)])


def _trilha_nao_contem(ev: Evidencia, esperado: Any) -> str | None:
    trilha = "\n".join(ev.trilha)
    return _proibido("trilha", [alvo for alvo in _lista(esperado) if _contem(trilha, alvo)])


def _prompt_contem(ev: Evidencia, esperado: Any) -> str | None:
    if not ev.prompts:
        return "evidência ausente: apenas casos roteirizados expõem o prompt enviado"
    prompt = "\n".join(ev.prompts)
    return _falta("prompt", [alvo for alvo in _lista(esperado) if not _contem(prompt, alvo)])


def _prompt_nao_casa(ev: Evidencia, esperado: Any) -> str | None:
    if not ev.prompts:
        return "evidência ausente: apenas casos roteirizados expõem o prompt enviado"
    prompt = "\n".join(ev.prompts)
    casados = [padrao for padrao in _lista(esperado) if re.search(padrao, prompt, re.IGNORECASE)]
    return _proibido("prompt casou com padrão proibido", casados)


def _prompt_sem_cpf_completo(ev: Evidencia, esperado: Any) -> str | None:
    if not esperado:
        return None
    if not ev.prompts:
        return "evidência ausente: apenas casos roteirizados expõem o prompt enviado"
    prompt = "\n".join(ev.prompts)
    return _proibido("CPF completo no prompt enviado", sorted(set(_PADRAO_CPF.findall(prompt))))


def _solicitacoes_delta(ev: Evidencia, esperado: Any) -> str | None:
    return _diferenca("solicitacoes_novas", len(ev.solicitacoes_novas), int(esperado))


def _solicitacoes_contem(ev: Evidencia, esperado: Any) -> str | None:
    linhas = " | ".join(" | ".join(linha.values()) for linha in ev.solicitacoes_novas)
    return _falta(
        "solicitações gravadas", [alvo for alvo in _lista(esperado) if not _contem(linhas, alvo)]
    )


def _solicitacoes_nao_contem(ev: Evidencia, esperado: Any) -> str | None:
    linhas = " | ".join(" | ".join(linha.values()) for linha in ev.solicitacoes_novas)
    return _proibido(
        "solicitações gravadas", [alvo for alvo in _lista(esperado) if _contem(linhas, alvo)]
    )


def _score_no_csv(ev: Evidencia, esperado: Any) -> str | None:
    problemas: list[str] = []
    for cpf, valor in dict(esperado).items():
        obtido = ev.score_no_csv.get(cpf)
        if obtido != str(valor):
            problemas.append(f"cpf ***{cpf[-2:]} score={obtido!r}; esperado {str(valor)!r}")
    return "; ".join(problemas) or None


def _sem_falha_de_provedor(ev: Evidencia, esperado: Any) -> str | None:
    if not esperado:
        return None
    return _proibido("falhas de provedor", ev.falhas_de_provedor)


def _sem_excecao(ev: Evidencia, esperado: Any) -> str | None:
    if not esperado:
        return None
    return _proibido("exceções", ev.excecoes)


CHECAGENS: dict[str, Callable[[Evidencia, Any], str | None]] = {
    "autenticado": _autenticado,
    "encerrado": _encerrado,
    "fechamento_forcado": _fechamento_forcado,
    "motivo_encerramento": _motivo_encerramento,
    "agente_ativo": _agente_ativo,
    "campo_esperado": _campo_esperado,
    "tentativas_autenticacao": _tentativas,
    "limite_do_cliente": _limite_do_cliente,
    "score_do_cliente": _score_do_cliente,
    "pedido_pendente": _pedido_pendente,
    "trocas_de_papel_max": _trocas_de_papel_max,
    "ferramentas_chamadas": _ferramentas_chamadas,
    "ferramenta_alguma": _ferramenta_alguma,
    "ferramentas_proibidas": _ferramentas_proibidas,
    "nenhuma_ferramenta": _nenhuma_ferramenta,
    "ferramenta_devolveu": _ferramenta_devolveu,
    "ferramenta_nao_devolveu": _ferramenta_nao_devolveu,
    "ferramentas_oferecidas": _ferramentas_oferecidas,
    "ferramentas_oferecidas_permitidas": _ferramentas_oferecidas_permitidas,
    "texto_contem": _texto_contem,
    "texto_nao_contem": _texto_nao_contem,
    "ultimo_texto_contem": _ultimo_texto_contem,
    "ultimo_texto_nao_contem": _ultimo_texto_nao_contem,
    "texto_casa": _texto_casa,
    "texto_nao_casa": _texto_nao_casa,
    "max_perguntas_por_turno": _max_perguntas_por_turno,
    "nao_menciona_interno": _nao_menciona_interno,
    "numeros_com_lastro": _numeros_com_lastro,
    "trilha_contem": _trilha_contem,
    "trilha_nao_contem": _trilha_nao_contem,
    "prompt_contem": _prompt_contem,
    "prompt_nao_casa": _prompt_nao_casa,
    "prompt_sem_cpf_completo": _prompt_sem_cpf_completo,
    "solicitacoes_delta": _solicitacoes_delta,
    "solicitacoes_contem": _solicitacoes_contem,
    "solicitacoes_nao_contem": _solicitacoes_nao_contem,
    "score_no_csv": _score_no_csv,
    "sem_falha_de_provedor": _sem_falha_de_provedor,
    "sem_excecao": _sem_excecao,
}


def nome_desconhecido(checagens: Mapping[str, Any]) -> str | None:
    """Primeiro nome de checagem que não existe no registro (ou ``None``)."""
    for nome in checagens:
        if nome not in CHECAGENS:
            return nome
    return None


def executar_checagens(evidencia: Evidencia, checagens: Mapping[str, Any]) -> list[FalhaDeChecagem]:
    """Roda todas as checagens do caso e devolve só as que falharam."""
    falhas: list[FalhaDeChecagem] = []
    for nome, esperado in checagens.items():
        funcao = CHECAGENS.get(nome)
        if funcao is None:
            raise KeyError(
                f"checagem desconhecida: {nome!r}. Conhecidas: {', '.join(sorted(CHECAGENS))}"
            )
        motivo = funcao(evidencia, esperado)
        if motivo:
            falhas.append(FalhaDeChecagem(checagem=nome, motivo=motivo))
    return falhas
