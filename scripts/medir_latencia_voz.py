r"""Mede a voz residente: texto -> primeiro bloco de audio, e o silencio.

Nunca toca som: cada frase e sintetizada para um WAV em `audio/` (pasta
ignorada pelo Git, apagado logo a seguir) ou, com `--nulo`, para lado
nenhum. Nao ha opcao para ouvir: isto e uma medicao, nao uma demonstracao.

O QUE MEDE

  1. Carregamento: quanto demora `jarvis.voz.aquecer()` (uma vez por processo).
  2. Latencia: 20 frases fixas na lingua da voz (`--lingua`, por omissao a
     das respostas do jarvis), do pedido a `falar()` ao primeiro bloco de
     audio escrito. Confirma tambem que o motor e o MESMO objeto do principio
     ao fim (nao ha carregamento por frase) e que fala a lingua pedida.
  3. Silencio: 5 frases compridas caladas por `calar_agora()` logo depois do
     primeiro audio; mede do pedido ate `falar()` devolver.
  4. Opcional, `--horas WAV`: o comando local "horas" pelo processo
     residente do jarvis em modo ficheiro (o mesmo de `python -m jarvis
     --wav`), com canal falso e a resposta falada para ficheiro; mede do fim
     da fala ao primeiro audio da resposta. O log do dia em `logs/` guarda as
     linhas com timestamps. A medicao completa (horas, ditado e recap) esta
     em `scripts/medir_ponta_a_ponta.py`.

Com `--verificar` sai com erro se a latencia passar de 300 ms p50 ou 600 ms
p95, se o silencio passar de 500 ms nalguma frase, ou se o motor tiver sido
carregado mais do que uma vez, ou se o motor carregado nao falar a lingua
pedida. Com `--horas` e `--verificar`, sai tambem com
erro se o "horas" passar de 1,2 s p50.

    .venv\Scripts\python scripts/medir_latencia_voz.py --verificar
    .venv\Scripts\python scripts/medir_latencia_voz.py --verificar --horas audio/t-horas.wav
    .venv\Scripts\python scripts/medir_latencia_voz.py --verificar --evidencia

`--evidencia` escreve um resumo em docs/forja/evidence/ (so numeros e as
frases fixas desta lista, nada da voz do utilizador).
"""

from __future__ import annotations

import argparse
import datetime
import math
import statistics
import sys
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis import voz  # noqa: E402
from jarvis.audio_util import (  # noqa: E402
    PASTA_AUDIO,
    PASTA_EVIDENCIA,
    caminho_evidencia_de_saida,
    caminho_para_mostrar,
)
from jarvis.consola import forcar_consola_utf8  # noqa: E402

LIMITE_P50_MS = 300.0
LIMITE_P95_MS = 600.0
LIMITE_SILENCIO_MS = voz.LIMITE_DE_SILENCIO_MS
LIMITE_HORAS_P50_MS = 1200.0

FRASES_PT = (
    "São catorze e trinta e dois.",
    "Hoje é quinta-feira, vinte e cinco de setembro.",
    "Está bem, fico calado.",
    "Vou dormir. Diz o meu nome para me acordares.",
    "Estou acordado.",
    "Abri o editor no projeto pedido.",
    "Abri a pasta do projeto.",
    "Não conheço esse projeto. Diz outra vez o nome, por favor.",
    "Enviei o pedido para a sessão do projeto.",
    "O run está a correr, na terceira tarefa de onze.",
    "O run acabou sem erros.",
    "A sessão está à tua espera.",
    "Não percebi. Podes repetir?",
    "Vou mandar isto. Confirmas?",
    "Cancelado, não enviei nada.",
    "O relatório diz que os testes passaram todos.",
    "Não há nenhum run ativo neste projeto.",
    "São nove e cinco da manhã.",
    "Hoje é segunda-feira, um de dezembro.",
    "Pronto.",
)

FRASES_EN = (
    "It's two thirty-two in the afternoon.",
    "Today is Thursday, September twenty-fifth.",
    "Okay, I'll be quiet.",
    "Going to sleep. Say my name to wake me up.",
    "I'm awake.",
    "I opened the editor in that project.",
    "I opened the project folder.",
    "I don't know that project. Please say the name again.",
    "I sent the request to the project session.",
    "The run is going, on task three of eleven.",
    "The run finished without errors.",
    "The session is waiting for you.",
    "I didn't catch that. Could you say it again?",
    "I'll send this. Do you confirm?",
    "Cancelled, nothing was sent.",
    "The report says all tests passed.",
    "There is no active run in this project.",
    "It's nine oh five in the morning.",
    "Today is Monday, December first.",
    "Done.",
)

#: Frase comprida para o silencio: tem de ainda estar a sair quando o pedido chega.
FRASE_LONGA = {
    "pt": (
        "Esta frase e comprida de proposito, para a voz ainda estar a meio quando o "
        "pedido de silencio chegar, e assim se medir o pior caso do motor residente. "
        "Tem uma segunda frase, e depois uma terceira, para haver sempre mais audio."
    ),
    "en": (
        "This sentence is long on purpose, so the voice is still speaking when the "
        "request to stop arrives, which measures the worst case of the resident engine. "
        "It has a second sentence, and then a third one, so there is always more audio."
    ),
}

def percentil(valores: list[float], p: float) -> float:
    """Percentil pelo metodo do vizinho mais proximo (sem interpolar)."""
    ordenados = sorted(valores)
    if not ordenados:
        return math.nan
    indice = max(0, math.ceil(p / 100.0 * len(ordenados)) - 1)
    return ordenados[indice]


def _wav_temporario(nome: str) -> Path:
    return PASTA_AUDIO / "_latencia_voz" / nome


def medir_uma(texto: str, *, nulo: bool, nome: str) -> float:
    """ms do pedido ao primeiro bloco de audio; levanta se nada saiu."""
    if nulo:
        fala = voz.FalaResidente(voz.motor_residente())
        fala.feed(texto)
        inicio = time.perf_counter()
        fala.play(muted=True)
        primeiro = fala.instante_do_primeiro_audio
    else:
        caminho = _wav_temporario(nome)
        inicio = time.perf_counter()
        resultado = voz.falar(texto, ficheiro=caminho)
        caminho.unlink(missing_ok=True)
        if not resultado.falou:
            raise RuntimeError(f"a voz nao falou: {resultado.motivo_falha}")
        primeiro = resultado.primeiro_audio
    if primeiro is None:
        raise RuntimeError("nenhum bloco de audio saiu")
    return (primeiro - inicio) * 1000.0


def medir_silencio(lingua: str, nome: str) -> dict:
    """Cala a meio uma frase comprida; mede do pedido ate `falar()` devolver."""
    caminho = _wav_temporario(nome)
    resultado: dict = {}

    def falar_em_ficheiro() -> None:
        resultado["fala"] = voz.falar(FRASE_LONGA.get(lingua, FRASE_LONGA["en"]), ficheiro=caminho)

    fio = threading.Thread(target=falar_em_ficheiro, name="medir-silencio", daemon=True)
    fio.start()
    limite = time.perf_counter() + 30.0
    while time.perf_counter() < limite:
        fala = voz._stream_ativo
        if getattr(fala, "instante_do_primeiro_audio", None) is not None:
            break
        time.sleep(0.005)
    inicio = time.perf_counter()
    silencio = voz.calar_agora("medicao do silencio")
    fio.join(timeout=10)
    total_ms = (time.perf_counter() - inicio) * 1000.0
    caminho.unlink(missing_ok=True)
    fala = resultado.get("fala")
    return {
        "ms": total_ms,
        "apanhou_a_meio": silencio.matou_sintese,
        "terminou": not fio.is_alive(),
        "motivo": fala.motivo_falha if fala else "",
    }


def medir_horas(wav: Path, repeticoes: int, device: str | None) -> list[float]:
    """Corre o 'horas' pelo processo residente do jarvis, com a resposta para ficheiro.

    O mesmo caminho de `python -m jarvis --wav`, com o canal falso de
    `scripts/medir_ponta_a_ponta.py`: uma transcricao errada nunca vira um
    pedido real ao Claude Code, e so contam as frases percebidas como 'horas'.
    """
    from dataclasses import replace

    from jarvis import app
    from jarvis.interprete import Interprete
    from scripts.medir_ponta_a_ponta import CanalDeMedicao, config_de_medicao

    config, _nota = config_de_medicao(None)
    if device is not None:
        config = replace(config, ouvido=replace(config.ouvido, device=device))
    log = app.LogDaSessao()
    saida = _wav_temporario("horas.wav")

    def falar_para_ficheiro(texto: str) -> voz.ResultadoFala:
        # O jarvis a serio toca nas colunas; aqui a resposta vai so para
        # ficheiro, pelo mesmo motor e pelo mesmo `falar()`.
        return voz.falar(texto, ficheiro=saida)

    jarvis = app.Jarvis(
        config,
        log,
        interprete=Interprete(config),
        canal=CanalDeMedicao(),
        falar=falar_para_ficheiro,
        painel=app.Painel(log.linha),
    )
    try:
        ouvido = app.construir_ouvido(jarvis, wavs=[wav] * repeticoes, ritmo_real=True)
        app.correr(jarvis, ouvido, com_voz=True)
    finally:
        log.fechar()
        saida.unlink(missing_ok=True)
    valores: list[float] = []
    for medida in jarvis.medidas:
        if medida.intencao != "horas":
            print(f"horas #{medida.numero}: percebido {medida.intencao!r} ({medida.texto!r}); corrida nao conta")
        elif medida.primeira_fala_ms is None:
            print(f"horas #{medida.numero}: sem inicio da resposta falada (ver o log)")
        else:
            valores.append(medida.primeira_fala_ms)
    return valores


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/medir_latencia_voz.py",
        description="Mede a voz residente (nunca toca som: ficheiro ou dispositivo nulo).",
    )
    parser.add_argument("--verificar", action="store_true", help="sai com erro fora das metas")
    parser.add_argument(
        "--lingua",
        choices=voz.LINGUAS_DA_VOZ,
        default=None,
        help="lingua da voz a medir (por omissao a das respostas do jarvis)",
    )
    parser.add_argument("--nulo", action="store_true", help="sintetiza para lado nenhum em vez de WAV")
    parser.add_argument("--horas", metavar="WAV", help="mede tambem o 'horas' do jarvis a partir deste WAV")
    parser.add_argument("--repeticoes", type=int, default=5, help="corridas do 'horas' (por omissao 5)")
    parser.add_argument(
        "--device", default=None, choices=["cuda", "cpu"], help="device do STT no 'horas' (por omissao o do config)"
    )
    parser.add_argument(
        "--evidencia",
        nargs="?",
        const="",
        metavar="FICHEIRO",
        help="escreve o resumo em docs/forja/evidence/ (por omissao com data no nome)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)
    destino = None
    if args.evidencia is not None:
        try:
            destino = (
                caminho_evidencia_de_saida(args.evidencia)
                if args.evidencia
                else PASTA_EVIDENCIA / f"latencia-voz-{datetime.datetime.now():%Y%m%d-%H%M%S}.md"
            )
        except ValueError as erro:
            print(f"FALHOU (--evidencia fora de docs/forja/evidence/): {erro}", file=sys.stderr)
            return 1

    if args.lingua:
        voz.definir_lingua_da_voz(args.lingua)
    lingua = voz.lingua_da_voz()
    print("=== jarvis - latencia da voz residente (sem som) ===")
    print(f"lingua da voz: {lingua}")
    inicio = time.perf_counter()
    try:
        descricao = voz.aquecer()
    except Exception as erro:  # noqa: BLE001 - sem motor nao ha nada para medir
        print(f"FALHOU: motor de voz por carregar: {erro}")
        return 1
    carregamento_ms = (time.perf_counter() - inicio) * 1000.0
    motor = voz.motor_residente()
    print(f"motor: {descricao}")
    print(f"carregamento + aquecimento: {carregamento_ms:.0f} ms (uma vez por processo)")

    frases = FRASES_EN if lingua == "en" else FRASES_PT
    latencias: list[float] = []
    for numero, frase in enumerate(frases, start=1):
        ms = medir_uma(frase, nulo=args.nulo, nome=f"frase-{numero:02d}.wav")
        latencias.append(ms)
        print(f"frase {numero:2d}: {ms:6.0f} ms | {frase}")
    mesmo_motor = voz.motor_residente() is motor
    p50, p95 = statistics.median(latencias), percentil(latencias, 95)
    print(f"texto -> primeiro audio: p50 {p50:.0f} ms, p95 {p95:.0f} ms, maximo {max(latencias):.0f} ms")
    print(f"motor carregado uma so vez: {'sim' if mesmo_motor else 'NAO'}")

    silencios = [medir_silencio(lingua, f"silencio-{n}.wav") for n in range(1, 6)]
    voz.retomar_a_voz()
    for numero, caso in enumerate(silencios, start=1):
        print(
            f"silencio {numero}: {caso['ms']:.0f} ms | a meio: {'sim' if caso['apanhou_a_meio'] else 'nao'}"
            f" | falar() terminou: {'sim' if caso['terminou'] else 'NAO'}"
        )

    horas: list[float] = []
    if args.horas:
        horas = medir_horas(Path(args.horas), args.repeticoes, args.device)
        if horas:
            print(
                f"horas, fim da fala -> inicio da resposta falada: p50 {statistics.median(horas):.0f} ms, "
                f"maximo {max(horas):.0f} ms ({len(horas)} corridas)"
            )

    falhas: list[str] = []
    if p50 > LIMITE_P50_MS or p95 > LIMITE_P95_MS:
        falhas.append(f"latencia p50 {p50:.0f} ms / p95 {p95:.0f} ms acima de {LIMITE_P50_MS:.0f}/{LIMITE_P95_MS:.0f} ms")
    if not mesmo_motor:
        falhas.append("o motor foi carregado mais do que uma vez")
    if motor.lingua != lingua:
        falhas.append(f"pedida a voz '{lingua}', mas o motor carregado fala '{motor.lingua}' ({descricao})")
    for numero, caso in enumerate(silencios, start=1):
        if caso["ms"] > LIMITE_SILENCIO_MS or not caso["terminou"]:
            falhas.append(f"silencio {numero}: {caso['ms']:.0f} ms (limite {LIMITE_SILENCIO_MS:.0f} ms)")
    if args.horas:
        if len(horas) < args.repeticoes:
            falhas.append(f"horas: so {len(horas)} de {args.repeticoes} corridas mediram o inicio da resposta")
        elif statistics.median(horas) > LIMITE_HORAS_P50_MS:
            falhas.append(f"horas p50 {statistics.median(horas):.0f} ms acima de {LIMITE_HORAS_P50_MS:.0f} ms")

    if destino is not None:
        linhas = [
            "# Voz residente: latencia e silencio\n",
            f"Gerado por `scripts/medir_latencia_voz.py` em {datetime.datetime.now():%Y-%m-%d %H:%M:%S}. "
            "Sem som: sintese para "
            + ("dispositivo nulo" if args.nulo else "WAV temporario em `audio/` (apagado)")
            + ".\n\n",
            f"- lingua da voz: {lingua}; motor: {descricao}\n",
            f"- carregamento + aquecimento: {carregamento_ms:.0f} ms, uma vez por processo; "
            f"motor carregado uma so vez: {'sim' if mesmo_motor else 'NAO'}\n",
            f"- texto -> primeiro audio ({len(latencias)} frases fixas): p50 **{p50:.0f} ms**, "
            f"p95 **{p95:.0f} ms**, maximo {max(latencias):.0f} ms (metas {LIMITE_P50_MS:.0f}/{LIMITE_P95_MS:.0f} ms)\n",
            "- silencio (`calar_agora()` a meio de uma frase comprida, ate `falar()` devolver): "
            + ", ".join(f"{caso['ms']:.0f} ms" for caso in silencios)
            + f" (limite {LIMITE_SILENCIO_MS:.0f} ms)\n",
        ]
        if args.horas:
            linhas.append(
                f"- 'horas' pelo caminho atual em modo ficheiro (`{Path(args.horas).as_posix()}`), fim da fala -> "
                "inicio da resposta falada: "
                + ", ".join(f"{valor:.0f} ms" for valor in horas)
                + (f"; p50 **{statistics.median(horas):.0f} ms**" if horas else "")
                + f" (meta {LIMITE_HORAS_P50_MS:.0f} ms; antes: 5,1-5,6 s com um `piper.exe` por frase)\n"
            )
        linhas.append("\n## Frases\n\n| # | ms | frase |\n|---|---|---|\n")
        linhas.extend(f"| {n} | {ms:.0f} | {frase} |\n" for n, (ms, frase) in enumerate(zip(latencias, frases), 1))
        linhas.append("\n" + ("FALHOU: " + "; ".join(falhas) if falhas else "OK: dentro das metas.") + "\n")
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text("".join(linhas), encoding="utf-8")
        print(f"evidencia escrita em {caminho_para_mostrar(destino)}")

    if falhas:
        print("FORA DAS METAS: " + "; ".join(falhas))
        return 1 if args.verificar else 0
    print("OK: dentro das metas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
