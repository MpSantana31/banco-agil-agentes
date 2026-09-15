"""Testes do serviço de câmbio — rede mockada com respx, zero chamada real."""

from __future__ import annotations

import httpx
import pytest
import respx

from banco_agil.errors import ExternalAPIError, ValidationError
from banco_agil.tools.fx import ServicoCambio, normalizar_moeda

AWESOME = "https://economia.awesomeapi.com.br/last/USD-BRL"
ER_API = "https://open.er-api.com/v6/latest/BRL"
FRANKFURTER = "https://api.frankfurter.app/latest"

RESPOSTA_AWESOME = {
    "USDBRL": {
        "bid": "5.4321",
        "ask": "5.4333",
        "name": "Dólar Americano/Real Brasileiro",
        "create_date": "2026-09-14 12:00:00",
    }
}
RESPOSTA_ER_API = {
    "result": "success",
    "time_last_update_utc": "Mon, 14 Sep 2026 00:02:31 +0000",
    "rates": {"USD": 0.184, "EUR": 0.158},
}
RESPOSTA_FRANKFURTER = {"amount": 1.0, "base": "USD", "date": "2026-09-13", "rates": {"BRL": 5.4}}


def _servico(**kwargs: object) -> ServicoCambio:
    padrao: dict[str, object] = {"timeout": 2.0, "cache_ttl": 300}
    padrao.update(kwargs)
    return ServicoCambio(**padrao)  # type: ignore[arg-type]


class TestNormalizacaoDeMoeda:
    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            ("USD", "USD"),
            ("usd", "USD"),
            ("dólar", "USD"),
            ("dolares", "USD"),
            ("Dólar americano", "USD"),
            ("euro", "EUR"),
            ("EUR", "EUR"),
            ("libra", "GBP"),
            ("iene", "JPY"),
            ("bitcoin", "BTC"),
            ("dólar canadense", "CAD"),
            ("peso argentino", "ARS"),
        ],
    )
    def test_reconhece_codigo_e_apelido(self, entrada: str, esperado: str) -> None:
        assert normalizar_moeda(entrada) == esperado

    def test_moeda_desconhecida_levanta(self) -> None:
        with pytest.raises(ValidationError):
            normalizar_moeda("moeda do reino de aparte")

    def test_vazio_levanta(self) -> None:
        with pytest.raises(ValidationError):
            normalizar_moeda("")


class TestCascataDeProvedores:
    @respx.mock
    def test_primeiro_provedor_responde(self) -> None:
        respx.get(AWESOME).mock(return_value=httpx.Response(200, json=RESPOSTA_AWESOME))
        cotacao = _servico().cotacao("dólar")

        assert cotacao.moeda == "USD"
        assert cotacao.valor == pytest.approx(5.4321)
        assert cotacao.fonte.startswith("AwesomeAPI")
        assert cotacao.atualizado_em == "2026-09-14 12:00:00"

    @respx.mock
    def test_usa_fallback_quando_o_primeiro_falha_http(self) -> None:
        respx.get(AWESOME).mock(return_value=httpx.Response(503))
        respx.get(ER_API).mock(return_value=httpx.Response(200, json=RESPOSTA_ER_API))

        cotacao = _servico().cotacao("USD")

        assert cotacao.fonte == "open.er-api.com"
        assert cotacao.valor == pytest.approx(1 / 0.184, rel=1e-4)

    @respx.mock
    def test_usa_terceiro_provedor_quando_os_dois_primeiros_falham(self) -> None:
        respx.get(AWESOME).mock(side_effect=httpx.ConnectTimeout("timeout"))
        respx.get(ER_API).mock(return_value=httpx.Response(500, text="erro"))
        respx.get(FRANKFURTER).mock(return_value=httpx.Response(200, json=RESPOSTA_FRANKFURTER))

        cotacao = _servico().cotacao("dolar")

        assert cotacao.fonte == "Frankfurter (BCE)"
        assert cotacao.valor == pytest.approx(5.4)

    @respx.mock
    def test_todos_falhando_levanta_erro_externo(self) -> None:
        respx.get(AWESOME).mock(return_value=httpx.Response(500))
        respx.get(ER_API).mock(return_value=httpx.Response(500))
        respx.get(FRANKFURTER).mock(side_effect=httpx.ConnectError("sem rede"))

        servico = _servico()
        with pytest.raises(ExternalAPIError) as exc:
            servico.cotacao("USD")

        assert "USD" in str(exc.value)
        assert len(servico.falhas_registradas) == 3

    @respx.mock
    def test_resposta_em_formato_inesperado_nao_quebra_a_cascata(self) -> None:
        respx.get(AWESOME).mock(return_value=httpx.Response(200, json={"algo": "inesperado"}))
        respx.get(ER_API).mock(return_value=httpx.Response(200, json=RESPOSTA_ER_API))

        assert _servico().cotacao("USD").fonte == "open.er-api.com"


class TestCache:
    @respx.mock
    def test_segunda_consulta_nao_bate_na_api(self) -> None:
        rota = respx.get(AWESOME).mock(return_value=httpx.Response(200, json=RESPOSTA_AWESOME))
        servico = _servico()

        primeira = servico.cotacao("USD")
        segunda = servico.cotacao("dolar")

        assert primeira == segunda
        assert rota.call_count == 1

    @respx.mock
    def test_ttl_expirado_consulta_de_novo(self) -> None:
        rota = respx.get(AWESOME).mock(return_value=httpx.Response(200, json=RESPOSTA_AWESOME))
        instante = {"agora": 0.0}
        servico = _servico(cache_ttl=60, relogio=lambda: instante["agora"])

        servico.cotacao("USD")
        instante["agora"] = 61.0
        servico.cotacao("USD")

        assert rota.call_count == 2

    @respx.mock
    def test_limpar_cache_forca_nova_consulta(self) -> None:
        rota = respx.get(AWESOME).mock(return_value=httpx.Response(200, json=RESPOSTA_AWESOME))
        servico = _servico()

        servico.cotacao("USD")
        servico.limpar_cache()
        servico.cotacao("USD")

        assert rota.call_count == 2

    @respx.mock
    def test_cache_desligado_consulta_sempre(self) -> None:
        rota = respx.get(AWESOME).mock(return_value=httpx.Response(200, json=RESPOSTA_AWESOME))
        servico = _servico(cache_ttl=0)

        servico.cotacao("USD")
        servico.cotacao("USD")

        assert rota.call_count == 2


class TestConfiguracao:
    def test_sem_provedores_levanta(self) -> None:
        with pytest.raises(ExternalAPIError):
            ServicoCambio([])

    @respx.mock
    def test_provedor_customizado_e_usado(self) -> None:
        from banco_agil.tools.fx import ProvedorCotacao

        def so_er_api(cliente: httpx.Client, moeda: str) -> tuple[float, str]:
            resposta = cliente.get(ER_API)
            return 1 / resposta.json()["rates"][moeda], "agora"

        respx.get(ER_API).mock(return_value=httpx.Response(200, json=RESPOSTA_ER_API))
        servico = _servico(provedores=[ProvedorCotacao("custom", so_er_api)])

        assert servico.cotacao("EUR").fonte == "custom"
