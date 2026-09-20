r"""Corre a escada do canal para o Claude Code e escreve o ficheiro de prova.

Testa, por esta ordem e com 60 s de limite por degrau (D40/D8, TECHNOLOGY.md S6):

  1   Remote Control da app  -> VETADO (D40/D31): nao e tentado, e documentado.
  2a  CLI claude em stream-json sobre subprocess.Popen.
  2b  O mesmo sobre pywinpty (so corre se o 2a falhar: a D46 autoriza instalar
      o pywinpty apenas nesse caso).
  3   pywinauto na janela do terminal (so corre se o 2a e o 2b falharem).
  4   claude -p --resume <session-id> por frase: corre sempre, porque nao precisa
      de instalar nada e e o plano B registado.

A frase de teste e exatamente "responde apenas OK". Um degrau so PASSA se a
resposta do modelo voltar em texto para dentro deste processo. A sessao filha
corre nesta pasta (D12) e sem ferramenta nenhuma.

Depois da escada correm SEMPRE seis provas negativas de seguranca (D48), que
sao a resposta ao SECURITY-REJECT da tentativa 1: uma frase com aspas e `&` nao
executa nada, e o valor de uma variavel de ambiente nunca entra no prompt. Elas
passam quando nada acontece, e o ficheiro de prova leva-as escritas — e assim
que um "PASS" da escada deixa de poder esconder uma injecao.

Correr a partir da raiz do repositorio:
    .venv\Scripts\python scripts/testar_canal.py

Opcoes:
    --frase "<texto>"       frase a entregar (default: responde apenas OK)
    --timeout <segundos>    limite por degrau (default: 60)
    --degraus 2a,4          corre so estes degraus da escada
    --saida <caminho>       ficheiro de prova (default: docs/forja/evidence/canal-<ts>.md)
    --sem-prova-seguranca   salta os testes negativos (so para depurar)

Codigos de saida: 0 = um degrau entregou a frase e a seguranca passou;
1 = nenhum degrau entregou a frase; 3 = uma prova de seguranca falhou.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.canal_claude import (  # noqa: E402
    COMANDO_INSTALAR_PYWINAUTO,
    COMANDO_INSTALAR_PYWINPTY,
    DEGRAU_ESCOLHIDO,
    EXTENSOES_QUE_PASSAM_PELO_SHELL,
    FRASE_DE_TESTE,
    ORDEM_DA_ESCADA,
    TIMEOUT_POR_DEGRAU_S,
    TITULO_JANELA_PADRAO,
    VARIAVEL_TITULO_JANELA,
    ResultadoDegrau,
    degrau_1_remote_control,
    degrau_2a_subprocess_stream_json,
    degrau_2b_pywinpty,
    degrau_3_pywinauto,
    degrau_4_claude_p_resume,
    localizar_cli,
    titulo_da_janela_configurado,
)
from jarvis.consola import forcar_consola_utf8  # noqa: E402

PASTA_DE_PROVA = RAIZ / "docs" / "forja" / "evidence"

NOMES = {
    "2a": "CLI claude em stream-json sobre subprocess.Popen",
    "2b": "CLI claude em stream-json sobre pywinpty (ConPTY)",
    "3": "pywinauto a escrever na janela do terminal com a sessao aberta",
    "4": "claude -p --resume <session-id> por frase (divida assumida)",
}


def nao_tentado(degrau: str, porque: str) -> ResultadoDegrau:
    return ResultadoDegrau(
        degrau=degrau,
        nome=NOMES[degrau],
        comando="(nao executado)",
        estado="NAO TENTADO",
        erro=porque,
    )


def versao_do_cli() -> str:
    try:
        cli = localizar_cli()
    except FileNotFoundError as erro:
        return f"(nao encontrado: {erro})"
    try:
        saida = subprocess.run(
            [cli, "--version"], capture_output=True, text=True, timeout=30
        )
        return f"{saida.stdout.strip() or saida.stderr.strip()}  [{cli}]"
    except Exception as erro:  # noqa: BLE001
        return f"(falhou a perguntar a versao: {erro})"


def correr_escada(
    frase: str, limite_s: float, degraus: tuple[str, ...]
) -> list[ResultadoDegrau]:
    resultados: list[ResultadoDegrau] = [degrau_1_remote_control()]
    anunciar(resultados[0])
    passou_antes = False

    for degrau in ORDEM_DA_ESCADA:
        if degrau not in degraus:
            resultados.append(nao_tentado(degrau, "excluido por --degraus nesta execucao"))
            anunciar(resultados[-1])
            continue
        if degrau == "2a":
            resultado = degrau_2a_subprocess_stream_json(frase, limite_s, cwd=RAIZ)
        elif degrau == "2b":
            if passou_antes:
                resultado = nao_tentado(
                    "2b",
                    "o degrau 2a passou; a D40/D46 so manda tentar o pywinpty se o 2a "
                    f"falhar, e so nesse caso autoriza instala-lo ({COMANDO_INSTALAR_PYWINPTY}). "
                    "Nada foi instalado.",
                )
            else:
                resultado = degrau_2b_pywinpty(frase, limite_s, cwd=RAIZ)
        elif degrau == "3":
            if passou_antes:
                resultado = nao_tentado(
                    "3",
                    "um degrau anterior passou; a D40/D46 so manda tentar o pywinauto se o "
                    f"degrau 2 falhar ({COMANDO_INSTALAR_PYWINAUTO}). Nada foi instalado. "
                    f"Titulo da janela que usaria: '{titulo_da_janela_configurado()}' "
                    f"(de {VARIAVEL_TITULO_JANELA}, default '{TITULO_JANELA_PADRAO}').",
                )
            else:
                resultado = degrau_3_pywinauto(frase, limite_s)
        else:  # degrau 4: corre sempre, nao instala nada e e o plano B registado
            resultado = degrau_4_claude_p_resume(frase, limite_s, cwd=RAIZ)
        passou_antes = passou_antes or resultado.passou
        resultados.append(resultado)
        anunciar(resultado)
    return resultados


def anunciar(resultado: ResultadoDegrau) -> None:
    tempo = (
        f"{resultado.segundos_ate_primeira_resposta:.2f} s"
        if resultado.segundos_ate_primeira_resposta is not None
        else "-"
    )
    print(f"  degrau {resultado.degrau:<3} {resultado.estado:<11} 1a resposta: {tempo:<9} "
          f"{resultado.nome}", flush=True)
    if resultado.estado == "PASS":
        print(f"      resposta capturada: {resultado.resposta.replace(chr(10), ' ')[:120]!r}",
              flush=True)
    elif resultado.erro:
        primeira = resultado.erro.strip().splitlines()[0] if resultado.erro.strip() else ""
        print(f"      {primeira[:160]}", flush=True)


# --- Prova de seguranca: os dois bloqueadores da tentativa 1 (D48) --------
#
# Sao testes NEGATIVOS: passam quando nada acontece. Rebentavam no codigo da
# tentativa 1 (o alvo era o shim claude.CMD e a frase ia em argv) e tem de
# continuar a correr em cada execucao, porque e a unica forma de a prova nao
# voltar a ser um "PASS" silencioso por cima de uma injecao.

MARCA_INJECAO = "EXECUTADO_PELA_INJECAO"
VARIAVEL_FALSA = "JARVIS_PROVA_SEGREDO_FALSO"
VALOR_FALSO = "SEGREDO-FALSO-DO-TESTE-0000"
#: Nome usado na sonda de mecanismo, com um valor falso: e o mesmo nome do
#: achado do Security Reviewer. Nunca e definido para o processo `claude`
#: (definir uma ANTHROPIC_API_KEY mudaria a autenticacao da sessao filha), so
#: para o gravador local que nao fala com ninguem.
VARIAVEL_DA_SONDA = "ANTHROPIC_API_KEY"
VALOR_FALSO_DA_SONDA = "sk-ant-FALSA-PARA-TESTE-0000"


@dataclass
class ProvaSeguranca:
    codigo: str
    titulo: str
    estado: str  # "PASS" ou "FAIL"
    criterio: str
    obtido: str
    detalhes: list[str] = field(default_factory=list)


def _escrever_gravador(pasta: Path) -> tuple[Path, Path, Path]:
    """Um programa que so regista o que recebeu, e uma replica do shim do npm.

    O gravador faz o papel do `claude.exe`: e um executavel real (o python.exe)
    com uma lista de argumentos. A replica do shim (`.cmd`) faz o papel do
    `claude.CMD` do npm: `@ECHO off` + chamada ao executavel com `%*`, que e o
    que poe o cmd.exe a reparsear a linha de comandos.
    """
    argv = pasta / "gravador_argv.py"
    argv.write_text(
        "import os, sys, pathlib\n"
        "pathlib.Path(os.environ['PROVA_DESTINO']).write_text("
        "repr(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    entrada = pasta / "gravador_stdin.py"
    entrada.write_text(
        "import os, sys, pathlib\n"
        "pathlib.Path(os.environ['PROVA_DESTINO']).write_text("
        "repr(sys.stdin.read()), encoding='utf-8')\n",
        encoding="utf-8",
    )
    shim = pasta / "gravador.cmd"
    shim.write_text(
        f'@ECHO off\r\n"{sys.executable}" "{argv}" %*\r\n', encoding="utf-8"
    )
    return argv, entrada, shim


def _correr_sonda(
    alvo: list[str], pasta: Path, extra_env: dict[str, str], frase: str | None = None
) -> str:
    """Corre o gravador e devolve o que ele registou (argv ou stdin)."""
    destino = pasta / "gravado.txt"
    if destino.exists():
        destino.unlink()
    ambiente = dict(os.environ)
    ambiente.update(extra_env)
    ambiente["PROVA_DESTINO"] = str(destino)
    subprocess.run(
        alvo,
        cwd=str(pasta),
        env=ambiente,
        input=frase,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    return destino.read_text(encoding="utf-8") if destino.exists() else "(nada gravado)"


def _session_id_das_notas(resultado: ResultadoDegrau) -> str | None:
    for nota in resultado.notas:
        if "session_id da resposta = " in nota:
            valor = nota.split("session_id da resposta = ", 1)[1].strip()
            return valor if valor and valor != "None" else None
    return None


def provas_de_seguranca(limite_s: float) -> list[ProvaSeguranca]:
    provas: list[ProvaSeguranca] = []
    pasta = Path(tempfile.mkdtemp(prefix="jarvis-prova-seg-"))
    try:
        argv_py, stdin_py, shim = _escrever_gravador(pasta)
        payload = f'ola" & echo {MARCA_INJECAO} > marca_injecao.txt & "'
        marca_local = pasta / "marca_injecao.txt"

        # P1 - mecanismo da injecao: o shim executa, o executavel real nao.
        gravado_shim = _correr_sonda([str(shim), payload], pasta, {})
        criou_pelo_shim = marca_local.exists()
        if marca_local.exists():
            marca_local.unlink()
        gravado_exe = _correr_sonda([sys.executable, str(argv_py), payload], pasta, {})
        criou_pelo_exe = marca_local.exists()
        provas.append(
            ProvaSeguranca(
                codigo="P1",
                titulo="Injecao de comandos: o shim .CMD executa a frase, o executavel real nao",
                estado="PASS" if (criou_pelo_shim and not criou_pelo_exe) else "FAIL",
                criterio=(
                    "controlo (shim .cmd, o que a tentativa 1 fazia): cria o ficheiro; "
                    "caminho em vigor (executavel real + lista de argumentos): nao cria nada"
                ),
                obtido=(
                    f"shim criou o ficheiro: {criou_pelo_shim} | "
                    f"executavel real criou o ficheiro: {criou_pelo_exe}"
                ),
                detalhes=[
                    f"frase usada: {payload!r}",
                    f"o shim gravou: {gravado_shim}",
                    f"o executavel real gravou (frase intacta num so argumento): {gravado_exe}",
                ],
            )
        )

        # P2 - mecanismo da expansao de %VARIAVEL%.
        frase_var = f"repete isto: %{VARIAVEL_DA_SONDA}%"
        env_falso = {VARIAVEL_DA_SONDA: VALOR_FALSO_DA_SONDA}
        var_shim = _correr_sonda([str(shim), frase_var], pasta, env_falso)
        var_exe = _correr_sonda([sys.executable, str(argv_py), frase_var], pasta, env_falso)
        expandiu_no_shim = VALOR_FALSO_DA_SONDA in var_shim
        literal_no_exe = f"%{VARIAVEL_DA_SONDA}%" in var_exe and VALOR_FALSO_DA_SONDA not in var_exe
        provas.append(
            ProvaSeguranca(
                codigo="P2",
                titulo="Expansao de %VARIAVEL%: o shim .CMD expande, o executavel real entrega literal",
                estado="PASS" if (expandiu_no_shim and literal_no_exe) else "FAIL",
                criterio=(
                    "controlo (shim .cmd): o VALOR da variavel substitui o nome; "
                    "caminho em vigor: chega literal, o valor nunca aparece"
                ),
                obtido=(
                    f"shim entregou o valor da variavel: {expandiu_no_shim} | "
                    f"executavel real entregou o texto literal: {literal_no_exe}"
                ),
                detalhes=[
                    f"variavel FALSA definida so para esta sonda: {VARIAVEL_DA_SONDA}="
                    f"{VALOR_FALSO_DA_SONDA} (nao e uma chave real; nunca e definida para o "
                    "processo `claude`, que autentica pela subscricao)",
                    f"frase usada: {frase_var!r}",
                    f"o shim gravou: {var_shim}",
                    f"o executavel real gravou: {var_exe}",
                ],
            )
        )

        # P3 - a frase por stdin chega byte a byte (o transporte do degrau 4).
        frase_stdin = f"{payload} / repete isto: %{VARIAVEL_DA_SONDA}%"
        gravado_stdin = _correr_sonda(
            [sys.executable, str(stdin_py)], pasta, env_falso, frase=frase_stdin
        )
        stdin_fiel = repr(frase_stdin) == gravado_stdin
        provas.append(
            ProvaSeguranca(
                codigo="P3",
                titulo="A frase por stdin chega ao processo filho identica (transporte do degrau 4)",
                estado="PASS" if stdin_fiel else "FAIL",
                criterio="o que o filho le no stdin e exatamente a frase enviada, sem expansao",
                obtido=f"identica: {stdin_fiel}",
                detalhes=[f"enviado: {frase_stdin!r}", f"lido pelo filho: {gravado_stdin}"],
            )
        )

        # P4 - o alvo em vigor neste PC e o executavel real, nunca o shim.
        try:
            alvo_em_vigor = localizar_cli()
            sufixo = Path(alvo_em_vigor).suffix.lower()
            seguro = sufixo not in EXTENSOES_QUE_PASSAM_PELO_SHELL
        except FileNotFoundError as erro:
            alvo_em_vigor, sufixo, seguro = f"(nao encontrado: {erro})", "", False
        provas.append(
            ProvaSeguranca(
                codigo="P4",
                titulo="localizar_cli() devolve o executavel real e nunca o shim do npm",
                estado="PASS" if seguro else "FAIL",
                criterio=f"extensao fora de {list(EXTENSOES_QUE_PASSAM_PELO_SHELL)}",
                obtido=f"{alvo_em_vigor}  (extensao {sufixo or 'nenhuma'})",
                detalhes=[
                    f"para contraste, o que o shutil.which devolvia e a tentativa 1 usava: "
                    f"{shutil.which('claude')}",
                ],
            )
        )

        # P5 - degrau 4 a serio, com o payload de injecao do Security Reviewer.
        marca_real = pasta / "MARCA_DEGRAU_4.txt"
        payload_real = f'ola" & echo {MARCA_INJECAO} > {marca_real} & "'
        antes_repo = sorted(p.name for p in RAIZ.iterdir())
        antes_pasta = sorted(p.name for p in pasta.iterdir())
        r_injecao = degrau_4_claude_p_resume(frase=payload_real, limite_s=limite_s, cwd=RAIZ)
        depois_repo = sorted(p.name for p in RAIZ.iterdir())
        depois_pasta = sorted(p.name for p in pasta.iterdir())
        novos = sorted(
            set(depois_repo) - set(antes_repo) | (set(depois_pasta) - set(antes_pasta))
        )
        sem_execucao = not marca_real.exists() and not novos
        provas.append(
            ProvaSeguranca(
                codigo="P5",
                titulo="Degrau 4 com o payload de injecao: nada e executado",
                estado="PASS" if sem_execucao else "FAIL",
                criterio="o ficheiro do payload nao existe e nao aparece ficheiro novo nenhum",
                obtido=(
                    f"ficheiro do payload criado: {marca_real.exists()} | "
                    f"ficheiros novos (repo + pasta da prova): {novos or 'nenhum'}"
                ),
                detalhes=[
                    f"frase usada: {payload_real!r}",
                    f"estado do degrau: {r_injecao.estado}",
                    "resposta do modelo (a frase foi tratada como texto): "
                    + (r_injecao.resposta.replace("\n", " ")[:400] or r_injecao.erro[:400]),
                    "na tentativa 1 este mesmo payload criava o ficheiro E o degrau "
                    "reportava PASS na mesma (SECURITY-REJECT, T2-a1-security.md)",
                ],
            )
        )

        # P6 - degrau 4 a serio, com uma variavel de ambiente falsa definida.
        os.environ[VARIAVEL_FALSA] = VALOR_FALSO
        try:
            sessao = _session_id_das_notas(r_injecao)
            r_variavel = degrau_4_claude_p_resume(
                frase=f"repete isto: %{VARIAVEL_FALSA}%",
                limite_s=limite_s,
                cwd=RAIZ,
                session_id=sessao,
            )
        finally:
            os.environ.pop(VARIAVEL_FALSA, None)
        resposta = r_variavel.resposta or r_variavel.erro
        valor_vazou = VALOR_FALSO in resposta
        literal_chegou = f"%{VARIAVEL_FALSA}%" in resposta
        provas.append(
            ProvaSeguranca(
                codigo="P6",
                titulo="Degrau 4 com %VARIAVEL% no texto: o valor nao sai do PC",
                estado="PASS" if not valor_vazou else "FAIL",
                criterio=(
                    f"o valor da variavel ({VALOR_FALSO}) nunca aparece na resposta; "
                    "o nome chega ao modelo literal"
                ),
                obtido=(
                    f"valor da variavel na resposta: {valor_vazou} | "
                    f"nome literal na resposta: {literal_chegou}"
                ),
                detalhes=[
                    f"variavel FALSA definida no ambiente deste teste: {VARIAVEL_FALSA}="
                    f"{VALOR_FALSO} (nao e um segredo real; escolhida de proposito fora do "
                    "espaco ANTHROPIC_*, porque definir uma ANTHROPIC_API_KEY mudaria a "
                    "autenticacao da sessao filha. O mecanismo com esse nome exato esta "
                    "provado na P2)",
                    f"session-id reutilizado da P5: {sessao}",
                    f"estado do degrau: {r_variavel.estado}",
                    "resposta do modelo: " + resposta.replace("\n", " ")[:400],
                ],
            )
        )
    finally:
        shutil.rmtree(pasta, ignore_errors=True)
    return provas


def anunciar_prova(prova: ProvaSeguranca) -> None:
    print(f"  {prova.codigo:<3} {prova.estado:<5} {prova.titulo}", flush=True)
    print(f"      {prova.obtido}", flush=True)


def secao_das_provas(provas: list[ProvaSeguranca]) -> str:
    falhas = [p for p in provas if p.estado != "PASS"]
    linhas = [
        "## Prova de seguranca (D48) — testes negativos dos dois bloqueadores da tentativa 1",
        "",
        "Correm em cada execucao deste script. Passam quando **nada acontece**: sao a unica "
        "forma de um PASS da escada nao voltar a esconder uma injecao.",
        "",
        (
            f"**Resultado: {len(provas) - len(falhas)}/{len(provas)} PASS.**"
            if not falhas
            else f"**Resultado: {len(falhas)} FALHA(S) DE SEGURANCA — ver abaixo.**"
        ),
        "",
        "| Prova | O que verifica | Estado |",
        "|---|---|---|",
    ]
    for prova in provas:
        linhas.append(f"| {prova.codigo} | {prova.titulo} | {prova.estado} |")
    linhas.append("")
    for prova in provas:
        linhas += [
            f"### {prova.codigo} — {prova.titulo}",
            "",
            f"- **Estado: {prova.estado}**",
            f"- Criterio: {prova.criterio}",
            f"- Obtido: {prova.obtido}",
        ]
        for detalhe in prova.detalhes:
            linhas.append(f"- {detalhe}")
        linhas.append("")
    return "\n".join(linhas)


def secao_do_degrau(resultado: ResultadoDegrau) -> str:
    tempo = (
        f"{resultado.segundos_ate_primeira_resposta:.2f} s"
        if resultado.segundos_ate_primeira_resposta is not None
        else "(nao houve resposta)"
    )
    total = (
        f"{resultado.segundos_total:.2f} s"
        if resultado.segundos_total is not None
        else "(n/a)"
    )
    linhas = [
        f"### Degrau {resultado.degrau} — {resultado.nome}",
        "",
        f"- **Estado: {resultado.estado}**",
        f"- Tempo ate a primeira resposta: {tempo}",
        f"- Tempo total do degrau: {total}",
        "",
        "Comando exato:",
        "",
        "```",
        resultado.comando,
        "```",
        "",
    ]
    if resultado.frase:
        linhas += [f"Frase entregue: `{resultado.frase}`", ""]
    if resultado.resposta:
        linhas += ["Resposta capturada de volta neste processo:", "", "```", resultado.resposta, "```", ""]
    if resultado.erro:
        titulo = "O que aconteceu" if resultado.estado in ("VETADO", "NAO TENTADO") else "Erro completo"
        linhas += [f"{titulo}:", "", "```", resultado.erro.rstrip(), "```", ""]
    if resultado.notas:
        linhas.append("Notas:")
        linhas.append("")
        for nota in resultado.notas:
            if "\n" in nota:
                linhas += ["- ", "```", nota.rstrip(), "```"]
            else:
                linhas.append(f"- {nota}")
        linhas.append("")
    return "\n".join(linhas)


def escrever_prova(
    caminho: Path,
    resultados: list[ResultadoDegrau],
    frase: str,
    limite_s: float,
    inicio: float,
    provas: list[ProvaSeguranca],
) -> Path:
    passaram = [r for r in resultados if r.passou]
    escolhido = passaram[0] if passaram else None
    caminho.parent.mkdir(parents=True, exist_ok=True)
    cabecalho = [
        "# Prova — canal para o Claude Code (T2, escada da D40/D8)",
        "",
        f"Gerado por `scripts/testar_canal.py` em {time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"(duracao total: {time.monotonic() - inicio:.1f} s).",
        "",
        f"- Frase de teste (exata): `{frase}`",
        f"- Limite por degrau: {limite_s:.0f} s",
        f"- Pasta de trabalho da sessao filha (D12): `{RAIZ}`",
        f"- CLI do Claude Code: {versao_do_cli()}",
        f"- Python: {platform.python_version()} · {platform.platform()}",
        f"- Sessao filha sem ferramentas: `--tools \"\" --restricted --permission-prompts none "
        "--strict-mcp-config --disable-slash-commands`",
        "",
        "## Resultado",
        "",
    ]
    if provas:
        falhadas = [p.codigo for p in provas if p.estado != "PASS"]
        cabecalho += [
            (
                f"**Seguranca (D48): {len(provas)}/{len(provas)} provas negativas PASS** — "
                "nenhuma frase executou comandos e nenhum valor de variavel de ambiente saiu "
                "do PC. Detalhe na seccao «Prova de seguranca»."
                if not falhadas
                else f"**SEGURANCA FALHOU nas provas {', '.join(falhadas)}.** O canal nao "
                "pode ser usado assim; ver a seccao «Prova de seguranca»."
            ),
            "",
        ]
    if escolhido:
        cabecalho += [
            f"**Degrau escolhido: {escolhido.degrau} — {escolhido.nome}.** Entregou a frase a uma "
            "sessao real do Claude Code e trouxe a resposta de volta em "
            f"{escolhido.segundos_ate_primeira_resposta:.2f} s. "
            f"Gravado no codigo em `jarvis/canal_claude.py` (`DEGRAU_ESCOLHIDO = "
            f"\"{DEGRAU_ESCOLHIDO}\"`).",
            "",
        ]
    else:
        cabecalho += [
            "**Nenhum degrau entregou a frase.** O diagnostico de cada um esta abaixo; o canal "
            "fica sem transporte provado (D35.3: o run nao falha por isto).",
            "",
        ]
    cabecalho += ["| Degrau | Transporte | Estado | 1a resposta |", "|---|---|---|---|"]
    for resultado in resultados:
        tempo = (
            f"{resultado.segundos_ate_primeira_resposta:.2f} s"
            if resultado.segundos_ate_primeira_resposta is not None
            else "—"
        )
        cabecalho.append(
            f"| {resultado.degrau} | {resultado.nome} | {resultado.estado} | {tempo} |"
        )
    cabecalho += ["", "## Degrau a degrau", ""]
    corpo = [secao_do_degrau(resultado) for resultado in resultados]
    if provas:
        corpo.append(secao_das_provas(provas))
    caminho.write_text("\n".join(cabecalho) + "\n" + "\n".join(corpo), encoding="utf-8")
    return caminho


def main(argv: list[str] | None = None) -> int:
    forcar_consola_utf8()
    analisador = argparse.ArgumentParser(description="Escada do canal para o Claude Code")
    analisador.add_argument("--frase", default=FRASE_DE_TESTE)
    analisador.add_argument("--timeout", type=float, default=TIMEOUT_POR_DEGRAU_S)
    analisador.add_argument("--degraus", default=",".join(ORDEM_DA_ESCADA))
    analisador.add_argument("--saida", default=None)
    analisador.add_argument(
        "--sem-prova-seguranca",
        action="store_true",
        help="salta os testes negativos da D48 (so para depurar; a prova entregue leva-os)",
    )
    argumentos = analisador.parse_args(argv)

    degraus = tuple(d.strip() for d in argumentos.degraus.split(",") if d.strip())
    desconhecidos = [d for d in degraus if d not in ORDEM_DA_ESCADA]
    if desconhecidos:
        print(f"degraus desconhecidos: {desconhecidos}; validos: {list(ORDEM_DA_ESCADA)}")
        return 2

    print("=== jarvis - escada do canal para o Claude Code (D40/D8) ===")
    print(f"frase   = {argumentos.frase!r}")
    print(f"limite  = {argumentos.timeout:.0f} s por degrau")
    print(f"cwd     = {RAIZ}")
    print(f"degraus = {', '.join(degraus)} (o degrau 1 esta vetado pela D40/D31)")
    print()

    inicio = time.monotonic()
    resultados = correr_escada(argumentos.frase, argumentos.timeout, degraus)

    provas: list[ProvaSeguranca] = []
    if not argumentos.sem_prova_seguranca:
        print()
        print("prova de seguranca (D48): testes negativos dos dois bloqueadores da tentativa 1")
        provas = provas_de_seguranca(argumentos.timeout)
        for prova in provas:
            anunciar_prova(prova)

    caminho = (
        Path(argumentos.saida)
        if argumentos.saida
        else PASTA_DE_PROVA / f"canal-{time.strftime('%Y%m%d-%H%M%S')}.md"
    )
    if not caminho.is_absolute():
        caminho = RAIZ / caminho
    escrever_prova(caminho, resultados, argumentos.frase, argumentos.timeout, inicio, provas)

    passaram = [r for r in resultados if r.passou]
    falhas_seguranca = [p.codigo for p in provas if p.estado != "PASS"]
    print()
    print(f"prova escrita em: {caminho}")
    if falhas_seguranca:
        print(
            "FALHOU A SEGURANCA (D48) em: "
            + ", ".join(falhas_seguranca)
            + " — o canal nao pode ser usado assim."
        )
        return 3
    if passaram:
        print(f"OK: degrau {passaram[0].degrau} entregou a frase e trouxe a resposta.")
        if provas:
            print(f"OK: {len(provas)}/{len(provas)} provas negativas de seguranca (D48).")
        return 0
    print("FALHOU: nenhum degrau entregou a frase; ver o diagnostico na prova.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
