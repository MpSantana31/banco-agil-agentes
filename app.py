"""Interface web (Streamlit) do Banco Ágil.

Duas abas:

- **Atendimento**: o cliente vê só a conversa (o handoff é invisível). A barra
  lateral mostra a cadeia de modelos ativa e quantas vezes houve fallback.
- **Configurações**: escolha dos modelos primário/secundário/terciário, chaves
  por provedor, teste de conexão e gravação no `.env` — esta última **só com
  confirmação explícita**, porque sobrescreve credenciais.

Uso:
    uv run streamlit run app.py
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any

import streamlit as st

from banco_agil.config import PROVEDORES_SUPORTADOS, Settings, obter_settings
from banco_agil.env_store import gravar_env, ler_env, mascarar_valor, planejar_alteracoes
from banco_agil.errors import BancoAgilError, ConfirmacaoNecessaria, ValidationError
from banco_agil.llm.catalogo import MODELOS_CONHECIDOS, ORIGEM_API, listar_modelos, testar_passo
from banco_agil.llm.corrente import CorrenteDeModelos, PassoDaCadeia
from banco_agil.llm.factory import construir_cadeia, passos_da_cadeia
from banco_agil.observability.log import configurar_logging
from banco_agil.orchestrator.atendimento import Atendimento
from banco_agil.orchestrator.campos import (
    CampoEsperado,
    campo_esperado,
    dados_visiveis,
)
from banco_agil.orchestrator.estado import EstadoSessao
from banco_agil.servicos import Servicos, construir_servicos
from banco_agil.tools.validators import validar_cpf

RAIZ = Path(__file__).resolve().parent
CAMINHO_ENV = Path(os.environ.get("BANCO_AGIL_ENV", RAIZ / ".env"))
SLOTS = ("primário", "secundário", "terciário")


def _servicos(settings: Settings) -> Servicos:
    return construir_servicos(settings)


def _montar_atendimento(settings: Settings, servicos: Servicos) -> tuple[Any, Atendimento]:
    """Cria a cadeia de modelos e o atendimento, guardando quem respondeu o quê."""
    corrente = construir_cadeia(settings)
    return corrente, Atendimento(corrente, servicos)


def _rotulo_da_cadeia(corrente: Any, settings: Settings) -> str:
    if isinstance(corrente, CorrenteDeModelos):
        return " → ".join(passo.rotulo for passo in corrente.passos)
    passos = passos_da_cadeia(settings)
    return passos[0].rotulo


def _linhas_da_cadeia() -> list[str]:
    return [
        f"{indice + 1}. {passo.rotulo}"
        for indice, passo in enumerate(passos_da_cadeia(obter_settings()))
    ]


def _enviar(atendimento: Atendimento, mensagem: str) -> None:
    """Manda uma fala do cliente para o atendimento e guarda a resposta.

    Usado pelos dois caminhos — texto livre e campo dedicado — para que a conversa
    siga idêntica, venha de onde vier.
    """
    st.session_state["historico"].append(("user", mensagem))
    with st.chat_message("user"):
        st.write(mensagem)

    with st.spinner("O atendente está digitando…"):
        resposta = atendimento.responder(mensagem)
    st.session_state["historico"].append(("assistant", resposta.texto))
    with st.chat_message("assistant"):
        st.write(resposta.texto)
    st.session_state["trocas"].extend(resposta.ferramentas_chamadas)


def _sem_markdown(texto: str) -> str:
    """Escapa o que o Streamlit leria como markdown — a máscara de CPF usa ``***``."""
    return texto.replace("*", "\\*")


def _painel_do_cliente(estado: EstadoSessao) -> None:
    """Cartão com os dados do cliente — renderiza **só** depois de autenticado."""
    dados = dados_visiveis(estado)
    if dados is None:
        return

    with st.container(border=True):
        colunas = st.columns([3, 1, 1])
        colunas[0].markdown(
            f"**{_sem_markdown(dados.nome)}**  \n"
            f"CPF {_sem_markdown(dados.cpf_mascarado)} · faixa {dados.faixa}"
        )
        colunas[1].metric("Limite atual", dados.limite)
        colunas[2].metric("Score", dados.score)


def _campo_do_turno(atendimento: Atendimento) -> None:
    """Campo dedicado para o dado que a máquina de estados está esperando.

    O texto livre continua disponível na barra de mensagem: o campo é um atalho,
    nunca uma porta fechada.
    """
    campo = campo_esperado(atendimento.estado)

    if campo is CampoEsperado.CPF:
        with st.container(border=True):
            st.caption("Campo de CPF — o dígito é conferido aqui, sem gastar tentativa")
            valor = st.text_input("CPF", placeholder="000.000.000-00", key="campo_cpf")
            if st.button("Confirmar CPF", key="botao_cpf"):
                try:
                    cpf = validar_cpf(valor)
                except ValidationError as erro:
                    st.error(f"{_sem_markdown(str(erro))} Confira os números e tente de novo.")
                else:
                    _enviar(atendimento, cpf)
                    st.rerun()

    elif campo is CampoEsperado.DATA_NASCIMENTO:
        hoje = dt.date.today()
        with st.container(border=True):
            st.caption("Calendário — sem erro de formato de data")
            escolhida = st.date_input(
                "Data de nascimento",
                value=None,
                min_value=dt.date(1900, 1, 1),
                max_value=hoje,
                format="DD/MM/YYYY",
                key="campo_data",
            )
            if st.button("Confirmar data", key="botao_data"):
                if escolhida is None:
                    st.error("Escolha a data no calendário.")
                else:
                    _enviar(atendimento, escolhida.strftime("%d/%m/%Y"))
                    st.rerun()


def aba_atendimento() -> None:
    """Conversa com o cliente + painel de auditoria."""
    settings = obter_settings()
    # Sem isto, falha de provedor/ferramenta só aparece no stderr do processo.
    configurar_logging(settings.caminho_erros, nivel=settings.log_level)

    with st.sidebar:
        st.header("Atendimento")
        st.caption("Cadeia de modelos (primário → fallback)")
        for linha in _linhas_da_cadeia():
            st.write(linha)
        if st.button("Reiniciar conversa", use_container_width=True):
            st.session_state.pop("atendimento", None)
            st.rerun()

    try:
        servicos = _servicos(settings)
    except BancoAgilError as erro:
        st.error(f"Falha ao carregar a base de dados: {erro}")
        return

    if "atendimento" not in st.session_state:
        try:
            # Primeiro turno chama o modelo: sem o aviso a tela fica em branco por segundos.
            with st.spinner("Conectando ao modelo…"):
                corrente, atendimento = _montar_atendimento(settings, servicos)
                abertura = atendimento.abrir()
        except BancoAgilError as erro:
            st.error(str(erro))
            st.info(
                "Confira a aba **Configurações**: escolha um provedor, informe a chave "
                "e use *Testar conexão*."
            )
            return
        st.session_state["atendimento"] = atendimento
        st.session_state["corrente"] = corrente
        st.session_state["historico"] = [("assistant", abertura.texto)]
        st.session_state["trocas"] = []

    atendimento: Atendimento = st.session_state["atendimento"]

    _painel_do_cliente(atendimento.estado)

    for papel, texto in st.session_state.get("historico", []):
        with st.chat_message(papel):
            st.write(texto)

    _campo_do_turno(atendimento)

    mensagem = st.chat_input("Escreva sua mensagem…")
    if mensagem:
        _enviar(atendimento, mensagem)
        st.rerun()

    estado = atendimento.estado
    corrente = st.session_state.get("corrente")
    trocas_de_modelo = getattr(corrente, "trocas", 0)

    with st.expander("Painel de auditoria (o que o cliente não vê)", expanded=False):
        st.write(
            {
                "papel ativo": estado.agente_ativo,
                "autenticado": estado.autenticado,
                "tentativas de autenticação": estado.tentativas_autenticacao,
                "cliente": estado.cliente.nome if estado.cliente else None,
                "limite atual": estado.cliente.limite_credito if estado.cliente else None,
                "score": estado.cliente.score if estado.cliente else None,
                "atendimento encerrado": estado.encerrado,
                "motivo do encerramento": estado.motivo_encerramento or None,
                "modelo respondendo": _rotulo_da_cadeia(corrente, settings) if corrente else None,
                "fallbacks de modelo": trocas_de_modelo,
                "ferramentas chamadas neste turno": st.session_state.get("trocas", [])[-6:],
            }
        )
        if trocas_de_modelo and isinstance(corrente, CorrenteDeModelos):
            st.warning("Houve fallback de modelo nesta conversa:")
            for passo, falha in corrente.falhas:
                st.text(f"  {passo.rotulo} → {falha[:140]}")
        if estado.trilha:
            st.caption("Trilha interna")
            for evento in estado.trilha[-12:]:
                st.text(evento)


SEM_SLOT = "(nenhum)"


def _slot_atual(valores: dict[str, str], indice: int) -> tuple[str, str]:
    """Lê do `.env` o provedor/modelo de um slot (o primeiro é o primário)."""
    if indice == 0:
        return valores.get("LLM_PROVIDER", "gemini"), valores.get("LLM_MODEL", "")
    bruto = valores.get(f"LLM_FALLBACK_{indice}", "")
    if not bruto.strip():
        return SEM_SLOT, ""
    provedor, _, modelo = bruto.partition(":")
    return provedor.strip(), modelo.strip()


def _escolher_slot(indice: int, atuais: dict[str, str]) -> tuple[str, str]:
    """Renderiza os controles de um slot e devolve ``(provedor, modelo)``."""
    rotulo = SLOTS[indice]
    provedor_atual, modelo_atual = _slot_atual(atuais, indice)
    provedores = (
        [SEM_SLOT, *sorted(PROVEDORES_SUPORTADOS)] if indice else sorted(PROVEDORES_SUPORTADOS)
    )

    with st.expander(f"Modelo {rotulo}", expanded=indice == 0):
        coluna_provedor, coluna_modelo, coluna_teste = st.columns([2, 3, 2])

        with coluna_provedor:
            provedor = st.selectbox(
                "Provedor",
                provedores,
                index=provedores.index(provedor_atual) if provedor_atual in provedores else 0,
                key=f"slot_provedor_{indice}",
            )

        with coluna_modelo:
            if provedor == SEM_SLOT:
                st.caption("Sem modelo neste slot.")
                return SEM_SLOT, ""

            if st.button("Listar modelos do provedor", key=f"listar_{indice}"):
                modelos, origem = listar_modelos(provedor, obter_settings())
                st.session_state[f"modelos_{indice}"] = modelos
                st.session_state[f"origem_{indice}"] = origem

            disponiveis = st.session_state.get(f"modelos_{indice}") or list(
                MODELOS_CONHECIDOS.get(provedor, ())
            )
            if st.session_state.get(f"origem_{indice}") == ORIGEM_API:
                st.caption("lista vinda da API do provedor")
            modelo = st.selectbox(
                "Modelo",
                disponiveis,
                index=disponiveis.index(modelo_atual) if modelo_atual in disponiveis else 0,
                key=f"slot_modelo_{indice}",
            )
            if st.checkbox("usar outro nome de modelo", key=f"manual_{indice}"):
                modelo = st.text_input("Nome exato do modelo", value=modelo, key=f"texto_{indice}")

        with coluna_teste:
            if st.button("Testar conexão", key=f"testar_{indice}"):
                with st.spinner("Chamando o provedor…"):
                    st.session_state[f"resultado_{indice}"] = testar_passo(
                        PassoDaCadeia(provedor=provedor, modelo=modelo), obter_settings()
                    )
            resultado = st.session_state.get(f"resultado_{indice}")
            if resultado is not None:
                (st.success if resultado.ok else st.error)(resultado.descricao())

    return provedor, modelo


def aba_configuracoes() -> None:
    """Cadeia de modelos, chaves e gravação no `.env` (com confirmação)."""
    st.subheader("Cadeia de modelos")
    st.caption(
        "Se o modelo da frente falhar por infraestrutura (quota, indisponibilidade, "
        "timeout, modelo descontinuado), o próximo assume **no mesmo turno** — o cliente "
        "não percebe. Falha de contrato (argumento inválido) não troca de modelo: isso é bug nosso."
    )

    atuais = ler_env(CAMINHO_ENV)
    escolhidos = [_escolher_slot(indice, atuais) for indice in range(len(SLOTS))]

    primario, segundo = escolhidos[0][0], escolhidos[1][0]
    if primario == segundo and primario != SEM_SLOT:
        st.warning(
            "Primário e secundário usam o **mesmo provedor** — mesma chave, mesma quota. "
            "Se a quota estourar, o fallback não vai ajudar."
        )

    st.divider()
    st.subheader("Chaves de API")
    st.caption("Deixe em branco para manter a chave atual. O valor nunca é exibido por inteiro.")
    chaves_informadas: dict[str, str] = {}
    for provedor in sorted(PROVEDORES_SUPORTADOS):
        nome_variavel = _variavel_da_chave(provedor)
        if nome_variavel is None:
            continue
        existente = atuais.get(nome_variavel, "")
        digitada = st.text_input(
            f"{provedor} ({nome_variavel})",
            type="password",
            placeholder=mascarar_valor(nome_variavel, existente) or "não configurada",
            key=f"chave_{provedor}",
        )
        if digitada.strip():
            chaves_informadas[nome_variavel] = digitada.strip()

    st.divider()
    st.subheader("Gravar no .env")
    valores: dict[str, str] = {
        "LLM_PROVIDER": escolhidos[0][0],
        "LLM_MODEL": escolhidos[0][1],
        "LLM_FALLBACK_1": _texto_do_slot(escolhidos[1]),
        "LLM_FALLBACK_2": _texto_do_slot(escolhidos[2]),
        **chaves_informadas,
    }

    alteracoes = planejar_alteracoes(CAMINHO_ENV, valores)
    if not alteracoes:
        st.info("Nada a alterar: o `.env` já está com esta configuração.")
        return

    st.write("Estas chaves vão ser sobrescritas em `.env`:")
    for alteracao in alteracoes:
        st.markdown(f"- `{alteracao.descricao()}`")
    st.caption(
        f"Um backup do arquivo atual é criado antes: `{CAMINHO_ENV.name}.bak-<data-hora>`. "
        "Variáveis de outros sistemas não são tocadas."
    )

    confirmado = st.checkbox(
        "Entendo que isto sobrescreve estas chaves no .env (arquivo com credenciais)."
    )
    if st.button("Gravar no .env agora", type="primary", disabled=not confirmado):
        try:
            backup = gravar_env(CAMINHO_ENV, valores, confirmado=True)
        except ConfirmacaoNecessaria as erro:  # só ocorre se o fluxo for contornado
            st.error(str(erro))
            return
        st.success(f"Gravado. Backup guardado em `{backup.name}`.")
        obter_settings.cache_clear()
        st.session_state.pop("atendimento", None)
        st.rerun()


def _variavel_da_chave(provedor: str) -> str | None:
    mapa = {
        "gemini": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "groq": "GROQ_API_KEY",
        "togetherai": "TOGETHER_API_KEY",
        "ollama": "OLLAMA_API_KEY",
    }
    return mapa.get(provedor)


def _texto_do_slot(escolha: tuple[str, str]) -> str:
    provedor, modelo = escolha
    if provedor == SEM_SLOT or not modelo:
        return ""
    return f"{provedor}:{modelo}"


def main() -> None:
    st.set_page_config(page_title="Banco Ágil — Atendimento", page_icon="🏦", layout="wide")
    st.title("🏦 Banco Ágil — atendimento inteligente")

    aba_chat, aba_config = st.tabs(["Atendimento", "Configurações"])
    with aba_chat:
        aba_atendimento()
    with aba_config:
        aba_configuracoes()


if __name__ == "__main__":
    main()
