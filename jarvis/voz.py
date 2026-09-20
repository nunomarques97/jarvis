r"""Voz de resposta do jarvis: falar(texto) via RealtimeTTS + PiperEngine.

Motor e voz decididos pelo Technology Scout (D47, TECHNOLOGY.md S4, corrige a
D37): RealtimeTTS 0.8.5 com o PiperEngine real a falar com o `piper.exe` do
proprio venv, voz `pt_PT-tugao-medium`. API exata, verbatim do TECHNOLOGY.md:

    from RealtimeTTS import TextToAudioStream, PiperEngine, PiperVoice
    voice = PiperVoice(model_file=..., config_file=...)
    engine = PiperEngine(voice=voice, piper_path=<caminho absoluto do venv>)
    stream = TextToAudioStream(engine)

`piper_path` e sempre o caminho ABSOLUTO do `piper.exe` deste venv (nunca o
PATH nem `PIPER_PATH`), pela mesma razao que scripts/gerar_wav.py (T3): o
jarvis pode correr sem o venv ativado no PATH do processo.

CODIFICACAO E SEGREDOS (repetidos aqui de scripts/gerar_wav.py, T3 — nao
importados de la porque scripts/ nao e um pacote importavel, ver o aviso no
docstring de jarvis/audio_util.py sobre a mesma fronteira):
  - PYTHONUTF8=1 / PYTHONIOENCODING=utf-8 no ambiente ANTES de sintetizar: o
    `piper.exe` le o stdin com a ANSI do processo se isto nao estiver ligado,
    e cada acento chegava partido (bug corrigido na T3 depois de medido).
  - o `piper.exe` corre SEM o ambiente deste processo enquanto sintetiza
    (`ambiente_sem_segredos`), para o token de mensagens da sessao-mae do
    Claude Code e as chaves de terceiros do Sponsor nunca chegarem a um
    processo GPL de terceiros que le um modelo vindo da rede (D50.9).

FALLBACK EXPLICITO (D35.4): `falar()` nunca levanta. Qualquer falha na
sintese, na reproducao ou na escrita do ficheiro e apanhada, o texto sai
impresso na consola como resposta de recurso, e o `ResultadoFala` devolvido
tem `falou=False` com `motivo_falha` preenchido — e assim que o Sponsor
continua a "ouvir" a resposta mesmo quando a voz falha (sem voz PT local que
sirva, sem dispositivo de audio, piper.exe em falta, modelo em falta, ...).

O texto a dizer e SEMPRE o que quem chamou decidiu dizer (D48.2): este modulo
nunca le transcricao nem configuracao, so recebe uma string e fala-a.

Uso como biblioteca:

    from jarvis.voz import falar
    resultado = falar("sao quinze e trinta")
    resultado = falar("sao quinze e trinta", ficheiro="audio/t-voz.wav")

Uso na linha de comandos:

    .venv\Scripts\python -m jarvis.voz "sao quinze e trinta"
    .venv\Scripts\python -m jarvis.voz "sao quinze e trinta" --ficheiro audio/t-voz.wav

Autoteste das partes que nao precisam de dispositivo de audio real (sintese
para ficheiro, confinamento do caminho, e o fallback provocado de proposito):

    .venv\Scripts\python -m jarvis.voz --autoteste
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.audio_util import (  # noqa: E402
    PASTA_MODELOS_PIPER,
    caminho_para_mostrar,
    caminho_wav_de_saida,
    garantir_pasta,
    ler_wav_pcm16,
)
from jarvis.canal_claude import verificar_executavel_seguro  # noqa: E402

#: A voz decidida pelo Scout (D47/S4). Nao mudar sem uma nova decisao dele.
NOME_DA_VOZ = "pt_PT-tugao-medium"
MODELO_ONNX = PASTA_MODELOS_PIPER / f"{NOME_DA_VOZ}.onnx"
CONFIG_ONNX = PASTA_MODELOS_PIPER / f"{NOME_DA_VOZ}.onnx.json"

#: Ambiente que obriga o interprete Python do `piper.exe` a ler o stdin em
#: UTF-8 — o mesmo bug e a mesma correcao da T3 (scripts/gerar_wav.py).
AMBIENTE_UTF8 = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

#: Fragmentos que marcam uma variavel de ambiente como segredo (D50.9): o
#: `piper.exe` e codigo GPL de terceiros e nao tem nada que fazer com o token
#: vivo da sessao-mae do Claude Code nem com chaves de terceiros do Sponsor.
PADROES_DE_VARIAVEL_SENSIVEL = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "APIKEY",
    "_KEY",
)


def variavel_sensivel(nome: str) -> bool:
    """True para variaveis de ambiente que um filho de terceiros nao deve ver."""
    maiusculas = nome.upper()
    if maiusculas.startswith("CLAUDE"):
        return True
    return any(padrao in maiusculas for padrao in PADROES_DE_VARIAVEL_SENSIVEL)


@contextlib.contextmanager
def _ambiente_sem_segredos():
    """Tira os segredos de `os.environ` enquanto o piper.exe corre (D50.9)."""
    escondidas = {nome: valor for nome, valor in os.environ.items() if variavel_sensivel(nome)}
    for nome in escondidas:
        del os.environ[nome]
    try:
        yield sorted(escondidas)
    finally:
        os.environ.update(escondidas)


def caminho_do_piper_exe() -> Path:
    """O piper.exe DESTE venv (mesma pasta Scripts/ do interprete a correr).

    Caminho absoluto de proposito (TECHNOLOGY.md S4): nunca o PATH nem
    PIPER_PATH. `verificar_executavel_seguro` e defesa em profundidade (D48.1):
    este candidato nunca vem do PATH nem de configuracao, so aqui por
    seguranca extra.
    """
    candidato = Path(sys.executable).resolve().parent / "piper.exe"
    verificar_executavel_seguro(candidato)
    if not candidato.is_file():
        raise FileNotFoundError(
            f"piper.exe nao encontrado em '{candidato}'. Instalar com: "
            '.venv\\Scripts\\pip install "RealtimeTTS[piper]==0.8.5" piper-tts==1.8.0 (D47)'
        )
    return candidato


def _preparar_encoding_do_piper() -> None:
    """Poe o ambiente em UTF-8 para o piper.exe (bug corrigido na T3).

    Atribuicao direta, nao setdefault: um PYTHONUTF8=0 herdado do ambiente do
    Sponsor traria o bug de volta em silencio (mesma nota de gerar_wav.py).
    """
    os.environ.update(AMBIENTE_UTF8)


def _construir_stream():
    """Constroi o TextToAudioStream ligado ao PiperEngine real (S4)."""
    from RealtimeTTS import PiperEngine, PiperVoice, TextToAudioStream

    if not MODELO_ONNX.is_file() or not CONFIG_ONNX.is_file():
        raise FileNotFoundError(
            f"voz '{NOME_DA_VOZ}' nao encontrada em {PASTA_MODELOS_PIPER}. Descarregar com: "
            f".venv\\Scripts\\python -m piper.download_voices {NOME_DA_VOZ} "
            f"--download-dir {PASTA_MODELOS_PIPER} (a voz e 'tugão' com til; ver docs/MODELOS.md)"
        )
    piper_exe = caminho_do_piper_exe()
    _preparar_encoding_do_piper()
    voz = PiperVoice(model_file=str(MODELO_ONNX), config_file=str(CONFIG_ONNX))
    motor = PiperEngine(voice=voz, piper_path=str(piper_exe))
    # tokenizer="rule-based": o default "nltk+rule-based" faz o stream2sentence
    # descarregar punkt_tab da rede na primeira corrida (achado da T3, fora do
    # registo de modelos da D14e); uma frase de cada vez nao precisa dele.
    return TextToAudioStream(motor, language="pt", tokenizer="rule-based")


def _duracao_do_wav(caminho: Path) -> float:
    dados, taxa, canais = ler_wav_pcm16(caminho)
    if taxa <= 0 or canais <= 0:
        return 0.0
    return (len(dados) / 2 / canais) / taxa


@dataclass(frozen=True)
class ResultadoFala:
    """O que `falar()` conseguiu fazer. Nunca e a excecao — e sempre o resultado."""

    #: True so quando a voz tocou ou gravou com sucesso.
    falou: bool
    #: Preenchido so no modo --ficheiro, com sucesso.
    caminho: Path | None = None
    duracao_s: float | None = None
    #: Nao vazio quando falou=False (D35.4): a razao do fallback para texto.
    motivo_falha: str = ""


def falar(texto: str, *, ficheiro: str | Path | None = None) -> ResultadoFala:
    """Fala `texto` em voz alta (Piper), ou grava-o num WAV se `ficheiro` for dado.

    NUNCA levanta (D35.4): qualquer falha na sintese, na reproducao ou na
    escrita do ficheiro e apanhada aqui dentro, o texto sai impresso na
    consola como resposta de recurso, e o resultado devolvido diz porque.
    """
    texto_limpo = (texto or "").strip()
    if not texto_limpo:
        motivo = "texto vazio: nada a dizer"
        print(f"jarvis (voz recusada: {motivo})")
        return ResultadoFala(falou=False, motivo_falha=motivo)

    saida: Path | None = None
    if ficheiro is not None:
        try:
            saida = caminho_wav_de_saida(ficheiro)
        except ValueError as erro:
            print(f"jarvis (texto, voz recusada): {texto_limpo}")
            return ResultadoFala(falou=False, motivo_falha=str(erro))

    try:
        stream = _construir_stream()
        stream.feed(texto_limpo)
        with _ambiente_sem_segredos():
            if saida is not None:
                garantir_pasta(saida.parent)
                # muted=True: nunca abre um dispositivo de saida de audio,
                # este ramo so escreve ficheiro (mesma convencao de T3).
                stream.play(muted=True, output_wavfile=str(saida))
            else:
                stream.play(muted=False)
    except Exception as erro:  # noqa: BLE001 - D35.4: qualquer falha cai para texto
        print(f"jarvis (texto, voz falhou: {erro}): {texto_limpo}")
        return ResultadoFala(falou=False, motivo_falha=str(erro))

    if saida is not None:
        duracao_s = _duracao_do_wav(saida)
        return ResultadoFala(falou=True, caminho=saida, duracao_s=duracao_s)
    return ResultadoFala(falou=True)


# --- Autoteste (sem depender de um dispositivo de audio real) --------------


def _autoteste() -> int:
    """Prova a sintese para ficheiro, o confinamento do caminho e o fallback.

    Nao toca em `audio/` (usa uma pasta temporaria) e nao exige um
    dispositivo de saida de som: so o ramo --ficheiro e testado a falar a
    serio; o fallback e provocado com um motor falso, sem tocar no piper.exe.
    """
    import tempfile

    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    # 1. texto vazio nunca chega a sintetizar, e falha de forma explicita.
    resultado_vazio = falar("   ")
    verificar("texto vazio: falou=False", resultado_vazio.falou, False)
    verificar("texto vazio: motivo preenchido", bool(resultado_vazio.motivo_falha), True)

    # 2. sintese real para ficheiro, dentro de audio/ (unico sitio permitido
    # por caminho_wav_de_saida) mas com um nome que se apaga a seguir.
    caminho_teste = Path("audio") / "_autoteste_voz_t5.wav"
    try:
        resultado = falar("ola, isto e um autoteste da voz.", ficheiro=str(caminho_teste))
        verificar("ficheiro: falou=True", resultado.falou, True)
        verificar("ficheiro: caminho existe", resultado.caminho is not None and resultado.caminho.is_file(), True)
        verificar("ficheiro: duracao audivel (> 0.3 s)", (resultado.duracao_s or 0) > 0.3, True)
    finally:
        (RAIZ / caminho_teste).unlink(missing_ok=True)

    # 3. caminho fora do repositorio e recusado ANTES de tocar no Piper
    # (mesma regra de audio_util.caminho_wav_de_saida, D48.2).
    with tempfile.TemporaryDirectory() as pasta:
        fora_da_raiz = str(Path(pasta) / "fora.wav")
        resultado_fora = falar("nunca deveria escrever aqui", ficheiro=fora_da_raiz)
        verificar("caminho fora do repo: falou=False", resultado_fora.falou, False)
        verificar("caminho fora do repo: nada escrito", Path(fora_da_raiz).exists(), False)

    # 4. fallback explicito (D35.4): um motor que rebenta nunca propaga a
    # excecao, e o texto sai impresso na consola em vez da voz.
    # `sys.modules[__name__]` e nao `import jarvis.voz`, de proposito: corrido
    # como `python -m jarvis.voz` este ficheiro executa como `__main__`, e um
    # `import jarvis.voz` fresco criava um SEGUNDO objeto de modulo (mundo a
    # parte do que `falar()` ve), e o monkeypatch nao apanhava nada.
    modulo_atual = sys.modules[__name__]

    original = modulo_atual._construir_stream

    def _motor_quebrado():
        raise RuntimeError("motor de voz de mentira, so para o autoteste")

    modulo_atual._construir_stream = _motor_quebrado
    saida_capturada = io.StringIO()
    try:
        with contextlib.redirect_stdout(saida_capturada):
            resultado_falha = falar("frase qualquer que devia sair em texto")
    finally:
        modulo_atual._construir_stream = original
    verificar("fallback: falou=False quando o motor rebenta", resultado_falha.falou, False)
    verificar(
        "fallback: motivo_falha tem a excecao original",
        "motor de voz de mentira" in resultado_falha.motivo_falha,
        True,
    )
    verificar(
        "fallback: o texto sai impresso na consola (D35.4)",
        "frase qualquer que devia sair em texto" in saida_capturada.getvalue(),
        True,
    )

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste da voz completo (sintese para ficheiro, confinamento, fallback D35.4).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("texto", nargs="?", help="frase a dizer em voz alta, em portugues europeu")
    parser.add_argument(
        "--ficheiro",
        default=None,
        help="grava o audio neste WAV em vez de o tocar (dentro do repo, sufixo .wav)",
    )
    parser.add_argument(
        "--autoteste",
        action="store_true",
        help="corre o autoteste (sintese para ficheiro, confinamento, fallback D35.4)",
    )
    args = parser.parse_args(argv)

    if args.autoteste:
        return _autoteste()

    if not args.texto or not args.texto.strip():
        parser.error("e preciso o texto a dizer (ou --autoteste)")

    print("=== jarvis - voz (Piper, voz pt_PT-tugao-medium) ===")
    print(f"texto    = {args.texto!r}")
    t0 = time.perf_counter()
    resultado = falar(args.texto, ficheiro=args.ficheiro)
    decorrido = time.perf_counter() - t0

    if resultado.falou and resultado.caminho is not None:
        print(f"ficheiro = {caminho_para_mostrar(resultado.caminho)}")
        print(f"duracao  = {resultado.duracao_s:.2f} s")
        print(f"tempo    = {decorrido:.2f} s")
        print("OK: WAV escrito.")
        return 0
    if resultado.falou:
        print(f"tempo    = {decorrido:.2f} s")
        print("OK: frase dita em voz alta.")
        return 0

    print(f"AVISO: voz falhou ({resultado.motivo_falha}); resposta caiu para texto (D35.4).")
    # --ficheiro pede um WAV real: se ele nao existe, isto e uma falha do
    # comando, nao um degrau normal de recurso.
    return 1 if args.ficheiro is not None else 0


if __name__ == "__main__":
    sys.exit(main())
