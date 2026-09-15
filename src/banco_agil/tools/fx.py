"""Consulta de cotação de moedas em APIs externas (sem chave de API).

Escolha de provedores: o enunciado sugere Tavily/SerpAPI, mas são APIs de
*busca* — para cotação elas custam caro, chegam com atraso de indexação e
devolvem texto a interpretar. Aqui usamos três provedores gratuitos de cotação,
consultados em cascata com cache em memória:

1. AwesomeAPI (Banco Central) — cotação BRL por moeda, sem chave;
2. open.er-api.com — câmbio do dia, sem chave;
3. Frankfurter (BCE) — fallback institucional.

Todos atrás da mesma interface: trocar/adicionar provedor é adicionar uma
função a ``PROVEDORES_PADRAO``.
"""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx

from banco_agil.domain.modelos import Cotacao
from banco_agil.errors import ExternalAPIError, ValidationError

__all__ = ["ServicoCambio", "normalizar_moeda", "MOEDAS_SUPORTADAS", "PROVEDORES_PADRAO"]

MOEDAS_SUPORTADAS: dict[str, tuple[str, tuple[str, ...]]] = {
    "USD": ("Dólar americano", ("usd", "dolar", "dolares", "dollar", "dollars")),
    "EUR": ("Euro", ("eur", "euro", "euros")),
    "GBP": ("Libra esterlina", ("gbp", "libra", "libras", "pound")),
    "JPY": ("Iene japonês", ("jpy", "iene", "ienes", "yen")),
    "ARS": ("Peso argentino", ("ars", "peso argentino", "pesos argentinos")),
    "CAD": ("Dólar canadense", ("cad", "dolar canadense", "dolar canadiano")),
    "AUD": ("Dólar australiano", ("aud", "dolar australiano")),
    "CHF": ("Franco suíço", ("chf", "franco suico", "franco suico")),
    "BTC": ("Bitcoin", ("btc", "bitcoin")),
}


def _sem_acento(texto: str) -> str:
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in decomposto if not unicodedata.combining(c)).strip().lower()


def normalizar_moeda(valor: object) -> str:
    """Mapeia "dólar", "USD", "dolares"… para o código ISO (``USD``)."""
    texto = _sem_acento(str(valor or "")).replace("$", "").strip()
    if not texto:
        raise ValidationError("Nenhuma moeda informada.")

    codigo = texto.upper()
    if codigo in MOEDAS_SUPORTADAS:
        return codigo

    # Apelidos mais longos primeiro: "dolar canadense" não pode casar com "dolar".
    apelidos = sorted(
        ((apelido, iso) for iso, (_, lista) in MOEDAS_SUPORTADAS.items() for apelido in lista),
        key=lambda par: len(par[0]),
        reverse=True,
    )
    for apelido, iso in apelidos:
        if apelido in texto:
            return iso

    raise ValidationError(
        f"Moeda não reconhecida: {valor!r}. Disponíveis: {', '.join(sorted(MOEDAS_SUPORTADAS))}."
    )


@dataclass(frozen=True, slots=True)
class ProvedorCotacao:
    """Provedor de cotação: nome + função que devolve (valor em BRL, data)."""

    nome: str
    buscar: Callable[[httpx.Client, str], tuple[float, str]]


def _buscar_awesomeapi(cliente: httpx.Client, moeda: str) -> tuple[float, str]:
    resposta = cliente.get(f"https://economia.awesomeapi.com.br/last/{moeda}-BRL")
    resposta.raise_for_status()
    dados = resposta.json()
    chave = f"{moeda}BRL"
    if chave not in dados:
        raise ExternalAPIError(f"AwesomeAPI não devolveu {chave}.")
    cotacao = dados[chave]
    return float(cotacao["bid"]), str(cotacao.get("create_date", ""))


def _buscar_er_api(cliente: httpx.Client, moeda: str) -> tuple[float, str]:
    resposta = cliente.get("https://open.er-api.com/v6/latest/BRL")
    resposta.raise_for_status()
    dados = resposta.json()
    taxa = dados.get("rates", {}).get(moeda)
    if not taxa:
        raise ExternalAPIError(f"open.er-api.com não devolveu {moeda}.")
    return 1 / float(taxa), str(dados.get("time_last_update_utc", ""))


def _buscar_frankfurter(cliente: httpx.Client, moeda: str) -> tuple[float, str]:
    resposta = cliente.get(
        "https://api.frankfurter.app/latest",
        params={"from": moeda, "to": "BRL"},
    )
    resposta.raise_for_status()
    dados = resposta.json()
    taxa = dados.get("rates", {}).get("BRL")
    if not taxa:
        raise ExternalAPIError(f"Frankfurter não devolveu BRL para {moeda}.")
    return float(taxa), str(dados.get("date", ""))


PROVEDORES_PADRAO: tuple[ProvedorCotacao, ...] = (
    ProvedorCotacao("AwesomeAPI (Banco Central)", _buscar_awesomeapi),
    ProvedorCotacao("open.er-api.com", _buscar_er_api),
    ProvedorCotacao("Frankfurter (BCE)", _buscar_frankfurter),
)


class ServicoCambio:
    """Cotação com cascata de provedores e cache em memória (TTL configurável)."""

    def __init__(
        self,
        provedores: Sequence[ProvedorCotacao] | None = None,
        *,
        timeout: float = 8.0,
        cache_ttl: int = 300,
        cliente_http: httpx.Client | None = None,
        relogio: Callable[[], float] | None = None,
    ) -> None:
        self._provedores = tuple(PROVEDORES_PADRAO if provedores is None else provedores)
        if not self._provedores:
            raise ExternalAPIError("Serviço de câmbio sem provedores configurados.")
        self._timeout = timeout
        self._cache_ttl = max(0, cache_ttl)
        self._cliente = cliente_http
        self._relogio = relogio or time.monotonic
        self._cache: dict[str, tuple[float, Cotacao]] = {}
        self._falhas: list[str] = []

    def cotacao(self, moeda: object) -> Cotacao:
        """Cotação atual em BRL. Levanta ``ExternalAPIError`` se todos falharem."""
        codigo = normalizar_moeda(moeda)

        em_cache = self._do_cache(codigo)
        if em_cache is not None:
            return em_cache

        falhas: list[str] = []
        for provedor in self._provedores:
            try:
                valor, atualizado_em = self._consultar(provedor, codigo)
            except (ExternalAPIError, httpx.HTTPError, ValueError, KeyError) as erro:
                falhas.append(f"{provedor.nome}: {type(erro).__name__}")
                self._registrar_falha(f"{provedor.nome}: {erro}")
                continue

            cotacao = Cotacao(
                moeda=codigo,
                nome=MOEDAS_SUPORTADAS[codigo][0],
                valor=round(valor, 4),
                atualizado_em=atualizado_em,
                fonte=provedor.nome,
            )
            self._guardar_no_cache(codigo, cotacao)
            return cotacao

        raise ExternalAPIError(
            f"Nenhum provedor de cotação respondeu para {codigo} ({'; '.join(falhas)})."
        )

    def limpar_cache(self) -> None:
        """Descarta o cache (usado pelos testes e pela UI ao forçar atualização)."""
        self._cache.clear()

    @property
    def falhas_registradas(self) -> tuple[str, ...]:
        """Últimas falhas por provedor (diagnóstico; a conversa não vê isso)."""
        return tuple(self._falhas)

    def _consultar(self, provedor: ProvedorCotacao, codigo: str) -> tuple[float, str]:
        if self._cliente is not None:
            return provedor.buscar(self._cliente, codigo)
        with httpx.Client(timeout=self._timeout) as cliente:
            return provedor.buscar(cliente, codigo)

    def _do_cache(self, codigo: str) -> Cotacao | None:
        registro = self._cache.get(codigo)
        if registro is None:
            return None
        expira_em, cotacao = registro
        if self._relogio() >= expira_em:
            del self._cache[codigo]
            return None
        return cotacao

    def _guardar_no_cache(self, codigo: str, cotacao: Cotacao) -> None:
        if self._cache_ttl <= 0:
            return
        self._cache[codigo] = (self._relogio() + self._cache_ttl, cotacao)

    def _registrar_falha(self, mensagem: str) -> None:
        self._falhas.append(mensagem)
        del self._falhas[:-20]  # mantém só as últimas: é diagnóstico, não histórico
