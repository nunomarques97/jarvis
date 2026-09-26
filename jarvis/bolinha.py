r"""A bolinha de estado: uma janela pequena, sem moldura, sempre por cima.

    .venv\Scripts\python -m jarvis.bolinha

Mostra um orbe que segue o estado do jarvis (a ouvir a palavra de ativacao,
a ouvir o Sponsor, a pensar, a falar, a espera de confirmacao, a dormir,
erro) e, por baixo, uma legenda curta opcional. O contrato visual esta em
`docs/DESIGN.md`. So usa a biblioteca padrao (tkinter e ctypes).

Corre num processo filho do jarvis, para que um erro ou uma janela presa
nunca atrase o caminho da voz. Fala com o pai por linhas de texto UTF-8:

  stdin (pai -> bolinha), uma mensagem por linha, no maximo 512 bytes:
    estado <id>        repouso | ouvir | pensar | falar | confirmar | dormir | erro
    nivel <0..1>       nivel do microfone (ouvir, confirmar) ou da voz (falar);
                       fora de 0..1 e preso aos limites; sem novo nivel em
                       0,5 s o orbe volta ao tamanho de repouso do estado
    legenda <texto>    texto curto por baixo do orbe; `legenda` sozinha apaga.
                       Os estados repouso e dormir apagam a legenda.
  stdout (bolinha -> pai):
    pronto             a janela esta no ecra
    clique             o Sponsor clicou (sem arrastar) no orbe ou na legenda

Linhas desconhecidas, mal formadas ou demasiado longas sao ignoradas com uma
linha no stderr; o stdout so leva as duas mensagens acima. Quando o stdin
fecha (o pai saiu ou fechou o tubo), a janela fecha e o processo sai com 0.

A janela nao rouba o foco: e uma janela de ferramenta que nao se ativa
(WS_EX_NOACTIVATE), e uma mudanca de estado so redesenha o orbe. Pode ser
arrastada; a posicao fica em `.jarvis/bolinha.json` (ignorado pelo Git) e, ao
arrancar, e presa de volta a um monitor visivel.

As partes puras (protocolo, modelo de estados, suavizacao do nivel, clique
contra arrasto, posicao) nao abrem janelas e sao testadas sem ecra.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterator

# --- Protocolo ----------------------------------------------------------------

ESTADOS = ("repouso", "ouvir", "pensar", "falar", "confirmar", "dormir", "erro")
TEMAS = ("escuro", "claro")

#: Tamanho maximo de uma linha do stdin, sem o fim de linha.
MAX_LINHA_BYTES = 512
#: Caracteres visiveis da legenda; o resto e cortado com reticencias.
MAX_LEGENDA = 80
#: Texto maximo de um nivel ("0.123456" chega e sobra).
MAX_NIVEL_CHARS = 16

MENSAGEM_PRONTO = "pronto"
MENSAGEM_CLIQUE = "clique"


class MensagemInvalida(ValueError):
    """Uma linha do stdin que nao e uma mensagem do protocolo."""


@dataclass(frozen=True)
class Mensagem:
    tipo: str  # "estado" | "nivel" | "legenda"
    valor: str | float


def limitar(valor: float, minimo: float = 0.0, maximo: float = 1.0) -> float:
    """`valor` dentro de [minimo, maximo]; NaN ou nao numerico vale `minimo`."""
    try:
        valor = float(valor)
    except (TypeError, ValueError):
        return minimo
    if not math.isfinite(valor):
        return maximo if valor > 0 and not math.isnan(valor) else minimo
    return min(maximo, max(minimo, valor))


def legenda_segura(texto: str, maximo: int = MAX_LEGENDA) -> str:
    """Texto de legenda sem controlos, espacos repetidos nem excesso.

    Tira caracteres de controlo e de formatacao (incluindo os que viram a
    direcao do texto) e os de fora do plano basico, que o Tk 8.6 nao desenha
    de forma fiavel. Corta numa fronteira de palavra quando pode e acaba com
    reticencias.
    """
    limpos: list[str] = []
    for caracter in str(texto):
        if ord(caracter) > 0xFFFF:
            continue
        categoria = unicodedata.category(caracter)
        if categoria == "Cc" or categoria == "Zl" or categoria == "Zp":
            limpos.append(" ")
        elif categoria.startswith("C"):
            continue
        else:
            limpos.append(caracter)
    texto = " ".join("".join(limpos).split())
    if len(texto) <= maximo:
        return texto
    corte = texto[: maximo - 1]
    espaco = corte.rfind(" ")
    if espaco >= maximo * 0.6:
        corte = corte[:espaco]
    return corte.rstrip(" ,.;:-") + "…"


def interpretar_linha(linha: bytes | str) -> Mensagem:
    """Uma linha do stdin -> `Mensagem`, ou `MensagemInvalida`."""
    if isinstance(linha, str):
        linha = linha.encode("utf-8", "surrogatepass")
    linha = linha.rstrip(b"\r\n")
    if len(linha) > MAX_LINHA_BYTES:
        raise MensagemInvalida(f"linha com {len(linha)} bytes (maximo {MAX_LINHA_BYTES})")
    try:
        texto = linha.decode("utf-8")
    except UnicodeDecodeError as erro:
        raise MensagemInvalida("linha que nao e UTF-8") from erro
    comando, _, resto = texto.partition(" ")
    if comando == "estado":
        if resto not in ESTADOS:
            raise MensagemInvalida(f"estado desconhecido: {resto[:24]!r}")
        return Mensagem("estado", resto)
    if comando == "nivel":
        if not resto or len(resto) > MAX_NIVEL_CHARS:
            raise MensagemInvalida("nivel sem numero ou comprido demais")
        try:
            valor = float(resto)
        except ValueError as erro:
            raise MensagemInvalida(f"nivel que nao e numero: {resto!r}") from erro
        if not math.isfinite(valor):
            raise MensagemInvalida(f"nivel que nao e finito: {resto!r}")
        return Mensagem("nivel", limitar(valor))
    if comando == "legenda":
        return Mensagem("legenda", legenda_segura(resto))
    raise MensagemInvalida(f"comando desconhecido: {comando[:24]!r}")


def ler_linhas(fluxo: BinaryIO, maximo: int = MAX_LINHA_BYTES) -> Iterator[bytes | None]:
    """As linhas do `fluxo` ate ao fim; `None` no lugar de cada linha longa demais.

    Nunca guarda mais do que `maximo` + 2 bytes de uma linha: o excesso e lido
    e deitado fora ate ao fim de linha.
    """
    while True:
        pedaco = fluxo.readline(maximo + 2)
        if not pedaco:
            return
        if pedaco.endswith(b"\n") or len(pedaco) < maximo + 2:
            yield pedaco.rstrip(b"\r\n")
            continue
        while True:
            resto = fluxo.readline(4096)
            if not resto or resto.endswith(b"\n"):
                break
        yield None


def ler_comandos(
    fluxo: BinaryIO | None,
    entregar: Callable[[Mensagem | None], object],
    avisar: Callable[[str], object],
) -> None:
    """Le o `fluxo` e entrega cada mensagem valida; no fim entrega `None`."""
    try:
        if fluxo is not None:
            for linha in ler_linhas(fluxo):
                if linha is None:
                    avisar(f"linha com mais de {MAX_LINHA_BYTES} bytes ignorada")
                    continue
                if not linha.strip():
                    continue
                try:
                    entregar(interpretar_linha(linha))
                except MensagemInvalida as erro:
                    avisar(f"mensagem ignorada: {erro}")
    except (OSError, ValueError) as erro:
        avisar(f"stdin falhou: {erro}")
    finally:
        entregar(None)


# --- Nivel e modelo de estados ------------------------------------------------

#: Constantes de tempo da suavizacao: sobe depressa, desce mais devagar.
SUBIDA_S = 0.05
DESCIDA_S = 0.18
#: Sem nivel novo durante isto, o alvo volta a zero (o pai deixou de mandar).
NIVEL_VELHO_S = 0.5


def suavizar(
    atual: float,
    alvo: float,
    dt: float,
    subida_s: float = SUBIDA_S,
    descida_s: float = DESCIDA_S,
) -> float:
    """Aproxima `atual` de `alvo` (ambos em 0..1) num passo de `dt` segundos."""
    atual = limitar(atual)
    alvo = limitar(alvo)
    if not dt > 0 or not math.isfinite(dt):
        return atual
    tau = subida_s if alvo > atual else descida_s
    fator = 1.0 - math.exp(-dt / tau)
    return limitar(atual + (alvo - atual) * fator)


@dataclass(frozen=True)
class Quadro:
    """O que desenhar num instante, em pixeis logicos (96 ppp)."""

    estado: str
    raio: float
    dx: float = 0.0
    anel: float | None = None
    anel_espessura: float = 0.0
    sinal: str = ""
    pontos: tuple[float, ...] = ()
    barras: tuple[float, ...] = ()
    lua: bool = False
    legenda: str = ""


def _r(valor: float) -> float:
    """Arredonda para 0,5 px: quadros iguais a vista nao se redesenham."""
    return round(valor * 2.0) / 2.0


#: Periodos das animacoes, em segundos.
RESPIRACAO_S = 4.0
PULSO_CONFIRMAR_S = 1.6
ONDA_PENSAR_S = 1.2
TREMOR_ERRO_S = 0.45


@dataclass
class ModeloDaBolinha:
    """Estado, nivel e legenda recebidos, e o quadro a desenhar em cada instante."""

    estado: str = "repouso"
    legenda: str = ""
    nivel_alvo: float = 0.0
    nivel: float = 0.0
    desde: float = 0.0
    nivel_em: float | None = None
    ultimo_passo: float | None = None

    def aplicar(self, mensagem: Mensagem, agora: float) -> bool:
        """Aplica uma mensagem; diz se mudou alguma coisa."""
        if mensagem.tipo == "estado":
            if mensagem.valor == self.estado:
                return False
            self.estado = str(mensagem.valor)
            self.desde = agora
            self.nivel_alvo = 0.0
            self.nivel_em = None
            if self.estado in ("repouso", "dormir"):
                self.legenda = ""
            return True
        if mensagem.tipo == "nivel":
            self.nivel_alvo = limitar(mensagem.valor)  # type: ignore[arg-type]
            self.nivel_em = agora
            return True
        if mensagem.tipo == "legenda":
            anterior = self.legenda
            self.legenda = legenda_segura(str(mensagem.valor))
            return self.legenda != anterior
        return False

    def passo(self, agora: float) -> None:
        """Avanca a suavizacao do nivel ate `agora`."""
        if self.nivel_em is not None and agora - self.nivel_em > NIVEL_VELHO_S:
            self.nivel_alvo = 0.0
            self.nivel_em = None
        dt = 0.0 if self.ultimo_passo is None else agora - self.ultimo_passo
        self.ultimo_passo = agora
        self.nivel = suavizar(self.nivel, self.nivel_alvo, dt)

    def quadro(self, agora: float, *, animacao: bool = True) -> Quadro:
        return quadro_do_estado(
            self.estado, self.nivel, agora - self.desde, agora, legenda=self.legenda, animacao=animacao
        )


def quadro_do_estado(
    estado: str,
    nivel: float,
    no_estado_s: float,
    agora: float,
    *,
    legenda: str = "",
    animacao: bool = True,
) -> Quadro:
    """O desenho de um estado: forma (a pista sem cor), tamanho e animacao.

    Com `animacao=False` (animacoes do Windows desligadas) cada estado tem um
    equivalente parado com a mesma pista de forma e sem depender do nivel.
    """
    nivel = limitar(nivel) if animacao else 0.0
    tau = 2.0 * math.pi
    if estado == "ouvir":
        raio = 30 + 18 * nivel
        anel = raio + 5 + 8 * nivel if animacao else raio + 7
        return Quadro(estado, _r(raio), anel=_r(anel), anel_espessura=2.0, legenda=legenda)
    if estado == "pensar":
        if animacao:
            pontos = tuple(
                _r(3.5 + 1.5 * max(0.0, math.sin(tau * (agora / ONDA_PENSAR_S) - i * 0.9)))
                for i in range(3)
            )
        else:
            pontos = (3.5, 3.5, 3.5)
        return Quadro(estado, 28.0, pontos=pontos, legenda=legenda)
    if estado == "falar":
        raio = 30 + 10 * nivel
        if animacao:
            barras = tuple(
                _r((6 + 26 * nivel * (0.7 + 0.3 * math.sin(tau * 3.0 * agora + i * 2.1))) * (1.0 if i == 1 else 0.7))
                for i in range(3)
            )
        else:
            barras = (10.0, 18.0, 10.0)
        return Quadro(estado, _r(raio), barras=barras, legenda=legenda)
    if estado == "confirmar":
        raio = 30 + 12 * nivel
        if animacao:
            fase = (max(0.0, no_estado_s) % PULSO_CONFIRMAR_S) / PULSO_CONFIRMAR_S
            anel, espessura = raio + 3 + 13 * fase, 0.5 + 2.5 * (1.0 - fase)
        else:
            anel, espessura = raio + 6, 2.0
        return Quadro(
            estado, _r(raio), anel=_r(anel), anel_espessura=_r(espessura), sinal="?", legenda=legenda
        )
    if estado == "dormir":
        return Quadro(estado, 18.0, lua=True, legenda=legenda)
    if estado == "erro":
        dx = 0.0
        if animacao and 0.0 <= no_estado_s < TREMOR_ERRO_S:
            dx = 4.0 * math.sin(tau * 12.0 * no_estado_s) * (1.0 - no_estado_s / TREMOR_ERRO_S)
        return Quadro(estado, 28.0, dx=_r(dx), sinal="!", legenda=legenda)
    # repouso (e qualquer estado que nao se conheca): pequeno e calmo
    raio = 20 + (1.5 * math.sin(tau * agora / RESPIRACAO_S) if animacao else 0.0)
    return Quadro("repouso", _r(raio), legenda=legenda)


def intervalo_do_quadro(estado: str, animacao: bool, no_estado_s: float) -> float:
    """Segundos ate ao proximo quadro: 30 por segundo so quando ha movimento."""
    if not animacao or estado == "dormir":
        return 0.1
    if estado == "repouso":
        return 1 / 15
    if estado == "erro" and no_estado_s >= TREMOR_ERRO_S:
        return 0.1
    return 1 / 30


# --- Cores (contrato em docs/DESIGN.md) -------------------------------------------

PALETAS: dict[str, dict[str, str]] = {
    "escuro": {
        "repouso": "#6B7A90",
        "ouvir": "#3D8BFD",
        "pensar": "#9B7BF7",
        "falar": "#1FB89A",
        "confirmar": "#F5A524",
        "dormir": "#56607A",
        "erro": "#F05252",
        "sinal": "#FFFFFF",
        "sinal_confirmar": "#1B1405",
        "contorno": "#0D1117",
        "pilula": "#202124",
        "pilula_borda": "#3C4043",
        "texto": "#ECEDEE",
    },
    "claro": {
        "repouso": "#8C97A8",
        "ouvir": "#1F6FEB",
        "pensar": "#7C4DDB",
        "falar": "#0E9477",
        "confirmar": "#E09200",
        "dormir": "#AEB6C2",
        "erro": "#D93636",
        "sinal": "#FFFFFF",
        "sinal_confirmar": "#1B1405",
        "contorno": "#FFFFFF",
        "pilula": "#FFFFFF",
        "pilula_borda": "#D0D4DA",
        "texto": "#1F2328",
    },
}

#: Cor chave que o Windows torna transparente (e deixa passar os cliques).
COR_CHAVE = "#010203"


# --- Clique contra arrasto -------------------------------------------------------

#: Movimento a partir do qual premir vira arrasto (o SM_CXDRAG do Windows).
LIMIAR_ARRASTO_PX = 4
#: Um clique e curto: mais do que isto premido sem mexer nao conta.
CLIQUE_MAXIMO_S = 0.6


class Arrasto:
    """Distingue um clique curto de um arrasto e da a posicao nova da janela."""

    def __init__(self, limiar_px: float = LIMIAR_ARRASTO_PX, clique_maximo_s: float = CLIQUE_MAXIMO_S) -> None:
        self._limiar = limiar_px
        self._clique_maximo_s = clique_maximo_s
        self._inicio: tuple[float, float, int, int, float] | None = None
        self.a_arrastar = False

    def premir(self, x_ecra: float, y_ecra: float, janela_x: int, janela_y: int, agora: float) -> None:
        self._inicio = (x_ecra, y_ecra, janela_x, janela_y, agora)
        self.a_arrastar = False

    def mover(self, x_ecra: float, y_ecra: float) -> tuple[int, int] | None:
        """A posicao nova da janela, ou `None` enquanto nao e arrasto."""
        if self._inicio is None:
            return None
        x0, y0, jx, jy, _ = self._inicio
        dx, dy = x_ecra - x0, y_ecra - y0
        if not self.a_arrastar and math.hypot(dx, dy) < self._limiar:
            return None
        self.a_arrastar = True
        return int(round(jx + dx)), int(round(jy + dy))

    def largar(self, x_ecra: float, y_ecra: float, agora: float) -> str | None:
        """"clique", "arrasto" ou `None` (largar sem premir, ou premido demais)."""
        if self._inicio is None:
            return None
        x0, y0, _, _, quando = self._inicio
        self._inicio = None
        if self.a_arrastar or math.hypot(x_ecra - x0, y_ecra - y0) >= self._limiar:
            self.a_arrastar = False
            return "arrasto"
        if agora - quando > self._clique_maximo_s:
            return None
        return "clique"


# --- Posicao -------------------------------------------------------------------

POSICAO_POR_OMISSAO = Path(__file__).resolve().parents[1] / ".jarvis" / "bolinha.json"
MARGEM_DO_CANTO = 16
_COORDENADA_MAXIMA = 100_000


@dataclass(frozen=True)
class Area:
    """Um retangulo do ecra (a area de trabalho de um monitor)."""

    esquerda: int
    topo: int
    direita: int
    fundo: int


def posicao_no_canto(largura: int, altura: int, area: Area, margem: int = MARGEM_DO_CANTO) -> tuple[int, int]:
    """Canto inferior direito da `area`, com margem."""
    return area.direita - largura - margem, area.fundo - altura - margem


def _intersecao(x: int, y: int, largura: int, altura: int, area: Area) -> int:
    largo = min(x + largura, area.direita) - max(x, area.esquerda)
    alto = min(y + altura, area.fundo) - max(y, area.topo)
    return max(0, largo) * max(0, alto)


def _distancia(x: int, y: int, largura: int, altura: int, area: Area) -> float:
    cx, cy = x + largura / 2, y + altura / 2
    dx = max(area.esquerda - cx, 0, cx - area.direita)
    dy = max(area.topo - cy, 0, cy - area.fundo)
    return math.hypot(dx, dy)


def prender_ao_monitor(x: int, y: int, largura: int, altura: int, areas: list[Area]) -> tuple[int, int]:
    """Poe a janela toda dentro do monitor que mais a mostra (ou do mais perto)."""
    if not areas:
        return x, y
    area = max(areas, key=lambda a: (_intersecao(x, y, largura, altura, a), -_distancia(x, y, largura, altura, a)))
    x = max(area.esquerda, min(x, area.direita - largura))
    y = max(area.topo, min(y, area.fundo - altura))
    return x, y


def ler_posicao(caminho: Path | None) -> tuple[int, int] | None:
    """A posicao guardada, ou `None` se nao existe ou nao presta."""
    if caminho is None:
        return None
    try:
        with open(caminho, "rb") as ficheiro:
            dados = json.loads(ficheiro.read(4096).decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(dados, dict):
        return None
    x, y = dados.get("x"), dados.get("y")
    if not all(isinstance(v, int) and not isinstance(v, bool) and abs(v) <= _COORDENADA_MAXIMA for v in (x, y)):
        return None
    return x, y


def guardar_posicao(caminho: Path | None, x: int, y: int) -> bool:
    """Grava a posicao de forma atomica; diz se conseguiu."""
    if caminho is None:
        return False
    temporario = caminho.with_name(caminho.name + ".tmp")
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        temporario.write_text(json.dumps({"x": int(x), "y": int(y)}), encoding="utf-8")
        os.replace(temporario, caminho)
        return True
    except OSError:
        try:
            temporario.unlink()
        except OSError:
            pass
        return False


def posicao_inicial(
    guardada: tuple[int, int] | None, largura: int, altura: int, areas: list[Area], ecra: Area
) -> tuple[int, int]:
    """Posicao guardada presa a um monitor visivel, ou o canto do monitor principal."""
    if guardada is not None:
        return prender_ao_monitor(guardada[0], guardada[1], largura, altura, areas or [ecra])
    principal = next((a for a in areas if a.esquerda <= 0 < a.direita and a.topo <= 0 < a.fundo), None)
    return posicao_no_canto(largura, altura, principal or (areas[0] if areas else ecra))


# --- Windows: tema, animacoes, monitores e foco ------------------------------------


def tema_do_windows() -> str:
    """"claro" ou "escuro", como o tema das aplicacoes do Windows."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        ) as chave:
            valor, _ = winreg.QueryValueEx(chave, "AppsUseLightTheme")
        return "claro" if valor == 1 else "escuro"
    except (ImportError, OSError):
        return "escuro"


def animacoes_do_windows() -> bool:
    """Falso quando "Efeitos de animacao" esta desligado no Windows."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        ligado = ctypes.c_int(1)
        spi_getclientareaanimation = 0x1042
        if ctypes.windll.user32.SystemParametersInfoW(spi_getclientareaanimation, 0, ctypes.byref(ligado), 0):
            return bool(ligado.value)
    except (OSError, AttributeError):
        pass
    return True


def areas_dos_monitores() -> list[Area]:
    """As areas de trabalho de todos os monitores (vazio fora do Windows ou se falhar)."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        areas: list[Area] = []
        user32 = ctypes.windll.user32
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
        tipo = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
        )

        def cada(monitor, _dc, _retangulo, _dados):
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                r = info.rcWork
                areas.append(Area(r.left, r.top, r.right, r.bottom))
            return True

        user32.EnumDisplayMonitors(None, None, tipo(cada), 0)
        return areas
    except (OSError, AttributeError):
        return []


def ativar_dpi() -> None:
    """Pixeis reais em ecras com escala (antes de criar o Tk)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (OSError, AttributeError):
        pass


def janela_sem_foco(identificador: int) -> bool:
    """Janela de ferramenta que nunca se ativa: nao rouba o foco nem vai a barra."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        gwl_exstyle = -20
        ws_ex_toolwindow, ws_ex_topmost, ws_ex_noactivate = 0x80, 0x8, 0x08000000
        hwnd = user32.GetParent(identificador) or identificador
        estilo = user32.GetWindowLongPtrW(hwnd, gwl_exstyle)
        user32.SetWindowLongPtrW(hwnd, gwl_exstyle, estilo | ws_ex_toolwindow | ws_ex_topmost | ws_ex_noactivate)
        return True
    except (OSError, AttributeError):
        return False


def _user32_do_foco():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    return user32


def janela_em_primeiro_plano() -> int | None:
    """A janela com o foco agora (antes de a bolinha existir)."""
    if sys.platform != "win32":
        return None
    try:
        return _user32_do_foco().GetForegroundWindow() or None
    except (OSError, AttributeError):
        return None


def devolver_o_foco(anterior: int | None) -> bool:
    """O Tk ativa a primeira janela que cria: devolve o foco a quem o tinha."""
    if sys.platform != "win32" or not anterior:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = _user32_do_foco()
        dono = wintypes.DWORD()
        user32.GetWindowThreadProcessId(anterior, ctypes.byref(dono))
        if dono.value == os.getpid():
            return False
        atual = user32.GetForegroundWindow()
        user32.GetWindowThreadProcessId(atual, ctypes.byref(dono))
        if not atual or dono.value != os.getpid():
            return False
        return bool(user32.SetForegroundWindow(anterior))
    except (OSError, AttributeError):
        return False


# --- A janela (tkinter) ------------------------------------------------------------

#: Geometria logica (96 ppp): a zona do orbe e a da legenda, uma por baixo da outra.
LARGURA = 224
ZONA_DO_ORBE = 128
ZONA_DA_LEGENDA = 64
ALTURA = ZONA_DO_ORBE + ZONA_DA_LEGENDA
LARGURA_DO_TEXTO = 200


class JanelaDaBolinha:
    """Desenha `Quadro`s numa janela tkinter sem moldura, sempre por cima."""

    def __init__(
        self,
        raiz,
        *,
        tema: str = "escuro",
        ao_clique: Callable[[], object] | None = None,
        ficheiro_posicao: Path | None = None,
        posicao: tuple[int, int] | None = None,
        relogio: Callable[[], float] = time.monotonic,
        foco_anterior: int | None = None,
    ) -> None:
        import tkinter

        self.raiz = raiz
        self.tema = tema if tema in PALETAS else "escuro"
        self._ao_clique = ao_clique
        self._ficheiro = ficheiro_posicao
        self._relogio = relogio
        self._arrasto = Arrasto()
        self.escala = max(1.0, float(raiz.winfo_fpixels("1i")) / 96.0)
        self.largura = int(round(LARGURA * self.escala))
        self.altura = int(round(ALTURA * self.escala))
        self._ultimo: Quadro | None = None

        raiz.withdraw()
        raiz.overrideredirect(True)
        raiz.attributes("-topmost", True)
        raiz.configure(bg=COR_CHAVE)
        try:
            raiz.attributes("-transparentcolor", COR_CHAVE)
        except tkinter.TclError:
            pass
        self.canvas = tkinter.Canvas(
            raiz, width=self.largura, height=self.altura, bg=COR_CHAVE, highlightthickness=0, bd=0
        )
        self.canvas.pack()
        self._fonte_sinal = ("Segoe UI Semibold", 18)
        self._fonte_legenda = ("Segoe UI", 9)
        self.canvas.bind("<ButtonPress-1>", self._premir)
        self.canvas.bind("<B1-Motion>", self._mover)
        self.canvas.bind("<ButtonRelease-1>", self._largar)

        if posicao is None:
            ecra = Area(0, 0, raiz.winfo_screenwidth(), raiz.winfo_screenheight())
            posicao = posicao_inicial(
                ler_posicao(self._ficheiro), self.largura, self.altura, areas_dos_monitores(), ecra
            )
        self.x, self.y = posicao
        raiz.geometry(f"{self.largura}x{self.altura}+{self.x}+{self.y}")
        raiz.update_idletasks()
        janela_sem_foco(raiz.winfo_id())
        raiz.deiconify()
        raiz.update()
        devolver_o_foco(foco_anterior)

    # -- rato

    def _premir(self, evento) -> None:
        self._arrasto.premir(evento.x_root, evento.y_root, self.x, self.y, self._relogio())

    def _mover(self, evento) -> None:
        nova = self._arrasto.mover(evento.x_root, evento.y_root)
        if nova is not None:
            self.x, self.y = nova
            self.raiz.geometry(f"+{self.x}+{self.y}")

    def _largar(self, evento) -> None:
        resultado = self._arrasto.largar(evento.x_root, evento.y_root, self._relogio())
        if resultado == "clique" and self._ao_clique is not None:
            self._ao_clique()
        elif resultado == "arrasto":
            ecra = Area(0, 0, self.raiz.winfo_screenwidth(), self.raiz.winfo_screenheight())
            self.x, self.y = prender_ao_monitor(
                self.x, self.y, self.largura, self.altura, areas_dos_monitores() or [ecra]
            )
            self.raiz.geometry(f"+{self.x}+{self.y}")
            guardar_posicao(self._ficheiro, self.x, self.y)

    # -- desenho

    def mudar_tema(self, tema: str) -> None:
        if tema in PALETAS and tema != self.tema:
            self.tema = tema
            self._ultimo = None

    def desenhar(self, quadro: Quadro) -> bool:
        """Redesenha so se o quadro mudou; diz se redesenhou."""
        if quadro == self._ultimo:
            return False
        self._ultimo = quadro
        c, e, cores = self.canvas, self.escala, PALETAS[self.tema]
        c.delete("all")
        # Fundo na cor chave a cobrir tudo: o Tk so repinta a caixa de cada
        # item apagado e deixa restos dos aneis grossos fora dela.
        c.create_rectangle(0, 0, self.largura + 1, self.altura + 1, fill=COR_CHAVE, outline="")
        cx = (self.largura / 2) + quadro.dx * e
        cy = (ZONA_DO_ORBE / 2) * e
        cor = cores.get(quadro.estado, cores["repouso"])
        raio = quadro.raio * e

        def disco(r: float, preenchimento: str, x: float = cx, y: float = cy) -> None:
            c.create_oval(x - r, y - r, x + r, y + r, fill=preenchimento, outline="")

        if quadro.anel is not None and quadro.anel_espessura > 0:
            r = quadro.anel * e
            c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=cor, width=max(1.0, quadro.anel_espessura * e))
        disco(raio + 1.5 * e, cores["contorno"])
        disco(raio, cor)
        if quadro.lua:
            disco(raio * 0.8, COR_CHAVE, cx + raio * 0.5, cy - raio * 0.35)
        if quadro.sinal:
            tinta = cores["sinal_confirmar"] if quadro.estado == "confirmar" else cores["sinal"]
            c.create_text(cx, cy, text=quadro.sinal, fill=tinta, font=self._fonte_sinal)
        for i, r in enumerate(quadro.pontos):
            disco(r * e, cores["sinal"], cx + (i - 1) * 11 * e, cy)
        for i, h in enumerate(quadro.barras):
            x = cx + (i - 1) * 10 * e
            meia = max(2.0, h * e / 2)
            c.create_line(x, cy - meia, x, cy + meia, fill=cores["sinal"], width=5 * e, capstyle="round")
        if quadro.legenda:
            self._desenhar_legenda(quadro.legenda, cores)
        return True

    def _desenhar_legenda(self, texto: str, cores: dict[str, str]) -> None:
        c, e = self.canvas, self.escala
        topo = (ZONA_DO_ORBE + 4) * e
        item = c.create_text(
            self.largura / 2,
            topo + 7 * e,
            text=texto,
            fill=cores["texto"],
            font=self._fonte_legenda,
            width=LARGURA_DO_TEXTO * e,
            justify="center",
            anchor="n",
        )
        folga_x, folga_y = 10 * e, 6 * e
        x0, y0, x1, y1 = c.bbox(item)
        # Letras largas podem dar mais linhas do que cabem: encurta ate caber.
        while y1 + folga_y > self.altura - 1 and len(texto) > 1:
            texto = legenda_segura(texto, max(1, len(texto) - 6))
            c.itemconfigure(item, text=texto)
            x0, y0, x1, y1 = c.bbox(item)
        x0, x1 = x0 - folga_x, x1 + folga_x
        y0, y1 = y0 - folga_y, min(y1 + folga_y, self.altura - 1)
        raio = min(12 * e, (y1 - y0) / 2)
        pilula = c.create_polygon(
            _retangulo_redondo(x0, y0, x1, y1, raio),
            smooth=True,
            fill=cores["pilula"],
            outline=cores["pilula_borda"],
        )
        c.tag_lower(pilula, item)


def _retangulo_redondo(x0: float, y0: float, x1: float, y1: float, r: float) -> list[float]:
    """Pontos de um poligono suave com cantos redondos (o Tk nao tem este desenho)."""
    return [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
        x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]  # fmt: skip


# --- Do lado do jarvis (processo pai; nunca abre janelas) ---------------------

#: Estado do painel do jarvis (`estado | ...`) -> estado da bolinha. Os textos
#: sao os de `jarvis.app` (A_OUVIR, A_PENSAR, ...); aqui repetidos para a
#: bolinha nao importar a aplicacao inteira.
ESTADO_DO_PAINEL = {
    "A OUVIR": "repouso",
    "EM CONVERSA": "ouvir",
    # Depois de o jarvis falar, a ouvir sem palavra de ativacao.
    "A OUVIR-TE": "ouvir",
    "A PENSAR": "pensar",
    "A FALAR": "falar",
    "À ESPERA DE CONFIRMAÇÃO": "confirmar",
    "À ESPERA DE CONFIRMAÇÃO — A OUVIR A RESPOSTA": "confirmar",
    "A DORMIR": "dormir",
}
#: No maximo tantos niveis por segundo, do microfone e da voz (cada um).
NIVEIS_POR_SEGUNDO = 30
#: Quanto tempo o estado de erro fica a vista antes de voltar ao do jarvis.
ERRO_VISIVEL_S = 3.0
#: Estados em que o nivel do microfone conta; o da voz so conta a falar.
ESTADOS_COM_MICROFONE = ("ouvir", "confirmar")
#: dBFS que valem nivel 0 e nivel 1 (fala normal ao microfone fica no meio).
NIVEL_ZERO_DB = -55.0
NIVEL_UM_DB = -15.0


def nivel_de_pcm16(pcm16: bytes) -> float:
    """Nivel 0..1 de um bloco PCM16 mono: o RMS em dBFS numa escala linear."""
    amostras = len(pcm16) // 2
    if amostras == 0:
        return 0.0
    try:
        import numpy

        valores = numpy.frombuffer(pcm16, dtype="<i2", count=amostras).astype(numpy.float32)
        media = float(numpy.mean(valores * valores))
    except ImportError:
        from array import array

        valores = array("h", pcm16[: amostras * 2])
        if sys.byteorder != "little":
            valores.byteswap()
        media = sum(v * v for v in valores) / amostras
    if media <= 0.0:
        return 0.0
    db = 10.0 * math.log10(media / (32768.0 * 32768.0))
    return limitar((db - NIVEL_ZERO_DB) / (NIVEL_UM_DB - NIVEL_ZERO_DB))


class PonteDaBolinha:
    """Junta o que o jarvis publica num estado da bolinha e manda so as mudancas.

    Fontes: o painel (`painel`), a escuta do ouvido (`ouvido`: inicio, fim,
    captada, descartada, erro), falhas (`erro`), legendas e os blocos de audio
    do microfone e da voz. Prioridade: erro (durante `ERRO_VISIVEL_S`), a falar,
    a ouvir o Sponsor (ou a resposta ao recap), a transcrever, o estado do
    painel. Os niveis so sao calculados quando contam e no maximo
    `NIVEIS_POR_SEGUNDO` vezes por segundo. Nada aqui espera: `enviar` e
    `enviar_nivel` tem de ser nao bloqueantes.
    """

    def __init__(
        self,
        enviar: Callable[[str], object],
        enviar_nivel: Callable[[float], object] | None = None,
        *,
        relogio: Callable[[], float] = time.monotonic,
        niveis_por_segundo: float = NIVEIS_POR_SEGUNDO,
        erro_visivel_s: float = ERRO_VISIVEL_S,
        medir: Callable[[bytes], float] = nivel_de_pcm16,
    ) -> None:
        self._enviar = enviar
        self._enviar_nivel = enviar_nivel or (lambda valor: enviar(f"nivel {valor:.3f}"))
        self._relogio = relogio
        self._intervalo = 1.0 / niveis_por_segundo
        self._erro_visivel_s = erro_visivel_s
        self._medir = medir
        self._tranca = threading.Lock()
        self._base = "repouso"
        self._a_captar = False
        self._a_transcrever = False
        self._erro_ate: float | None = None
        self._ultimo_microfone = -math.inf
        self._ultima_voz = -math.inf
        #: O ultimo estado mandado a bolinha.
        self.estado: str | None = None

    def _efetivo(self, agora: float) -> str:
        if self._erro_ate is not None:
            if agora < self._erro_ate:
                return "erro"
            self._erro_ate = None
        if self._base == "falar":
            return "falar"
        if self._a_captar:
            return "confirmar" if self._base == "confirmar" else "ouvir"
        if self._a_transcrever:
            return "pensar"
        return self._base

    def _atualizar(self, agora: float) -> None:
        estado = self._efetivo(agora)
        if estado != self.estado:
            self.estado = estado
            self._mandar(self._enviar, f"estado {estado}")

    @staticmethod
    def _mandar(enviar: Callable, valor: object) -> None:
        try:
            enviar(valor)
        except Exception:  # noqa: BLE001 - a bolinha e um extra, quem publica segue
            pass

    def atualizar(self) -> None:
        with self._tranca:
            self._atualizar(self._relogio())

    def painel(self, estado_do_painel: str) -> None:
        """Um `estado | ...` do painel; os desconhecidos nao mudam nada."""
        estado = ESTADO_DO_PAINEL.get(estado_do_painel)
        if estado is None:
            return
        with self._tranca:
            self._base = estado
            if estado == "pensar":
                self._a_transcrever = False  # a frase transcrita ja chegou ao jarvis
            self._atualizar(self._relogio())

    def ouvido(self, evento: str) -> None:
        """Um momento da escuta (`jarvis.ouvido.EVENTOS_DO_OUVIDO`)."""
        with self._tranca:
            agora = self._relogio()
            if evento == "inicio":
                self._a_captar = True
            elif evento == "fim":
                # Sem mandar ja: logo a seguir vem `captada` (pensar) ou
                # `descartada`, e a bolinha nao pisca pelo repouso. Se nao vier
                # nada (janela fechada), o proximo chunk do microfone atualiza.
                self._a_captar = False
                return
            elif evento == "captada":
                self._a_transcrever = True
            elif evento == "descartada":
                self._a_captar = self._a_transcrever = False
            elif evento == "erro":
                self._a_captar = self._a_transcrever = False
                self._erro_ate = agora + self._erro_visivel_s
            else:
                return
            self._atualizar(agora)

    def erro(self) -> None:
        """Algo falhou: a bolinha mostra erro durante `ERRO_VISIVEL_S`."""
        with self._tranca:
            agora = self._relogio()
            self._erro_ate = agora + self._erro_visivel_s
            self._atualizar(agora)

    def legenda(self, texto: str) -> None:
        """A frase ouvida ou o texto a enviar; vazio apaga a legenda."""
        limpo = legenda_segura(texto or "")
        with self._tranca:
            self._mandar(self._enviar, f"legenda {limpo}" if limpo else "legenda")

    def _nivel(self, pcm16: bytes, estados: tuple[str, ...], microfone: bool) -> None:
        with self._tranca:
            agora = self._relogio()
            self._atualizar(agora)  # e aqui que um erro velho expira
            if self.estado not in estados:
                return
            ultimo = self._ultimo_microfone if microfone else self._ultima_voz
            if agora - ultimo < self._intervalo:
                return
            if microfone:
                self._ultimo_microfone = agora
            else:
                self._ultima_voz = agora
        self._mandar(self._enviar_nivel, self._medir(pcm16))

    def nivel_do_microfone(self, pcm16: bytes) -> None:
        """Um chunk lido do microfone (chamado para todos; so alguns contam)."""
        self._nivel(pcm16, ESTADOS_COM_MICROFONE, True)

    def nivel_da_voz(self, pcm16: bytes) -> None:
        """Um bloco que vai para a saida de som."""
        self._nivel(pcm16, ("falar",), False)


#: Mensagens (estado, legenda) a espera do escritor; cheia, as novas perdem-se.
FILA_DA_LIGACAO = 64
#: Linhas do stderr da bolinha copiadas para o log, no maximo.
MAXIMO_DE_LINHAS_DO_FILHO = 20
#: Respostas desconhecidas da bolinha registadas no log, no maximo.
MAXIMO_DE_IGNORADAS = 20


def argv_da_bolinha() -> list[str]:
    return [sys.executable, "-m", "jarvis.bolinha"]


def _fechar_em_silencio(fluxo: object) -> None:
    try:
        fluxo.close()
    except Exception:  # noqa: BLE001 - so arrumacao
        pass


class LigacaoABolinha:
    """A bolinha como processo filho, sem nunca fazer esperar quem manda.

    `enviar` poe a mensagem numa fila limitada (cheia, perde-se) e
    `enviar_nivel` guarda so o nivel mais recente; uma thread escreve no stdin
    do filho. Outra le o stdout e so aceita a linha exata `clique`; o resto e
    ignorado e registado. Se o filho nao arranca, morre ou o tubo parte, fica
    uma linha no log e o jarvis segue sem ela.
    """

    def __init__(
        self,
        ao_clique: Callable[[], object],
        escrever: Callable[[str], object],
        *,
        argv: list[str] | None = None,
        popen: Callable[..., object] = subprocess.Popen,
        tamanho_da_fila: int = FILA_DA_LIGACAO,
    ) -> None:
        self._ao_clique = ao_clique
        self._escrever = escrever
        self._argv = argv or argv_da_bolinha()
        self._popen = popen
        self._fila: queue.Queue[str] = queue.Queue(maxsize=tamanho_da_fila)
        self._nivel: float | None = None
        self._tranca = threading.Lock()
        self._acordar = threading.Event()
        self._processo = None
        self._fios: list[threading.Thread] = []
        self._morta = False
        self._a_fechar = False
        self.perdidas = 0
        self.ignoradas = 0
        self.cliques = 0

    @property
    def viva(self) -> bool:
        return self._processo is not None and not self._morta

    def iniciar(self) -> bool:
        """Lanca o filho e as threads. False (com uma linha no log) se nao arrancou."""
        try:
            self._processo = self._popen(
                self._argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(Path(__file__).resolve().parents[1]),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as erro:  # noqa: BLE001 - sem bolinha o jarvis funciona na mesma
            self._morta = True
            self._escrever(f"bolinha | nao arrancou ({erro!r}); o jarvis segue sem ela")
            return False
        self._fios = [
            threading.Thread(target=self._escrever_em_ciclo, name="bolinha-escrita", daemon=True),
            threading.Thread(target=self._ler_em_ciclo, name="bolinha-leitura", daemon=True),
            threading.Thread(target=self._ler_erros_em_ciclo, name="bolinha-stderr", daemon=True),
        ]
        for fio in self._fios:
            fio.start()
        return True

    # -- quem manda (qualquer thread; nunca espera)

    def enviar(self, linha: str) -> None:
        if self._morta or self._a_fechar:
            return
        try:
            self._fila.put_nowait(linha)
        except queue.Full:
            self.perdidas += 1
            return
        self._acordar.set()

    def enviar_nivel(self, valor: float) -> None:
        if self._morta or self._a_fechar:
            return
        with self._tranca:
            self._nivel = valor
        self._acordar.set()

    # -- threads

    def _morreu(self, motivo: str) -> None:
        with self._tranca:
            if self._morta:
                return
            self._morta = True
        if not self._a_fechar:
            self._escrever(f"bolinha | {motivo}; o jarvis segue sem ela")

    def _escrever_em_ciclo(self) -> None:
        stdin = self._processo.stdin
        try:
            while True:
                self._acordar.wait()
                self._acordar.clear()
                if self._a_fechar or self._morta:
                    return
                linhas: list[str] = []
                while True:
                    try:
                        linhas.append(self._fila.get_nowait())
                    except queue.Empty:
                        break
                with self._tranca:
                    nivel, self._nivel = self._nivel, None
                if nivel is not None:
                    linhas.append(f"nivel {nivel:.3f}")
                if not linhas:
                    continue
                try:
                    stdin.write("".join(linha + "\n" for linha in linhas).encode("utf-8"))
                    stdin.flush()
                except (OSError, ValueError) as erro:
                    self._morreu(f"o tubo para a janela partiu ({erro.__class__.__name__})")
                    return
        finally:
            try:
                stdin.close()  # sem stdin, a bolinha fecha a janela e sai
            except (OSError, ValueError):
                pass

    def _ler_em_ciclo(self) -> None:
        try:
            for linha in ler_linhas(self._processo.stdout):
                if linha == MENSAGEM_CLIQUE.encode("ascii"):
                    self.cliques += 1
                    try:
                        self._ao_clique()
                    except Exception as erro:  # noqa: BLE001 - um clique falhado nunca derruba o jarvis
                        self._escrever(f"bolinha | o clique falhou: {erro!r}")
                elif linha == MENSAGEM_PRONTO.encode("ascii"):
                    self._escrever("bolinha | janela no ecra")
                else:
                    self.ignoradas += 1
                    if self.ignoradas <= MAXIMO_DE_IGNORADAS:
                        mostrar = "linha longa demais" if linha is None else repr(linha[:40])
                        self._escrever(f"bolinha | resposta ignorada: {mostrar}")
        except (OSError, ValueError):
            pass
        _fechar_em_silencio(self._processo.stdout)
        codigo = None
        try:
            codigo = self._processo.wait(timeout=1.0)
        except Exception:  # noqa: BLE001 - so para o log
            pass
        self._morreu(f"a janela fechou (codigo {codigo})")

    def _ler_erros_em_ciclo(self) -> None:
        copiadas = 0
        try:
            for linha in ler_linhas(self._processo.stderr):
                if linha is None or copiadas >= MAXIMO_DE_LINHAS_DO_FILHO:
                    continue
                copiadas += 1
                self._escrever(f"bolinha | {linha[:200].decode('utf-8', 'replace')}")
        except (OSError, ValueError):
            pass
        _fechar_em_silencio(self._processo.stderr)

    # -- fim

    def fechar(self, espera_s: float = 1.0) -> None:
        """Fecha o stdin (a bolinha sai sozinha); se nao sair a tempo, e morta."""
        if self._processo is None:
            return
        self._a_fechar = True
        self._acordar.set()
        if self._fios:
            self._fios[0].join(timeout=espera_s / 2)
        try:
            self._processo.wait(timeout=espera_s)
        except Exception:  # noqa: BLE001 - presa: mata-se
            try:
                self._processo.kill()
            except Exception:  # noqa: BLE001
                pass


# --- O processo filho --------------------------------------------------------------


class _Saida:
    """Escreve as mensagens do protocolo no stdout; um tubo partido nao faz mal."""

    def __init__(self, fluxo: BinaryIO | None) -> None:
        self._fluxo = fluxo
        self._tranca = threading.Lock()

    def __call__(self, mensagem: str) -> None:
        if self._fluxo is None:
            return
        with self._tranca:
            try:
                self._fluxo.write(mensagem.encode("ascii") + b"\n")
                self._fluxo.flush()
            except (OSError, ValueError):
                self._fluxo = None


def _avisar(texto: str) -> None:
    try:
        print(f"bolinha: {texto}", file=sys.stderr, flush=True)
    except (OSError, ValueError, AttributeError):
        pass


#: Mensagens aplicadas por quadro, no maximo; o resto fica para o seguinte.
MENSAGENS_POR_QUADRO = 200
#: Fila entre o leitor do stdin e a janela; cheia, as mensagens sao deitadas fora.
TAMANHO_DA_FILA = 1000
#: De quanto em quanto tempo se volta a ler o tema e as animacoes do Windows.
RELER_O_SISTEMA_S = 5.0


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m jarvis.bolinha", description="bolinha de estado do jarvis")
    parser.add_argument("--tema", choices=("auto", *TEMAS), default="auto", help="tema; auto segue o Windows")
    parser.add_argument("--sem-animacao", action="store_true", help="equivalentes parados, como sem animacoes")
    parser.add_argument("--posicao", type=Path, default=POSICAO_POR_OMISSAO, help="ficheiro da posicao")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = construir_parser().parse_args(argv)
    fila: queue.Queue[Mensagem | None] = queue.Queue(maxsize=TAMANHO_DA_FILA)

    def entregar(mensagem: Mensagem | None) -> None:
        if mensagem is None:
            while True:
                try:
                    fila.put(None, timeout=0.5)
                    return
                except queue.Full:
                    continue
        try:
            fila.put_nowait(mensagem)
        except queue.Full:
            pass

    ativar_dpi()
    foco_anterior = janela_em_primeiro_plano()
    try:
        import tkinter

        raiz = tkinter.Tk()
    except Exception as erro:  # sem Tk ou sem ecra: o jarvis continua sem a bolinha
        _avisar(f"sem janela: {erro}")
        return 1

    saida = _Saida(getattr(sys.stdout, "buffer", None))
    tema_fixo = None if args.tema == "auto" else args.tema
    janela = JanelaDaBolinha(
        raiz,
        tema=tema_fixo or tema_do_windows(),
        ao_clique=lambda: saida(MENSAGEM_CLIQUE),
        ficheiro_posicao=args.posicao,
        foco_anterior=foco_anterior,
    )
    modelo = ModeloDaBolinha(desde=time.monotonic())
    sistema = {"animacao": not args.sem_animacao and animacoes_do_windows(), "lido_em": time.monotonic()}
    stdin = getattr(sys.stdin, "buffer", None)
    threading.Thread(target=ler_comandos, args=(stdin, entregar, _avisar), daemon=True).start()

    def quadro_seguinte() -> None:
        agora = time.monotonic()
        for _ in range(MENSAGENS_POR_QUADRO):
            try:
                mensagem = fila.get_nowait()
            except queue.Empty:
                break
            if mensagem is None:
                raiz.quit()
                return
            modelo.aplicar(mensagem, agora)
        if agora - sistema["lido_em"] > RELER_O_SISTEMA_S:
            sistema["lido_em"] = agora
            sistema["animacao"] = not args.sem_animacao and animacoes_do_windows()
            if tema_fixo is None:
                janela.mudar_tema(tema_do_windows())
        modelo.passo(agora)
        animacao = bool(sistema["animacao"])
        janela.desenhar(modelo.quadro(agora, animacao=animacao))
        espera = intervalo_do_quadro(modelo.estado, animacao, agora - modelo.desde)
        raiz.after(max(10, int(espera * 1000)), quadro_seguinte)

    quadro_seguinte()
    saida(MENSAGEM_PRONTO)
    try:
        raiz.mainloop()
    except KeyboardInterrupt:
        pass
    try:
        raiz.destroy()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
