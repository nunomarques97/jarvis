r"""Mede o jarvis de ponta a ponta, pelo caminho vivo, a partir de ficheiros WAV.

    .venv\Scripts\python scripts/medir_ponta_a_ponta.py --verificar

Corre o processo residente de `python -m jarvis` (ouvido, interprete,
confirmacao, executor e voz verdadeiros) em modo ficheiro, com duas trocas:

  * o microfone e a tecla de falar sao WAV entregues ao ritmo real, um de cada
    vez, como alguem que fala, ouve a resposta e volta a falar;
  * o canal para o Claude Code e falso: regista o prompt confirmado e nunca
    envia nada para fora deste processo. A voz grava a resposta num WAV em
    `audio/` (pasta ignorada pelo Git) e nunca toca nas colunas.

Cada volta diz tres frases: "horas", um ditado para um projeto ficticio e o
"sim" ao recap. Por frase, o log do dia (`logs/`, ignorada) guarda as etapas
com timestamps; no fim sai a tabela com:

  * horas: fim da fala -> inicio da resposta falada (p50 e p95);
  * ditado: fim da fala -> inicio do recap falado (p50 e p95);
  * por etapa, para as horas e para o ditado: o p50 do fim de turno, do STT,
    do interprete (e quantas frases foram pela regra ou pelo LLM), do resto
    e da voz ate ao primeiro audio, desde o ultimo chunk com voz;
  * primeiro sinal de vida: fim da fala -> linha A PENSAR (o maximo);
  * arranque: do inicio do processo a PRONTO, com a VRAM livre antes e depois.

Com `--verificar` sai com codigo 1 se:

  * horas > 1,2 s p50 ou > 2,0 s p95;
  * ditado -> recap > 2,5 s p50 ou > 4,0 s p95;
  * algum primeiro sinal de vida > 1,0 s;
  * o arranque passar de 30 s;
  * alguma frase nao for percebida (so contam as frases percebidas, e tem de
    haver uma por volta), ou o canal falso nao receber o prompt no projeto certo.

Os WAV de teste sao gerados pela voz local do proprio jarvis (`python -m
jarvis.voz ... --ficheiro`, num processo a parte para o arranque medido ser o
de um processo frio) e ficam em cache em `audio/_ponta_a_ponta/`. Voz sintetica
serve so para medir tempos: o acerto na voz do utilizador mede-se com as
gravacoes dele. Para medir com gravacoes proprias:

    .venv\Scripts\python scripts/medir_ponta_a_ponta.py --wav-horas recordings/a.wav --wav-ditado recordings/b.wav --wav-sim recordings/c.wav

(o ditado tem de nomear o projeto ficticio "atlas").

`--evidencia` escreve um resumo em docs/forja/evidence/ (so numeros e as
frases fixas deste ficheiro).
"""

from __future__ import annotations

import argparse
import datetime
import math
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis import app, voz  # noqa: E402
from jarvis.audio_util import (  # noqa: E402
    PASTA_AUDIO,
    PASTA_EVIDENCIA,
    caminho_evidencia_de_saida,
    caminho_para_mostrar,
)
from jarvis.config import CAMINHO_CONFIG_PADRAO, Config, ConfigError, Projeto, carregar_config  # noqa: E402
from jarvis.confirmacao import classificar_resposta  # noqa: E402
from jarvis.consola import forcar_consola_utf8  # noqa: E402
from jarvis.interprete import Interprete  # noqa: E402

LIMITE_HORAS_P50_MS = 1200.0
LIMITE_HORAS_P95_MS = 2000.0
LIMITE_RECAP_P50_MS = 2500.0
LIMITE_RECAP_P95_MS = 4000.0
LIMITE_SINAL_DE_VIDA_MS = 1000.0
LIMITE_ARRANQUE_S = app.LIMITE_DO_ARRANQUE_S

PASTA_DOS_WAV = PASTA_AUDIO / "_ponta_a_ponta"

#: O projeto do ditado. Ficticio: a medicao nunca ve os projetos reais.
PROJETO = "atlas"
PROJETOS_FICTICIOS = (
    Projeto(PROJETO, Path("C:/jarvis-medicao/atlas")),
    Projeto("orbita", Path("C:/jarvis-medicao/orbita")),
)

#: As frases de cada volta. Escolhidas para a voz sintetica sair bem
#: transcrita (as formas mais curtas, "que horas sao" e "sim", saem trocadas
#: nesta voz). As horas vao pela regra, sem o LLM; o ditado passa sempre pelo
#: LLM, por isso as duas medidas cobrem os dois caminhos do interprete.
FRASES = {
    "pt": {
        "horas": "podes dizer-me que horas são",
        "ditado": "no projeto atlas, corrige o teste do login que está a falhar",
        "sim": "sim, envia isso",
    },
    "en": {
        "horas": "can you tell me what time it is",
        "ditado": "in the atlas project, fix the login test that keeps failing",
        "sim": "yes, send it",
    },
}
TIPOS = ("horas", "ditado", "sim")


class CanalDeMedicao:
    """Faz de canal para as sessoes: guarda o prompt confirmado e nada sai daqui."""

    def __init__(self) -> None:
        self.recebidos: list[tuple[str, str]] = []

    def enviar(self, projeto: str, texto: str, ao_responder) -> bool:
        self.recebidos.append((projeto, texto))
        return True

    def fechar(self) -> None:
        pass


@dataclass
class Resultado:
    """Os numeros de uma medicao e o que correu mal nela."""

    voltas: int
    horas_ms: list[float] = field(default_factory=list)
    recap_ms: list[float] = field(default_factory=list)
    sinal_de_vida_ms: list[float] = field(default_factory=list)
    confirmados: int = 0
    nao_percebidas: list[str] = field(default_factory=list)
    recebidos: list[tuple[str, str]] = field(default_factory=list)
    arranque_s: float | None = None
    #: A decomposicao por etapa de cada resposta medida (a linha "tempos" do log).
    tempos_horas: list[app.TemposDaResposta] = field(default_factory=list)
    tempos_recap: list[app.TemposDaResposta] = field(default_factory=list)
    vram_antes: object = None
    vram_depois: object = None
    notas: list[str] = field(default_factory=list)


def percentil(valores: list[float], p: float) -> float:
    """Percentil pelo metodo do vizinho mais proximo (sem interpolar)."""
    ordenados = sorted(valores)
    if not ordenados:
        return math.nan
    return ordenados[max(0, math.ceil(p / 100.0 * len(ordenados)) - 1)]


def classificar(medidas: list[app.MedidaDaFrase], voltas: int) -> Resultado:
    """Separa as medidas por tipo de frase, pelo que o jarvis percebeu de cada uma."""
    resultado = Resultado(voltas=voltas)
    for medida in medidas:
        if medida.sinal_de_vida_ms is not None:
            resultado.sinal_de_vida_ms.append(medida.sinal_de_vida_ms)
        if medida.resposta_ao_recap:
            if medida.desfecho == "executado":
                resultado.confirmados += 1
            else:
                resultado.nao_percebidas.append(f"resposta ao recap {medida.texto!r} -> {medida.desfecho}")
        elif medida.intencao == "horas" and medida.primeira_fala_ms is not None:
            resultado.horas_ms.append(medida.primeira_fala_ms)
            if medida.tempos is not None:
                resultado.tempos_horas.append(medida.tempos)
        elif (
            medida.intencao == "ditar_prompt"
            and medida.projeto == PROJETO
            and medida.desfecho == "pendente"
            and medida.primeira_fala_ms is not None
        ):
            resultado.recap_ms.append(medida.primeira_fala_ms)
            if medida.tempos is not None:
                resultado.tempos_recap.append(medida.tempos)
        else:
            resultado.nao_percebidas.append(
                f"{medida.texto!r} -> intencao={medida.intencao} projeto={medida.projeto} desfecho={medida.desfecho}"
            )
    return resultado


def avaliar(resultado: Resultado) -> list[str]:
    """As falhas contra as metas; lista vazia quando tudo passa."""
    falhas: list[str] = []
    voltas = resultado.voltas

    def contar(nome: str, obtidos: int) -> None:
        if obtidos < voltas:
            falhas.append(f"{nome}: so {obtidos} de {voltas} frase(s) percebida(s)")

    contar("horas", len(resultado.horas_ms))
    contar("ditado", len(resultado.recap_ms))
    contar("sim ao recap", resultado.confirmados)
    for nome, valores, limite_p50, limite_p95 in (
        ("horas -> resposta falada", resultado.horas_ms, LIMITE_HORAS_P50_MS, LIMITE_HORAS_P95_MS),
        ("ditado -> recap falado", resultado.recap_ms, LIMITE_RECAP_P50_MS, LIMITE_RECAP_P95_MS),
    ):
        if not valores:
            continue
        p50, p95 = percentil(valores, 50), percentil(valores, 95)
        if p50 > limite_p50:
            falhas.append(f"{nome}: p50 {p50:.0f} ms > {limite_p50:.0f} ms")
        if p95 > limite_p95:
            falhas.append(f"{nome}: p95 {p95:.0f} ms > {limite_p95:.0f} ms")
    if not resultado.sinal_de_vida_ms:
        falhas.append("primeiro sinal de vida: nenhuma frase medida")
    elif max(resultado.sinal_de_vida_ms) > LIMITE_SINAL_DE_VIDA_MS:
        falhas.append(
            f"primeiro sinal de vida: maximo {max(resultado.sinal_de_vida_ms):.0f} ms > {LIMITE_SINAL_DE_VIDA_MS:.0f} ms"
        )
    if resultado.arranque_s is None:
        falhas.append("arranque: nao chegou a PRONTO")
    elif resultado.arranque_s > LIMITE_ARRANQUE_S:
        falhas.append(f"arranque: {resultado.arranque_s:.1f} s > {LIMITE_ARRANQUE_S:.0f} s")
    errados = [recebido for recebido in resultado.recebidos if recebido[0] != PROJETO]
    if errados:
        falhas.append(f"canal: prompt entregue ao projeto errado: {sorted({p for p, _ in errados})}")
    if len(resultado.recebidos) != resultado.confirmados:
        falhas.append(
            f"canal: recebeu {len(resultado.recebidos)} prompt(s) para {resultado.confirmados} 'sim' confirmado(s)"
        )
    return falhas


def linha_das_etapas(nome: str, tempos: list[app.TemposDaResposta]) -> str:
    """O p50 de cada etapa, da ultima voz ao primeiro audio, e quantas frases foram pela regra."""
    if not tempos:
        return f"{nome:<35}: sem amostras"

    def p50(valores: list[float | None]) -> str:
        medidos = [valor for valor in valores if valor is not None]
        return f"{percentil(medidos, 50):.0f} ms" if medidos else "?"

    origens = [tempo.origem or "sem interprete" for tempo in tempos]
    contagem = ", ".join(f"{origem} {origens.count(origem)}" for origem in sorted(set(origens)))
    return (
        f"{nome:<35}: fim de turno {p50([t.fim_de_turno_ms for t in tempos])} | stt {p50([t.stt_ms for t in tempos])} "
        f"| interprete {p50([t.interprete_ms for t in tempos])} ({contagem}) | resto {p50([t.resto_ms for t in tempos])} "
        f"| voz {p50([t.voz_ms for t in tempos])} | total {p50([t.total_ms for t in tempos])} (p50, n={len(tempos)})"
    )


def linhas_do_resumo(resultado: Resultado) -> list[str]:
    """A tabela final, igual na consola, no log e na evidencia."""

    def estatistica(valores: list[float]) -> str:
        if not valores:
            return "sem amostras"
        return (
            f"p50 {percentil(valores, 50):6.0f} ms | p95 {percentil(valores, 95):6.0f} ms | "
            f"media {statistics.fmean(valores):6.0f} ms | n={len(valores)}"
        )

    arranque = f"{resultado.arranque_s:.1f} s" if resultado.arranque_s is not None else "nao chegou a PRONTO"
    return [
        f"horas -> inicio da resposta falada : {estatistica(resultado.horas_ms)} "
        f"(meta p50 <= {LIMITE_HORAS_P50_MS:.0f}, p95 <= {LIMITE_HORAS_P95_MS:.0f})",
        f"ditado -> inicio do recap falado   : {estatistica(resultado.recap_ms)} "
        f"(meta p50 <= {LIMITE_RECAP_P50_MS:.0f}, p95 <= {LIMITE_RECAP_P95_MS:.0f})",
        linha_das_etapas("horas, por etapa", resultado.tempos_horas),
        linha_das_etapas("ditado, por etapa", resultado.tempos_recap),
        f"primeiro sinal de vida (maximo)    : "
        + (f"{max(resultado.sinal_de_vida_ms):6.0f} ms" if resultado.sinal_de_vida_ms else "sem amostras")
        + f" (meta <= {LIMITE_SINAL_DE_VIDA_MS:.0f})",
        f"arranque ate PRONTO                : {arranque} (meta <= {LIMITE_ARRANQUE_S:.0f} s)",
        f"VRAM livre antes / depois          : {resultado.vram_antes} / {resultado.vram_depois}",
        f"'sim' confirmados / prompts no canal falso: {resultado.confirmados} / {len(resultado.recebidos)}",
    ]


# --- WAV de teste ----------------------------------------------------------------------


#: Sinteses tentadas por frase ate uma sair percebida (a voz local varia de
#: sintese para sintese).
TENTATIVAS_POR_WAV = 6


def caminho_do_wav(lingua: str, tipo: str) -> Path:
    return PASTA_DOS_WAV / f"{lingua}-{tipo}.wav"


def frase_serve(tipo: str, texto: str, interprete: Interprete) -> bool:
    """A transcricao do WAV sintetico leva o jarvis ao caminho que se quer medir?"""
    if tipo == "sim":
        return classificar_resposta(texto)[0] == "confirmar"
    interpretacao = interprete.interpretar(texto)
    if tipo == "horas":
        return interpretacao.intencao == "horas"
    return interpretacao.intencao == "ditar_prompt" and interpretacao.projeto == PROJETO


def preparar_wavs(config: Config) -> int:
    """No processo filho: sintetiza cada frase, confirma que e percebida e guarda-a."""
    from jarvis.ouvido import pcm_do_wav
    from jarvis.stt import criar_motor

    lingua = "en" if config.ouvido.lingua == "en" else "pt"
    voz.definir_lingua_da_voz(lingua)
    motor = criar_motor(config.ouvido.motor, config.ouvido.device)
    motor.carregar()
    interprete = Interprete(config)
    em_falta = []
    for tipo in TIPOS:
        final = caminho_do_wav(lingua, tipo)
        rascunho = final.with_name(f"_{final.name}")
        final.parent.mkdir(parents=True, exist_ok=True)
        for tentativa in range(1, TENTATIVAS_POR_WAV + 1):
            fala = voz.falar(FRASES[lingua][tipo], ficheiro=rascunho)
            if not fala.falou:
                print(f"wav {tipo}: a voz nao falou ({fala.motivo_falha})")
                break
            texto = motor.transcrever(pcm_do_wav(rascunho), lingua=lingua).texto
            serve = frase_serve(tipo, texto, interprete)
            print(f"wav {tipo}, sintese {tentativa}: {texto!r} -> {'serve' if serve else 'nao serve'}")
            if serve:
                rascunho.replace(final)
                break
        else:
            em_falta.append(tipo)
        rascunho.unlink(missing_ok=True)
    if em_falta:
        print(f"nenhuma sintese percebida para: {', '.join(em_falta)}")
        return 1
    return 0


def gerar_wavs(
    lingua: str, *, regenerar: bool = False, python: str = sys.executable, correr=subprocess.run
) -> dict[str, Path]:
    """As tres frases fixas, ditas pela voz local e confirmadas num processo a parte.

    Ficam em cache em `audio/_ponta_a_ponta/`; o processo medido comeca frio.
    """
    wavs = {tipo: caminho_do_wav(lingua, tipo) for tipo in TIPOS}
    if regenerar:
        for caminho in wavs.values():
            caminho.unlink(missing_ok=True)
    if all(caminho.is_file() for caminho in wavs.values()):
        return wavs
    comando = [python, str(Path(__file__).resolve()), "--preparar-wavs", "--lingua", lingua]
    saida = correr(comando, cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    relato = (saida.stdout or "") + (saida.stderr or "")
    for linha in relato.splitlines():
        if linha.startswith(("wav ", "nenhuma sintese")):
            print(f"preparar | {linha}")
    if saida.returncode != 0 or not all(caminho.is_file() for caminho in wavs.values()):
        raise RuntimeError(
            f"os WAV de teste nao ficaram prontos (codigo {saida.returncode}): {relato.strip()[-800:]}"
        )
    return wavs


# --- Medicao --------------------------------------------------------------------------


def config_de_medicao(lingua: str | None) -> tuple[Config, str]:
    """As tabelas [ouvido] e [interprete] do config.toml local, com projetos ficticios."""
    nota = "config.toml local ([ouvido] e [interprete]); projetos ficticios"
    try:
        local = carregar_config(CAMINHO_CONFIG_PADRAO)
        ouvido, interprete = local.ouvido, local.interprete
    except ConfigError:
        base = Config(microfone="", projetos=())
        ouvido, interprete = base.ouvido, base.interprete
        nota = "sem config.toml valido: valores por omissao; projetos ficticios"
    if lingua is not None and lingua != ouvido.lingua:
        from dataclasses import replace

        ouvido = replace(ouvido, lingua=lingua)
    return Config(microfone="", projetos=PROJETOS_FICTICIOS, ouvido=ouvido, interprete=interprete), nota


def medir(config: Config, wavs: dict[str, Path], voltas: int, log, *, inicio: float) -> Resultado:
    """Corre as voltas pelo processo residente e devolve os numeros."""
    lingua = "en" if config.ouvido.lingua == "en" else "pt"
    voz.definir_lingua_da_voz(lingua)
    saida = PASTA_DOS_WAV / "_resposta.wav"
    canal = CanalDeMedicao()

    def falar_para_ficheiro(texto: str) -> voz.ResultadoFala:
        # O jarvis a serio toca nas colunas; aqui a resposta vai so para ficheiro,
        # pelo mesmo motor residente e pelo mesmo `falar()`.
        return voz.falar(texto, ficheiro=saida)

    jarvis = app.Jarvis(
        config,
        log,
        interprete=Interprete(config),
        canal=canal,
        falar=falar_para_ficheiro,
        painel=app.Painel(log.linha),
    )
    sequencia = [wavs[tipo] for _ in range(voltas) for tipo in TIPOS]
    try:
        ouvido = app.construir_ouvido(jarvis, wavs=sequencia, ritmo_real=True)
        app.correr(jarvis, ouvido, com_voz=True, inicio=inicio)
    finally:
        saida.unlink(missing_ok=True)
    resultado = classificar(jarvis.medidas, voltas)
    resultado.recebidos = list(canal.recebidos)
    if jarvis.arranque is not None:
        resultado.arranque_s = jarvis.arranque.total_s
        resultado.vram_antes = jarvis.arranque.vram_antes
        resultado.vram_depois = jarvis.arranque.vram_depois
        if jarvis.arranque.erros:
            resultado.notas.append(f"pecas com erro no arranque: {jarvis.arranque.erros}")
    return resultado


def escrever_evidencia(caminho: Path, resultado: Resultado, falhas: list[str], contexto: list[str]) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    linhas = [
        f"# Ponta a ponta — {datetime.datetime.now():%Y-%m-%d %H:%M}",
        "",
        "Medido por `scripts/medir_ponta_a_ponta.py` pelo caminho vivo em modo ficheiro, com canal falso",
        "e a voz gravada para ficheiro. Os WAV de teste são sintéticos: medem tempos, não o acerto na voz",
        "do utilizador.",
        "",
        *[f"- {linha}" for linha in contexto],
        "",
        "```",
        *linhas_do_resumo(resultado),
        "```",
        "",
        "Resultado: " + ("PASSA" if not falhas else "FALHA"),
        *[f"- {falha}" for falha in falhas],
        *[f"- nota: {nota}" for nota in resultado.notas],
        "",
    ]
    caminho.write_text("\n".join(linhas), encoding="utf-8")


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/medir_ponta_a_ponta.py",
        description="Mede o jarvis de ponta a ponta a partir de WAV, com canal falso e sem tocar som.",
    )
    parser.add_argument("--verificar", action="store_true", help="sai com erro fora das metas")
    parser.add_argument("--voltas", type=int, default=6, help="voltas de horas + ditado + sim (por omissao 6)")
    parser.add_argument("--lingua", choices=sorted(FRASES), default=None, help="por omissao a do config.toml")
    parser.add_argument("--regenerar", action="store_true", help="volta a gerar os WAV sinteticos")
    # Uso interno: o processo filho que gera e confirma os WAV sinteticos.
    parser.add_argument("--preparar-wavs", action="store_true", help=argparse.SUPPRESS)
    for tipo in TIPOS:
        parser.add_argument(f"--wav-{tipo}", metavar="WAV", help=f"WAV proprio para a frase '{tipo}'")
    parser.add_argument(
        "--evidencia",
        nargs="?",
        const="",
        default=None,
        metavar="FICHEIRO",
        help="escreve o resumo em docs/forja/evidence/ (por omissao ponta-a-ponta-<data>.md)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    inicio = time.perf_counter()
    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)
    if args.voltas < 1:
        print("ERRO: --voltas tem de ser pelo menos 1")
        return 2
    evidencia = None
    if args.evidencia is not None:
        nome = args.evidencia or f"ponta-a-ponta-{datetime.datetime.now():%Y%m%d-%H%M%S}.md"
        try:
            evidencia = caminho_evidencia_de_saida(nome if args.evidencia else PASTA_EVIDENCIA / nome)
        except ValueError as erro:
            print(f"ERRO: {erro}")
            return 1

    config, nota_da_config = config_de_medicao(args.lingua)
    if args.preparar_wavs:
        return preparar_wavs(config)
    lingua = "en" if config.ouvido.lingua == "en" else "pt"
    proprios = {tipo: getattr(args, f"wav_{tipo}") for tipo in TIPOS}
    try:
        wavs = gerar_wavs(lingua, regenerar=args.regenerar) if not all(proprios.values()) else {}
    except (RuntimeError, subprocess.TimeoutExpired) as erro:
        print(f"ERRO: {erro}")
        return 2
    for tipo, caminho in proprios.items():
        if caminho:
            wavs[tipo] = Path(caminho)
    origem = "WAV proprios" if all(proprios.values()) else "WAV sinteticos da voz local (so tempos)"

    log = app.LogDaSessao()
    contexto = [
        f"lingua={lingua} | motor={config.ouvido.motor} ({config.ouvido.device}) | "
        f"LLM={config.interprete.modelo} (recurso {config.interprete.modelo_alternativo})",
        f"{args.voltas} volta(s) de horas + ditado + sim | {origem} | {nota_da_config}",
        "canal falso: nada e enviado ao Claude Code; voz para ficheiro: nada toca",
    ]
    for linha in contexto:
        log.linha(f"medicao | {linha}")
    try:
        resultado = medir(config, wavs, args.voltas, log, inicio=inicio)
        falhas = avaliar(resultado)
        log.bruto("")
        for linha in linhas_do_resumo(resultado):
            log.linha(f"medicao | {linha}")
        for nota in resultado.notas + resultado.nao_percebidas:
            log.linha(f"medicao | nota: {nota}")
        if falhas:
            for falha in falhas:
                log.linha(f"medicao | FALHA: {falha}")
        else:
            log.linha("medicao | OK: todas as metas de ponta a ponta cumpridas")
        log.linha(f"medicao | log completo em {caminho_para_mostrar(log.caminho)}")
    finally:
        log.fechar()
    if evidencia is not None:
        escrever_evidencia(evidencia, resultado, falhas, contexto)
        print(f"evidencia: {caminho_para_mostrar(evidencia)}")
    return 1 if (args.verificar and falhas) else 0


if __name__ == "__main__":
    raise SystemExit(main())
