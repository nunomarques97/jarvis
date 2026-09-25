r"""Cria o atalho local que arranca o jarvis com dois cliques.

    .venv\Scripts\python scripts/criar_atalho.py
    .venv\Scripts\python scripts/criar_atalho.py --sem-ativacao   # so a tecla de falar
    .venv\Scripts\python scripts/criar_atalho.py --simular        # mostra, nao escreve

Escreve em `.jarvis/atalho/` (pasta ignorada pelo Git, porque o atalho leva
os caminhos absolutos desta maquina):

  * `jarvis.lnk`: atalho do Windows para `.venv\Scripts\python.exe -m jarvis`,
    a comecar na raiz do repositorio. Pode ser copiado para o Ambiente de
    Trabalho ou afixado no menu Iniciar;
  * `jarvis.cmd`: o mesmo arranque num ficheiro de comandos com caminhos
    relativos, o recurso se o atalho nao puder ser criado.

O atalho e criado pelo PowerShell (WScript.Shell). Os caminhos passam por
variaveis de ambiente e nunca sao colados no texto do comando.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PASTA_DO_ATALHO = RAIZ / ".jarvis" / "atalho"

#: As unicas opcoes do jarvis que o atalho pode levar.
OPCOES_PERMITIDAS = ("--sem-ativacao", "--com-som", "--sem-voz")

#: O PowerShell le tudo das variaveis de ambiente: nada e interpolado.
SCRIPT_DO_ATALHO = (
    "$atalho = (New-Object -ComObject WScript.Shell).CreateShortcut($env:JARVIS_ATALHO); "
    "$atalho.TargetPath = $env:JARVIS_ALVO; "
    "$atalho.Arguments = $env:JARVIS_ARGUMENTOS; "
    "$atalho.WorkingDirectory = $env:JARVIS_PASTA; "
    "$atalho.Description = 'jarvis: controlo por voz do Claude Code'; "
    "$atalho.Save()"
)


def argumentos_do_jarvis(opcoes: list[str]) -> list[str]:
    """`-m jarvis` e as opcoes pedidas, so da lista fechada e sem repetir."""
    desconhecidas = [opcao for opcao in opcoes if opcao not in OPCOES_PERMITIDAS]
    if desconhecidas:
        raise ValueError(f"opcoes nao permitidas no atalho: {', '.join(desconhecidas)}")
    return ["-m", "jarvis", *dict.fromkeys(opcoes)]


def python_do_venv(raiz: Path = RAIZ) -> Path:
    return raiz / ".venv" / "Scripts" / "python.exe"


def conteudo_do_cmd(argumentos: list[str]) -> str:
    """O recurso: caminhos relativos a pasta do proprio ficheiro, nada da maquina."""
    return (
        "@echo off\r\n"
        'cd /d "%~dp0..\\.."\r\n'
        f'".venv\\Scripts\\python.exe" {" ".join(argumentos)} %*\r\n'
    )


def comando_do_powershell() -> list[str]:
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", SCRIPT_DO_ATALHO]


def ambiente_do_atalho(atalho: Path, alvo: Path, argumentos: list[str], pasta: Path) -> dict[str, str]:
    ambiente = dict(os.environ)
    ambiente.update(
        {
            "JARVIS_ATALHO": str(atalho),
            "JARVIS_ALVO": str(alvo),
            "JARVIS_ARGUMENTOS": " ".join(argumentos),
            "JARVIS_PASTA": str(pasta),
        }
    )
    return ambiente


def criar(
    opcoes: list[str],
    *,
    raiz: Path = RAIZ,
    pasta: Path | None = None,
    correr=subprocess.run,
    escrever=print,
) -> int:
    """Escreve o `.cmd` e tenta o `.lnk`. Devolve 0 se ao menos o `.cmd` ficou."""
    pasta = pasta or raiz / ".jarvis" / "atalho"
    argumentos = argumentos_do_jarvis(opcoes)
    alvo = python_do_venv(raiz)
    if not alvo.is_file():
        escrever(f"ERRO: nao ha {alvo.relative_to(raiz)}; cria primeiro o ambiente (ver README)")
        return 1
    pasta.mkdir(parents=True, exist_ok=True)
    cmd = pasta / "jarvis.cmd"
    cmd.write_text(conteudo_do_cmd(argumentos), encoding="ascii", newline="")
    escrever(f"escrito: {cmd.relative_to(raiz)}")
    lnk = pasta / "jarvis.lnk"
    try:
        saida = correr(
            comando_do_powershell(),
            env=ambiente_do_atalho(lnk, alvo, argumentos, raiz),
            capture_output=True,
            text=True,
            timeout=60,
        )
        criado = saida.returncode == 0 and lnk.is_file()
        motivo = (saida.stderr or saida.stdout or "").strip()[-300:]
    except (OSError, subprocess.TimeoutExpired) as erro:
        criado, motivo = False, str(erro)
    if criado:
        escrever(f"escrito: {lnk.relative_to(raiz)} (copia-o para o Ambiente de Trabalho se quiseres)")
    else:
        escrever(f"AVISO: o atalho .lnk nao foi criado ({motivo or 'sem motivo'}); usa o jarvis.cmd")
    return 0


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/criar_atalho.py",
        description="Cria em .jarvis/atalho/ (pasta ignorada) o atalho que arranca o jarvis.",
    )
    parser.add_argument("--sem-ativacao", action="store_true", help="o atalho arranca so com a tecla de falar")
    parser.add_argument("--com-som", action="store_true", help="o atalho arranca com o bip no inicio e fim da escuta")
    parser.add_argument("--simular", action="store_true", help="mostra o que faria, sem escrever nada")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = construir_parser().parse_args(argv)
    opcoes = [opcao for opcao, ligada in (("--sem-ativacao", args.sem_ativacao), ("--com-som", args.com_som)) if ligada]
    if args.simular:
        argumentos = argumentos_do_jarvis(opcoes)
        print(f"pasta: {PASTA_DO_ATALHO.relative_to(RAIZ)} (ignorada pelo Git)")
        print(f"arranque: .venv\\Scripts\\python.exe {' '.join(argumentos)}")
        return 0
    return criar(opcoes)


if __name__ == "__main__":
    raise SystemExit(main())
