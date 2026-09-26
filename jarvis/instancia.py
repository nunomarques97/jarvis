"""Uma so instancia do jarvis com o microfone: a tranca com o PID em `logs/`.

Dois jarvis abertos ao mesmo tempo ouvem as mesmas frases e respondem ambos.
Por isso o jarvis com o microfone so arranca depois de criar, de forma
atomica (`O_CREAT | O_EXCL`), o ficheiro `logs/jarvis.lock` com o seu PID
(e o instante em que o processo foi criado, quando o sistema o diz). Se o
ficheiro ja existe:

  - o processo la escrito esta vivo: o arranque e recusado com uma mensagem
    clara (`OutraInstanciaAberta`), antes de carregar modelos ou abrir o
    microfone;
  - o processo morreu (ou o PID foi reaproveitado por outro programa, ou o
    ficheiro esta estragado): a tranca velha e substituida e o arranque
    continua, com uma linha no log a dize-lo.

Saber se um processo esta vivo usa so a biblioteca padrao: `ctypes` com a API
do Windows (OpenProcess, GetExitCodeProcess, GetProcessTimes) e `os.kill(pid,
0)` nos outros sistemas. A pasta `logs/` ja esta no .gitignore.

Os modos `--wav` e `--autoteste` nao tiram a tranca: nao abrem o microfone.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

#: Nome do ficheiro da tranca, dentro da pasta dos logs.
NOME_DA_TRANCA = "jarvis.lock"

#: Tentativas de criar a tranca quando outra corrida a mexe ao mesmo tempo.
TENTATIVAS = 3
#: Uma tranca vazia mais nova do que isto e um jarvis a meio de a escrever:
#: espera-se pelo PID em vez de a dar como velha.
ESPERA_DA_TRANCA_VAZIA_S = 2.0

_MENSAGENS = {
    "pt": (
        "Já há outro jarvis aberto (PID {pid}). Fecha essa janela primeiro "
        "e depois volta a arrancar o jarvis. Este não arrancou."
    ),
    "en": (
        "Another jarvis is already running (PID {pid}). Close it first, "
        "then start jarvis again. This one did not start."
    ),
}


class OutraInstanciaAberta(RuntimeError):
    """Ha outro jarvis vivo com a tranca: este nao arranca."""

    def __init__(self, pid: int) -> None:
        super().__init__(f"outro jarvis aberto com o pid {pid}")
        self.pid = pid


def mensagem_de_recusa(pid: int, lingua: str) -> str:
    """O que o ecra diz quando outro jarvis ja esta aberto."""
    return _MENSAGENS["en" if lingua == "en" else "pt"].format(pid=pid)


@dataclass(frozen=True)
class DonoDaTranca:
    """O que esta escrito no ficheiro: o PID e, se conhecido, quando o processo nasceu."""

    pid: int
    criado: int | None = None


def ler_dono(texto: str) -> DonoDaTranca | None:
    """Le o conteudo da tranca. None se estiver vazio ou estragado."""
    linhas = [linha.strip() for linha in texto.splitlines() if linha.strip()]
    if not linhas or not linhas[0].isdigit():
        return None
    pid = int(linhas[0])
    if pid <= 0:
        return None
    criado = int(linhas[1]) if len(linhas) > 1 and linhas[1].isdigit() else None
    return DonoDaTranca(pid, criado)


def escrever_dono(dono: DonoDaTranca) -> str:
    return f"{dono.pid}\n" + (f"{dono.criado}\n" if dono.criado is not None else "")


# --- Processos vivos (so a biblioteca padrao) ---------------------------------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


def _kernel32():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return ctypes, wintypes, kernel32


def _estado_windows(pid: int) -> tuple[bool, int | None]:
    """(vivo, instante de criacao em FILETIME) de um processo no Windows."""
    ctypes, wintypes, kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Sem acesso quer dizer que existe (de outro utilizador); o resto, que nao.
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED, None
    try:
        codigo = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(codigo)):
            return True, None
        if codigo.value != _STILL_ACTIVE:
            return False, None
        tempos = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(tempo) for tempo in tempos)):
            return True, None
        criacao = tempos[0]
        return True, (criacao.dwHighDateTime << 32) | criacao.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def _estado_posix(pid: int) -> tuple[bool, int | None]:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, None
    except PermissionError:
        return True, None
    except OSError:
        return False, None
    return True, None


def estado_do_processo(pid: int) -> tuple[bool, int | None]:
    """(vivo, instante de criacao ou None) do processo `pid`."""
    if pid <= 0:
        return False, None
    if sys.platform == "win32":
        return _estado_windows(pid)
    return _estado_posix(pid)


# --- A tranca -----------------------------------------------------------------


class TrancaDaInstancia:
    """O ficheiro `logs/jarvis.lock` com o PID do jarvis que tem o microfone.

    `estado` recebe um PID e devolve (vivo, instante de criacao ou None); os
    testes passam processos falsos por aqui. Por omissao e
    `estado_do_processo`, procurado a cada chamada.
    """

    def __init__(
        self,
        caminho: Path,
        *,
        pid: int | None = None,
        estado: Callable[[int], tuple[bool, int | None]] | None = None,
    ) -> None:
        self.caminho = Path(caminho)
        self.pid = os.getpid() if pid is None else pid
        self._estado = estado
        self._minha = False
        self.espera_da_tranca_vazia_s = ESPERA_DA_TRANCA_VAZIA_S

    @property
    def adquirida(self) -> bool:
        return self._minha

    def _dono_vivo(self, dono: DonoDaTranca | None) -> bool:
        if dono is None or dono.pid == self.pid:
            return False
        vivo, criado = self._estado_de(dono.pid)
        if not vivo:
            return False
        # O mesmo PID num processo criado noutro instante e outro programa.
        return dono.criado is None or criado is None or dono.criado == criado

    def _estado_de(self, pid: int) -> tuple[bool, int | None]:
        return (self._estado or estado_do_processo)(pid)

    def _criar(self) -> bool:
        """Cria o ficheiro de forma atomica. False se ja existir."""
        # Antes de criar: o ficheiro fica vazio o menos tempo possivel.
        _, criado = self._estado_de(self.pid)
        try:
            descritor = os.open(self.caminho, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(descritor, "w", encoding="ascii") as ficheiro:
            ficheiro.write(escrever_dono(DonoDaTranca(self.pid, criado)))
        return True

    def _ler(self, caminho: Path) -> DonoDaTranca | None:
        try:
            return ler_dono(caminho.read_text(encoding="ascii", errors="replace"))
        except FileNotFoundError:
            raise
        except OSError:
            return None

    def _ler_esperando_o_pid(self) -> DonoDaTranca | None:
        """Le a tranca; se esta vazia e acabou de ser criada, espera que o PID la chegue."""
        while True:
            dono = self._ler(self.caminho)
            if dono is not None:
                return dono
            try:
                estado = self.caminho.stat()
            except FileNotFoundError:
                raise
            except OSError:
                return None
            if estado.st_size > 0 or time.time() - estado.st_mtime >= self.espera_da_tranca_vazia_s:
                return None
            time.sleep(0.02)

    def adquirir(self) -> DonoDaTranca | None:
        """Fica com a tranca, ou levanta `OutraInstanciaAberta`.

        Devolve o dono da tranca velha que foi substituida (processo morto ou
        ficheiro estragado; `pid` 0 quando nem o PID se lia), ou None se nao
        havia tranca nenhuma.
        """
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        substituida: DonoDaTranca | None = None
        for _ in range(TENTATIVAS):
            if self._criar():
                self._minha = True
                return substituida
            try:
                dono = self._ler_esperando_o_pid()
            except FileNotFoundError:
                continue  # a outra instancia saiu entretanto: tenta de novo
            if self._dono_vivo(dono):
                assert dono is not None
                raise OutraInstanciaAberta(dono.pid)
            # Tranca velha: tira-a do caminho com um nome so deste processo.
            # Se outra instancia a trocou entretanto por uma viva, devolve-a.
            posta_de_parte = self.caminho.with_name(f"{self.caminho.name}.{self.pid}.velha")
            try:
                os.replace(self.caminho, posta_de_parte)
            except FileNotFoundError:
                continue
            try:
                movida = self._ler(posta_de_parte)
            except FileNotFoundError:
                movida = None
            if movida != dono and self._dono_vivo(movida):
                try:
                    os.rename(posta_de_parte, self.caminho)
                except OSError:
                    pass
                assert movida is not None
                raise OutraInstanciaAberta(movida.pid)
            try:
                posta_de_parte.unlink()
            except OSError:
                pass
            substituida = dono or DonoDaTranca(0)
        # Tres corridas perdidas seguidas: alguem esta a arrancar agora mesmo.
        try:
            dono = self._ler(self.caminho)
        except FileNotFoundError:
            dono = None
        raise OutraInstanciaAberta(dono.pid if dono else 0)

    def libertar(self) -> None:
        """Apaga a tranca se ainda for deste processo. Pode chamar-se varias vezes."""
        if not self._minha:
            return
        self._minha = False
        try:
            dono = self._ler(self.caminho)
        except FileNotFoundError:
            return
        if dono is not None and dono.pid != self.pid:
            return
        try:
            self.caminho.unlink()
        except OSError:
            pass

    def __enter__(self) -> "TrancaDaInstancia":
        self.adquirir()
        return self

    def __exit__(self, *_excecao) -> None:
        self.libertar()
