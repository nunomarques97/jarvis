r"""Acoes locais da lista branca fechada da D4: EXECUTA o que o router (T4)
so descreve.

`jarvis/router.py` decide e devolve uma `ResultadoRouter` com `tipo="local"`,
`nome_acao` e `argumento` — mas nunca executa nada (D48.2: "quem executa e a
T5"). Este modulo e essa T5: pega no `ResultadoRouter` (ou, na CLI de teste,
num nome de accao e num nome de projeto dados diretamente) e corre a accao a
serio, ou so imprime o que correria (`--simular`).

ORDEM DE CORTE (D13/D35), OBRIGATORIOS nesta task: (a) horas e data; (b) abrir
o VS Code num projeto da configuracao. Os restantes tres da lista branca da D4
— abrir uma pasta, calar, adormecer e acordar — ficam de fora desta task:
`abrir_pasta` esta implementada porque o custo extra sobre `abrir_vscode` era
quase zero (mesma resolucao de projeto, so muda o executavel), mas `calar`,
`adormecer` e `acordar` PRECISAM de um processo `jarvis` vivo com estado (a
consola a ouvir, o microfone aberto) que so a T6 (`jarvis/app.py`) vai criar —
nao ha nada aqui para "calar" ou "adormecer" ainda. `executar()` recusa-as com
`AcaoError`, citando a T6, para nao fingir uma accao que nao faz nada.

REGRAS DE SEGURANCA (D48, D12), nao negociaveis:
  - as accoes SO usam caminhos que vieram da configuracao (`jarvis.config`),
    NUNCA texto da transcricao — o `argumento` que `executar()` recebe de um
    `ResultadoRouter` "abrir_vscode"/"abrir_pasta" e sempre revalidado contra
    `config.projetos` (`_projeto_pelo_caminho`), nunca aceite em bruto;
  - todo o subprocess corre com uma LISTA de argumentos e `shell=False`, nunca
    com uma linha de comandos composta a mao;
  - o executavel e sempre resolvido para o binario REAL (`Code.exe`,
    `explorer.exe`), nunca para um shim `.cmd`/`.bat` que reabriria o
    cmd.exe a reparsear a linha toda (D48.1) — `localizar_code_exe()` segue a
    mesma logica de `jarvis.canal_claude.localizar_cli()` para o `claude.CMD`
    do npm, aplicada ao `code.cmd` da instalacao do VS Code;
  - nenhuma accao toca noutro repositorio do Sponsor, em corretoras, carteiras
    ou ordens (D12) — a unica coisa que uma accao local sabe abrir e um
    projeto da configuracao privada, e a lista branca fechada da D4 nao tem
    nenhuma accao capaz de negociar.

Uso como biblioteca:

    from jarvis.config import carregar_config
    from jarvis.router import encaminhar
    from jarvis.acoes_locais import executar

    config = carregar_config()
    resultado_router = encaminhar("que horas sao", config)
    resultado_acao = executar(resultado_router, config)

Uso na linha de comandos (accao direta, sem passar pelo router — para testar):

    .venv\Scripts\python -m jarvis.acoes_locais horas
    .venv\Scripts\python -m jarvis.acoes_locais data
    .venv\Scripts\python -m jarvis.acoes_locais abrir-vscode <projeto> [--simular]
    .venv\Scripts\python -m jarvis.acoes_locais abrir-pasta <projeto> [--simular]
    .venv\Scripts\python -m jarvis.acoes_locais --config <caminho> ...
    .venv\Scripts\python -m jarvis.acoes_locais --sem-voz ...   # nao fala, so imprime

Autoteste das partes puras, com uma configuracao ficticia em memoria (nunca
com o config.toml real):

    .venv\Scripts\python -m jarvis.acoes_locais --autoteste
"""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.canal_claude import EXTENSOES_QUE_PASSAM_PELO_SHELL, verificar_executavel_seguro  # noqa: E402
from jarvis.config import CAMINHO_CONFIG_PADRAO, Config, ConfigError, Projeto, carregar_config  # noqa: E402
from jarvis.router import ResultadoRouter  # noqa: E402

#: Nomes de accao que a lista branca da D4 conhece e que esta task implementa
#: (as tres restantes — calar, adormecer, acordar — precisam de um processo
#: jarvis vivo, que so a T6 cria; ver o docstring do modulo).
ACOES_IMPLEMENTADAS = frozenset({"horas_e_data", "abrir_vscode", "abrir_pasta"})

#: As tres accoes da D4 que ficam por fazer nesta task, e porque (D13/D35: a
#: ordem de corte deixa-as de fora quando o tempo aperta; aqui ficam de fora
#: porque dependem de estado que ainda nao existe, nao so por falta de tempo).
ACOES_ADIADAS_PARA_A_T6 = ("calar", "adormecer", "acordar")

MESES_PT = (
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)
DIAS_SEMANA_PT = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)


class AcaoError(Exception):
    """Uma accao local nao pode ser executada (projeto desconhecido, accao
    ainda nao implementada, executavel em falta, ...). Nunca uma excecao de
    baixo nivel do subprocess ou do sistema de ficheiros a escapar em bruto."""


@dataclass(frozen=True)
class ResultadoAcao:
    """O que uma accao fez, ou teria feito em --simular. Nunca guarda texto
    vindo da transcricao (D48.2): so o que a propria accao construiu."""

    nome_acao: str
    #: True so quando o subprocess foi mesmo arrancado (nunca em --simular).
    executou: bool
    #: A frase para o Sponsor ouvir/ler (D2/D11): a resposta da accao.
    texto: str
    #: A linha de comando exata (lista de argumentos, ja formatada para
    #: leitura humana com subprocess.list2cmdline) — preenchida so nas accoes
    #: que arrancam um processo externo (abrir_vscode, abrir_pasta).
    comando: str = ""
    projeto: str | None = None


def _texto_horas(agora: datetime.datetime) -> str:
    if agora.minute == 0:
        return f"São {agora.hour} horas."
    return f"São {agora.hour} horas e {agora.minute} minutos."


def _texto_data(agora: datetime.datetime) -> str:
    dia_semana = DIAS_SEMANA_PT[agora.weekday()]
    mes = MESES_PT[agora.month - 1]
    return f"Hoje é {dia_semana}, dia {agora.day} de {mes} de {agora.year}."


def horas_e_data(argumento: str | None, *, agora: datetime.datetime | None = None) -> ResultadoAcao:
    """D4.a: diz a hora ou a data. `argumento` vem do router ("horas"/"data")."""
    agora = agora or datetime.datetime.now()
    if argumento == "data":
        texto = _texto_data(agora)
    else:
        texto = _texto_horas(agora)
    return ResultadoAcao(nome_acao="horas_e_data", executou=True, texto=texto)


def _projeto_conhecido(nome: str, config: Config) -> Projeto:
    """O Projeto com este nome exato na config, ou AcaoError (D4: nunca 'o
    mais parecido' — quem ja decidiu isso e o router; aqui so se confirma)."""
    projeto = config.encontrar_projeto(nome)
    if projeto is None:
        nomes = ", ".join(sorted(p.nome for p in config.projetos)) or "(nenhum)"
        raise AcaoError(
            f"projeto '{nome}' nao esta na configuracao. Projetos conhecidos: {nomes}. "
            "Uma accao local nunca adivinha nem executa 'o mais parecido' (D4)."
        )
    return projeto


def _projeto_pelo_caminho(caminho_texto: str, config: Config) -> Projeto:
    """O Projeto cujo caminho RESOLVIDO e exatamente este (D48.2).

    Usado quando o argumento vem de um `ResultadoRouter` (o router ja
    resolveu o caminho a partir da config): revalida-se contra a config em
    vez de confiar em bruto no texto, para que um bug no router nunca deixe
    passar um caminho que nao veio de la.
    """
    alvo = Path(caminho_texto)
    for projeto in config.projetos:
        if projeto.caminho == alvo:
            return projeto
    raise AcaoError(
        f"caminho '{caminho_texto}' nao corresponde a nenhum projeto da configuracao; "
        "uma accao local nunca executa um caminho que nao veio da config (D48.2)."
    )


def localizar_code_exe(ambiente: dict[str, str] | None = None) -> Path:
    """O `Code.exe` REAL desta maquina, nunca o shim `code.cmd` (D48.1).

    Mesma logica de `jarvis.canal_claude.localizar_cli()` para o `claude.CMD`
    do npm: o `shutil.which("code")` devolve tipicamente o `code.cmd` da
    instalacao do VS Code (`<instalacao>\\bin\\code.cmd`), e arrancar um .cmd
    faz o Windows reabrir o cmd.exe a reparsear a linha de comandos inteira.
    O binario real fica um nivel acima de `bin\\`, em `<instalacao>\\Code.exe`.
    """
    env = dict(os.environ if ambiente is None else ambiente)
    candidatos: list[Path] = []

    atalho = shutil.which("code", path=env.get("PATH"))
    if atalho:
        alvo = Path(atalho)
        if alvo.suffix.lower() in EXTENSOES_QUE_PASSAM_PELO_SHELL:
            candidatos.append(alvo.parent.parent / "Code.exe")
        else:
            candidatos.append(alvo)

    localapp = env.get("LOCALAPPDATA")
    if localapp:
        candidatos.append(Path(localapp) / "Programs" / "Microsoft VS Code" / "Code.exe")
    programfiles = env.get("ProgramFiles")
    if programfiles:
        candidatos.append(Path(programfiles) / "Microsoft VS Code" / "Code.exe")
    programfilesx86 = env.get("ProgramFiles(x86)")
    if programfilesx86:
        candidatos.append(Path(programfilesx86) / "Microsoft VS Code" / "Code.exe")

    vistos: list[Path] = []
    for candidato in candidatos:
        if candidato not in vistos:
            vistos.append(candidato)
    for candidato in vistos:
        if candidato.is_file() and candidato.suffix.lower() not in EXTENSOES_QUE_PASSAM_PELO_SHELL:
            verificar_executavel_seguro(candidato)
            return candidato
    procurados = "\n".join(f"  - {caminho}" for caminho in vistos) or "  (nenhum)"
    raise AcaoError(
        "executavel real do VS Code (Code.exe) nao encontrado. Por D48(1) o jarvis nunca "
        f"arranca o shim code.cmd, por isso nao ha alternativa: procurei em\n{procurados}\n"
        "Instala o VS Code (https://code.visualstudio.com/) ou acrescenta-o ao PATH."
    )


def localizar_explorer_exe(ambiente: dict[str, str] | None = None) -> Path:
    """O `explorer.exe` REAL do Windows (D48.1: mesma regra, binario de sistema)."""
    env = dict(os.environ if ambiente is None else ambiente)
    candidatos: list[Path] = []
    windir = env.get("WINDIR") or env.get("SystemRoot")
    if windir:
        candidatos.append(Path(windir) / "explorer.exe")
    atalho = shutil.which("explorer", path=env.get("PATH"))
    if atalho:
        candidatos.append(Path(atalho))

    vistos: list[Path] = []
    for candidato in candidatos:
        if candidato not in vistos:
            vistos.append(candidato)
    for candidato in vistos:
        if candidato.is_file() and candidato.suffix.lower() not in EXTENSOES_QUE_PASSAM_PELO_SHELL:
            verificar_executavel_seguro(candidato)
            return candidato
    procurados = "\n".join(f"  - {caminho}" for caminho in vistos) or "  (nenhum)"
    raise AcaoError(f"explorer.exe nao encontrado neste Windows. Procurei em\n{procurados}")


def _abrir_com(
    nome_acao: str,
    localizar_exe,
    projeto: Projeto,
    *,
    simular: bool,
) -> ResultadoAcao:
    """O corpo comum de abrir_vscode/abrir_pasta: resolve o exe, monta a lista
    de argumentos (nunca uma linha de comandos composta a mao), e ou arranca o
    subprocess ou so devolve a linha exata que arrancaria (--simular)."""
    executavel = localizar_exe()
    # O UNICO valor que entra na lista de argumentos e o caminho JA RESOLVIDO
    # que veio da config (nunca texto de transcricao, D48.2).
    comando = [str(executavel), str(projeto.caminho)]
    linha = subprocess.list2cmdline(comando)
    if simular:
        return ResultadoAcao(
            nome_acao=nome_acao,
            executou=False,
            texto=f"(simulado, nada foi aberto) {nome_acao.replace('_', ' ')} em '{projeto.nome}'.",
            comando=linha,
            projeto=projeto.nome,
        )
    subprocess.Popen(comando, shell=False, cwd=str(projeto.caminho))
    return ResultadoAcao(
        nome_acao=nome_acao,
        executou=True,
        texto=f"A abrir {nome_acao.replace('_', ' ')} em '{projeto.nome}'.",
        comando=linha,
        projeto=projeto.nome,
    )


def abrir_vscode(projeto: Projeto, *, simular: bool = False) -> ResultadoAcao:
    """D4.b: abre o VS Code na pasta de um projeto conhecido."""
    return _abrir_com("abrir_vscode", localizar_code_exe, projeto, simular=simular)


def abrir_pasta(projeto: Projeto, *, simular: bool = False) -> ResultadoAcao:
    """D4.c: abre a pasta de um projeto conhecido no explorador do Windows."""
    return _abrir_com("abrir_pasta", localizar_explorer_exe, projeto, simular=simular)


def executar(resultado_router: ResultadoRouter, config: Config, *, simular: bool = False) -> ResultadoAcao:
    """Executa (ou simula) a accao que `jarvis.router.encaminhar()` descreveu.

    Levanta `AcaoError` se `resultado_router.tipo != "local"`, se a accao
    ainda nao estiver implementada (D13/D35, ver ACOES_ADIADAS_PARA_A_T6), ou
    se o argumento nao corresponder a nenhum projeto da configuracao.
    """
    if resultado_router.tipo != "local":
        raise AcaoError(
            f"executar() so aceita resultados 'local' do router; recebi "
            f"'{resultado_router.tipo}' ({resultado_router.motivo})"
        )
    nome_acao = resultado_router.nome_acao
    if nome_acao in ACOES_ADIADAS_PARA_A_T6:
        raise AcaoError(
            f"accao '{nome_acao}' e da lista branca da D4 mas ainda nao esta implementada "
            "nesta task (D13/D35): precisa de um processo jarvis vivo com estado, que so a "
            "T6 (jarvis/app.py) cria. Registado no relatorio da T5."
        )
    if nome_acao == "horas_e_data":
        return horas_e_data(resultado_router.argumento)
    if nome_acao in ("abrir_vscode", "abrir_pasta"):
        if not resultado_router.argumento:
            raise AcaoError(f"accao '{nome_acao}' sem argumento (caminho do projeto); router inconsistente")
        projeto = _projeto_pelo_caminho(resultado_router.argumento, config)
        funcao = abrir_vscode if nome_acao == "abrir_vscode" else abrir_pasta
        return funcao(projeto, simular=simular)
    raise AcaoError(f"accao desconhecida '{nome_acao}': fora da lista branca da D4")


# --- Autoteste das partes puras (config ficticia em memoria) ---------------


def _autoteste() -> int:
    import tempfile

    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    def apanhar(funcao) -> str:
        try:
            funcao()
        except AcaoError as erro:
            return str(erro)
        return ""

    # 1. horas e data: distinguidas pelo argumento, sem tocar em rede/disco.
    agora = datetime.datetime(2026, 9, 20, 15, 30)
    verificar("horas: texto tem a hora", "15" in horas_e_data("horas", agora=agora).texto, True)
    verificar(
        "data: texto tem o dia da semana e o ano",
        ("domingo" in _texto_data(agora)) and ("2026" in _texto_data(agora)),
        True,
    )
    verificar("horas/data: sempre executou=True (nunca toca em subprocess)", horas_e_data("horas", agora=agora).executou, True)

    with tempfile.TemporaryDirectory() as pasta:
        projeto_a = Path(pasta) / "projeto-a"
        projeto_a.mkdir()
        config = Config(
            microfone="Microfone de Teste",
            projetos=(Projeto(nome="projeto-a", caminho=projeto_a.resolve()),),
        )

        # 2. --simular nunca arranca nada e devolve a linha de comando exata.
        resultado = abrir_vscode(config.projetos[0], simular=True)
        verificar("simular: executou=False", resultado.executou, False)
        verificar("simular: a linha de comando tem o caminho do projeto", str(projeto_a.resolve()) in resultado.comando, True)
        verificar("simular: a linha de comando comeca pelo executavel", resultado.comando.split(" ")[0] not in ("code", "code.cmd"), True)

        resultado_pasta = abrir_pasta(config.projetos[0], simular=True)
        verificar("simular (pasta): executou=False", resultado_pasta.executou, False)
        verificar("simular (pasta): explorer.exe na linha de comando", "explorer.exe" in resultado_pasta.comando.lower(), True)

        # 3. um projeto que nao esta na config e SEMPRE recusado, nunca "o
        # mais parecido" (D4) — tanto por nome direto como pelo caminho que um
        # ResultadoRouter traria.
        verificar(
            "projeto desconhecido (por nome): recusado com mensagem legivel",
            "nao esta na configuracao" in apanhar(lambda: _projeto_conhecido("projeto-fantasma", config)),
            True,
        )
        verificar(
            "projeto desconhecido (por caminho): recusado, nunca executa",
            "nao corresponde a nenhum projeto" in apanhar(lambda: _projeto_pelo_caminho(str(Path(pasta) / "outro-sitio"), config)),
            True,
        )

        # 4. executar() a partir de um ResultadoRouter, como o router (T4)
        # devolveria de verdade — nunca confia em bruto no argumento.
        resultado_horas = executar(ResultadoRouter("local", nome_acao="horas_e_data", argumento="horas"), config)
        verificar("executar(): horas_e_data devolve texto com a hora", bool(resultado_horas.texto), True)

        resultado_router_vscode = ResultadoRouter(
            "local", nome_acao="abrir_vscode", argumento=str(projeto_a.resolve())
        )
        resultado_exec = executar(resultado_router_vscode, config, simular=True)
        verificar("executar(): abrir_vscode via router, simulado", resultado_exec.executou, False)
        verificar("executar(): projeto identificado", resultado_exec.projeto, "projeto-a")

        # 5. um ResultadoRouter com um caminho que NAO veio da config nunca
        # executa (defesa em profundidade contra um bug no router, D48.2).
        resultado_router_falso = ResultadoRouter(
            "local", nome_acao="abrir_vscode", argumento=str(Path(pasta) / "nunca-esteve-na-config")
        )
        verificar(
            "executar(): argumento fora da config e sempre recusado",
            "nao corresponde a nenhum projeto" in apanhar(lambda: executar(resultado_router_falso, config, simular=True)),
            True,
        )

        # 6. as tres accoes adiadas para a T6 sao recusadas com uma razao
        # clara, nunca fingidas como "feitas".
        for nome_acao in ACOES_ADIADAS_PARA_A_T6:
            resultado_adiado = ResultadoRouter("local", nome_acao=nome_acao)
            verificar(
                f"executar(): '{nome_acao}' adiada para a T6, nunca finge sucesso",
                "T6" in apanhar(lambda r=resultado_adiado: executar(r, config)),
                True,
            )

        # 7. executar() so aceita resultados "local"; "claude"/"nada" sao erro
        # de quem chamou, nunca uma accao silenciosa.
        verificar(
            "executar(): recusa um ResultadoRouter que nao e 'local'",
            "so aceita resultados 'local'" in apanhar(lambda: executar(ResultadoRouter("claude", texto="oi"), config)),
            True,
        )

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste das accoes locais completo (horas/data, abrir, simular, recusas D4/D48.2).")
    return 0


def _resposta(resultado: ResultadoAcao, *, sem_voz: bool) -> None:
    """Imprime a resposta e, salvo --sem-voz, di-la em voz alta (D35.4)."""
    print(f"resposta = {resultado.texto}")
    if sem_voz:
        return
    from jarvis.voz import falar

    falar(resultado.texto)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config",
        default=None,
        help=f"caminho do config.toml (default: {CAMINHO_CONFIG_PADRAO.name} na raiz do repositorio)",
    )
    parser.add_argument(
        "--sem-voz",
        action="store_true",
        help="nao fala a resposta em voz alta, so imprime (default: fala via Piper, com fallback D35.4)",
    )
    parser.add_argument("--autoteste", action="store_true", help="corre o autoteste com config ficticia")

    sub = parser.add_subparsers(dest="comando")
    sub.add_parser("horas", help="diz a hora atual (D4.a)")
    sub.add_parser("data", help="diz a data de hoje (D4.a)")

    p_vscode = sub.add_parser("abrir-vscode", help="abre o VS Code num projeto conhecido (D4.b)")
    p_vscode.add_argument("projeto", help="nome do projeto, tal como esta em [[projetos]] na config")
    p_vscode.add_argument("--simular", action="store_true", help="so imprime a linha de comando, nao abre nada")

    p_pasta = sub.add_parser("abrir-pasta", help="abre a pasta de um projeto conhecido no explorador (D4.c)")
    p_pasta.add_argument("projeto", help="nome do projeto, tal como esta em [[projetos]] na config")
    p_pasta.add_argument("--simular", action="store_true", help="so imprime a linha de comando, nao abre nada")

    args = parser.parse_args(argv)

    if args.autoteste:
        return _autoteste()

    if not args.comando:
        parser.error("e preciso um comando (horas, data, abrir-vscode, abrir-pasta) ou --autoteste")

    try:
        if args.comando in ("horas", "data"):
            resultado = horas_e_data(args.comando)
            _resposta(resultado, sem_voz=args.sem_voz)
            return 0

        # abrir-vscode / abrir-pasta precisam da configuracao. Em --simular
        # nada toca no disco, por isso os caminhos ficticios do
        # config.exemplo.toml (D10) tambem podem ser pre-vistos sem existirem
        # de verdade — fora de --simular a validacao de caminho continua
        # sempre ligada (validar_caminhos, jarvis/config.py).
        caminho_config = args.config if args.config is not None else CAMINHO_CONFIG_PADRAO
        config = carregar_config(caminho_config, validar_caminhos=not args.simular)
        projeto = _projeto_conhecido(args.projeto, config)
        funcao = abrir_vscode if args.comando == "abrir-vscode" else abrir_pasta
        resultado = funcao(projeto, simular=args.simular)
        print(f"comando  = {resultado.comando}")
        _resposta(resultado, sem_voz=args.sem_voz)
        return 0
    except (AcaoError, ConfigError) as erro:
        print(f"ERRO: {erro}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
