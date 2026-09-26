r"""Capturas de ecra da bolinha de estado em cada estado, nos dois temas.

    .venv\Scripts\python scripts/capturar_bolinha.py
    .venv\Scripts\python scripts/capturar_bolinha.py --verificar
    .venv\Scripts\python scripts/capturar_bolinha.py --so-verificar

Abre a janela real da bolinha (`jarvis.bolinha`) no canto do ecra, por cima
de um fundo liso da cor do tema, e desenha cada estado com um instante e um
nivel fixos: os sete estados, dois exemplos com legenda (a frase ouvida e o
texto a enviar no recap, este cortado por ser longo) e o equivalente parado
de cada estado (animacoes do Windows desligadas). Faz isto no tema escuro e
no claro.

A captura e do ecra, com a GDI do Windows por ctypes, e cada PNG e escrito
com zlib (o Pillow nao esta instalado). Os PNG, uma folha com todos e um
indice ficam em `docs/forja/evidence/bolinha/`, pasta ignorada pelo Git.

Com `--verificar` (depois de capturar) ou `--so-verificar` (sem capturar),
sai com 1 se faltar uma captura, se uma estiver em branco (so a cor do fundo)
ou se dois estados diferentes derem imagens iguais. So corre no Windows e
precisa do ecra desbloqueado; nao toca som nem grava a posicao da bolinha.
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
import time
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis import bolinha  # noqa: E402

PASTA = RAIZ / "docs" / "forja" / "evidence" / "bolinha"

#: Fundo liso por baixo da bolinha em cada tema (cores de janela do Windows 11).
FUNDOS = {"escuro": "#202020", "claro": "#F3F3F3"}
#: Margem do fundo a volta da janela da bolinha.
MARGEM = 16
#: Instante fixo das animacoes, para capturas repetiveis.
INSTANTE = 1.0

LEGENDA_OUVIDA = "what time is it"
LEGENDA_RECAP = (
    "Send to demo-project: add unit tests for the login form and make sure every "
    "error message stays short and clear for the user"
)


@dataclass(frozen=True)
class Cena:
    nome: str
    estado: str
    nivel: float = 0.0
    no_estado_s: float = 1.0
    legenda: str = ""
    animacao: bool = True


def cenas() -> list[Cena]:
    niveis = {"ouvir": 0.6, "falar": 0.7}
    animadas = [
        Cena(estado, estado, niveis.get(estado, 0.0), 0.3 if estado == "confirmar" else 1.0)
        for estado in bolinha.ESTADOS
    ]
    legendas = [
        Cena("ouvir-legenda", "ouvir", 0.3, legenda=LEGENDA_OUVIDA),
        Cena("confirmar-legenda", "confirmar", 0.0, 0.3, legenda=LEGENDA_RECAP),
    ]
    paradas = [Cena(f"estatico-{estado}", estado, animacao=False) for estado in bolinha.ESTADOS]
    return animadas + legendas + paradas


def nome_do_ficheiro(tema: str, cena: Cena) -> str:
    return f"{tema}-{cena.nome}.png"


# --- PNG (so biblioteca padrao) -------------------------------------------------------


def _bloco(tipo: bytes, dados: bytes) -> bytes:
    return struct.pack(">I", len(dados)) + tipo + dados + struct.pack(">I", zlib.crc32(tipo + dados) & 0xFFFFFFFF)


def png_rgb(largura: int, altura: int, rgb: bytes) -> bytes:
    """PNG de 8 bits RGB, sem filtros, a partir das linhas RGB seguidas."""
    passo = largura * 3
    if len(rgb) != passo * altura:
        raise ValueError("tamanho dos pixeis nao bate com a imagem")
    linhas = b"".join(b"\x00" + rgb[y * passo : (y + 1) * passo] for y in range(altura))
    cabecalho = struct.pack(">IIBBBBB", largura, altura, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _bloco(b"IHDR", cabecalho)
        + _bloco(b"IDAT", zlib.compress(linhas, 9))
        + _bloco(b"IEND", b"")
    )


def ler_png(dados: bytes) -> tuple[int, int, bytes]:
    """(largura, altura, rgb) de um PNG escrito por `png_rgb`; ValueError se nao for."""
    if not dados.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("nao e PNG")
    posicao, largura, altura, comprimido = 8, 0, 0, b""
    while posicao < len(dados):
        (tamanho,) = struct.unpack(">I", dados[posicao : posicao + 4])
        tipo = dados[posicao + 4 : posicao + 8]
        conteudo = dados[posicao + 8 : posicao + 8 + tamanho]
        posicao += 12 + tamanho
        if tipo == b"IHDR":
            largura, altura, bits, cor = struct.unpack(">IIBB", conteudo[:10])
            if (bits, cor) != (8, 2):
                raise ValueError("so PNG RGB de 8 bits")
        elif tipo == b"IDAT":
            comprimido += conteudo
        elif tipo == b"IEND":
            break
    linhas = zlib.decompress(comprimido)
    passo = largura * 3
    if len(linhas) != (passo + 1) * altura:
        raise ValueError("PNG cortado")
    rgb = bytearray()
    for y in range(altura):
        inicio = y * (passo + 1)
        if linhas[inicio] != 0:
            raise ValueError("filtro de linha nao suportado")
        rgb += linhas[inicio + 1 : inicio + 1 + passo]
    return largura, altura, bytes(rgb)


# --- Verificacao -----------------------------------------------------------------------

#: Fracao minima de pixeis que nao sao a cor dominante (o fundo).
FRACAO_MINIMA = 0.002


def em_branco(largura: int, altura: int, rgb: bytes) -> bool:
    """Verdadeiro se a imagem e (quase) so uma cor: nada foi desenhado."""
    total = largura * altura
    if total == 0:
        return True
    cores = Counter(rgb[i : i + 3] for i in range(0, len(rgb), 3))
    _, dominante = cores.most_common(1)[0]
    return len(cores) < 2 or (total - dominante) / total < FRACAO_MINIMA


def verificar(pasta: Path, temas: tuple[str, ...] = bolinha.TEMAS) -> list[str]:
    """Os problemas encontrados nas capturas (vazio se esta tudo bem)."""
    problemas: list[str] = []
    impressoes: dict[tuple[str, bool], dict[str, str]] = {}
    por_cena: dict[str, dict[str, str]] = {}
    for tema in temas:
        for cena in cenas():
            nome = nome_do_ficheiro(tema, cena)
            caminho = pasta / nome
            if not caminho.is_file():
                problemas.append(f"falta {nome}")
                continue
            try:
                largura, altura, rgb = ler_png(caminho.read_bytes())
            except (ValueError, zlib.error, struct.error) as erro:
                problemas.append(f"{nome} nao se le: {erro}")
                continue
            if em_branco(largura, altura, rgb):
                problemas.append(f"{nome} esta em branco")
            impressao = hashlib.sha256(rgb).hexdigest()
            grupo = impressoes.setdefault((tema, cena.animacao), {})
            for outro, dela in grupo.items():
                if dela == impressao:
                    problemas.append(f"{nome} e igual a {outro}, de outro estado")
            grupo[nome] = impressao
            igual = por_cena.setdefault(cena.nome, {})
            for outro, dela in igual.items():
                if dela == impressao:
                    problemas.append(f"{nome} e igual a {outro}, de outro tema")
            igual[nome] = impressao
    return problemas


# --- Captura do ecra (Windows GDI por ctypes) ----------------------------------------


def capturar_ecra(x: int, y: int, largura: int, altura: int) -> bytes:
    """RGB de um retangulo do ecra, tal como se ve (janelas em camadas incluidas)."""
    import ctypes
    from ctypes import wintypes

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.BitBlt.argtypes = [wintypes.HDC] + [ctypes.c_int] * 4 + [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT
    ]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
        ]  # fmt: skip

    srccopy, captureblt = 0x00CC0020, 0x40000000
    ecra = user32.GetDC(None)
    memoria = gdi32.CreateCompatibleDC(ecra)
    imagem = gdi32.CreateCompatibleBitmap(ecra, largura, altura)
    antigo = gdi32.SelectObject(memoria, imagem)
    try:
        if not gdi32.BitBlt(memoria, 0, 0, largura, altura, ecra, x, y, srccopy | captureblt):
            raise OSError("BitBlt falhou (ecra bloqueado?)")
        cabecalho = BITMAPINFOHEADER()
        cabecalho.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        cabecalho.biWidth, cabecalho.biHeight = largura, -altura
        cabecalho.biPlanes, cabecalho.biBitCount = 1, 32
        bgra = ctypes.create_string_buffer(largura * altura * 4)
        gdi32.SelectObject(memoria, antigo)
        if gdi32.GetDIBits(memoria, imagem, 0, altura, bgra, ctypes.byref(cabecalho), 0) != altura:
            raise OSError("GetDIBits falhou")
    finally:
        gdi32.DeleteObject(imagem)
        gdi32.DeleteDC(memoria)
        user32.ReleaseDC(None, ecra)
    dados = bgra.raw
    rgb = bytearray(largura * altura * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = dados[2::4], dados[1::4], dados[0::4]
    return bytes(rgb)


def folha(imagens: list[list[tuple[int, int, bytes]]], separador: tuple[int, int, int] = (128, 128, 128)) -> tuple[int, int, bytes]:
    """Junta linhas de imagens do mesmo tamanho numa so, com uma grelha de 2 px."""
    largura, altura, _ = imagens[0][0]
    colunas = max(len(linha) for linha in imagens)
    total_l, total_a = colunas * (largura + 2), len(imagens) * (altura + 2)
    rgb = bytearray(bytes(separador) * (total_l * total_a))
    for i, linha in enumerate(imagens):
        for j, (l, a, pixeis) in enumerate(linha):
            for y in range(a):
                destino = ((i * (altura + 2) + y) * total_l + j * (largura + 2)) * 3
                rgb[destino : destino + l * 3] = pixeis[y * l * 3 : (y + 1) * l * 3]
    return total_l, total_a, bytes(rgb)


def capturar(pasta: Path) -> list[str]:
    """Desenha e captura todas as cenas; devolve os nomes escritos."""
    import tkinter

    bolinha.ativar_dpi()
    foco = bolinha.janela_em_primeiro_plano()
    fundo = tkinter.Tk()
    fundo.withdraw()
    fundo.overrideredirect(True)
    fundo.attributes("-topmost", True)
    orbe = tkinter.Toplevel(fundo)
    janela = bolinha.JanelaDaBolinha(orbe, ficheiro_posicao=None, posicao=(0, 0), foco_anterior=None)
    largura, altura = janela.largura + 2 * MARGEM, janela.altura + 2 * MARGEM
    areas = bolinha.areas_dos_monitores()
    area = areas[0] if areas else bolinha.Area(0, 0, fundo.winfo_screenwidth(), fundo.winfo_screenheight())
    x, y = bolinha.posicao_no_canto(largura, altura, area)
    fundo.geometry(f"{largura}x{altura}+{x}+{y}")
    fundo.update_idletasks()
    bolinha.janela_sem_foco(fundo.winfo_id())
    fundo.deiconify()
    janela.x, janela.y = x + MARGEM, y + MARGEM
    orbe.geometry(f"+{janela.x}+{janela.y}")
    orbe.lift(fundo)
    fundo.update()
    bolinha.devolver_o_foco(foco)

    pasta.mkdir(parents=True, exist_ok=True)
    escritos: list[str] = []
    linhas_da_folha: list[list[tuple[int, int, bytes]]] = []
    try:
        for tema in bolinha.TEMAS:
            fundo.configure(bg=FUNDOS[tema])
            janela.mudar_tema(tema)
            animadas: list[tuple[int, int, bytes]] = []
            paradas: list[tuple[int, int, bytes]] = []
            for cena in cenas():
                quadro = bolinha.quadro_do_estado(
                    cena.estado, cena.nivel, cena.no_estado_s, INSTANTE, legenda=bolinha.legenda_segura(cena.legenda), animacao=cena.animacao
                )
                janela.desenhar(quadro)
                fundo.update()
                time.sleep(0.2)
                fundo.update()
                rgb = capturar_ecra(x, y, largura, altura)
                nome = nome_do_ficheiro(tema, cena)
                (pasta / nome).write_bytes(png_rgb(largura, altura, rgb))
                escritos.append(nome)
                (animadas if cena.animacao else paradas).append((largura, altura, rgb))
            linhas_da_folha += [animadas, paradas]
    finally:
        fundo.destroy()
    (pasta / "folha.png").write_bytes(png_rgb(*folha(linhas_da_folha)))
    return escritos


def escrever_indice(pasta: Path, escritos: list[str], problemas: list[str] | None) -> None:
    linhas = [
        "# Status orb captures",
        "",
        f"Captured {time.strftime('%Y-%m-%d %H:%M:%S')} by `scripts/capturar_bolinha.py` from the real",
        "tkinter window over a plain theme background (GDI screen capture).",
        "",
        "`folha.png` rows: dark animated states + caption examples, dark static",
        "(reduced motion), light animated + captions, light static. Column order:",
        ", ".join(c.nome for c in cenas() if c.animacao) + "; static: " + ", ".join(bolinha.ESTADOS) + ".",
        "",
        "Files:",
        "",
        *[f"- {nome}" for nome in escritos],
        "",
    ]
    if problemas is not None:
        linhas += ["Verification: " + ("passed" if not problemas else "FAILED"), ""]
        linhas += [f"- {p}" for p in problemas]
    (pasta / "capturas.md").write_text("\n".join(linhas) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="capturas da bolinha em cada estado")
    grupo = parser.add_mutually_exclusive_group()
    grupo.add_argument("--verificar", action="store_true", help="captura e verifica")
    grupo.add_argument("--so-verificar", action="store_true", help="so verifica as capturas que ja existem")
    parser.add_argument("--pasta", type=Path, default=PASTA, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    escritos: list[str] = []
    if not args.so_verificar:
        if sys.platform != "win32":
            print("As capturas usam a GDI do Windows: corre isto no Windows.")
            return 2
        escritos = capturar(args.pasta)
        print(f"{len(escritos)} capturas em {args.pasta.relative_to(RAIZ) if args.pasta.is_relative_to(RAIZ) else args.pasta}")
    if not (args.verificar or args.so_verificar):
        escrever_indice(args.pasta, escritos, None)
        return 0
    problemas = verificar(args.pasta)
    if escritos:
        escrever_indice(args.pasta, escritos, problemas)
    for problema in problemas:
        print(f"FALHA: {problema}")
    if problemas:
        return 1
    print(f"verificacao ok: {len(cenas()) * len(bolinha.TEMAS)} capturas, nenhuma em branco, estados todos diferentes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
