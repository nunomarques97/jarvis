r"""Gera um WAV pt-PT com a voz Piper, para provar o jarvis sem microfone (D33).

Motor: RealtimeTTS 0.8.5, PiperEngine real (D47/TECHNOLOGY.md S4) a falar com o
`piper.exe` do proprio venv — a API exata que o Scout deixou escrita:

    from RealtimeTTS import TextToAudioStream, PiperEngine, PiperVoice
    voice = PiperVoice(model_file=..., config_file=...)
    engine = PiperEngine(voice=voice, piper_path=<caminho absoluto do venv>)
    stream = TextToAudioStream(engine)

O texto da linha de comandos e entrada externa (D48(2)): o PiperEngine do
RealtimeTTS ja o entrega ao `piper.exe` por stdin, nunca por argv nem por um
shell (`subprocess.run(cmd_list, input=texto.encode(...), shell=False)`, lido
em RealtimeTTS/engines/piper_engine.py) — este script nunca constroi uma linha
de comandos a mao. `verificar_executavel_seguro` (jarvis.canal_claude, D48(1))
e reaproveitado so como defesa em profundidade, e faz exatamente uma coisa:
recusa um alvo com extensao que o Windows mandaria ao cmd.exe (.cmd/.bat/.ps1).
Nao verifica que o alvo e o piper-tts — quem garante isso e a origem do
caminho, `Path(sys.executable).resolve().parent / "piper.exe"`, que nunca vem
do PATH nem de configuracao.

A voz `pt_PT-tugao-medium` sintetiza nativamente a 22050 Hz (ver
models/piper/pt_PT-tugao-medium.onnx.json). O criterio da T3 pede um WAV mono
de 16 kHz; a reamostragem final e feita so com audioop (stdlib, sem
dependencia nova) em jarvis.audio_util.reamostrar_pcm16.

CODIFICACAO DO TEXTO (bug corrigido na tentativa 2, ver
`preparar_encoding_do_piper`): o `piper.exe` e um console script Python que le
o texto de `sys.stdin` em modo texto (piper/__main__.py: `texts = sys.stdin`),
ou seja descodificado com a codificacao ANSI do processo — cp1252 nesta
maquina. O PiperEngine manda-lhe UTF-8. Sem correcao, cada `a`/`c` acentuado
chegava partido em dois caracteres e a voz soava os NOMES deles ("a til",
"paragrafo"), com exit 0 e sem aviso nenhum.

TESTES SILENCIOSOS POR OMISSAO (D61, TECHNOLOGY.md S13): por omissao este
script NUNCA abre um dispositivo de audio — so escreve o WAV pedido
(`muted=True`, como sempre fez). Ouvir o que foi gerado, alem de o escrever, e
`--com-som`, sempre opt-in explicito e nunca uma variavel de ambiente
(D61.2). `--autoteste` nunca usa `--com-som`: a regressao continua muda.

Uso:
    .venv\Scripts\python scripts/gerar_wav.py "que horas sao" --saida audio/t-horas.wav
    .venv\Scripts\python scripts/gerar_wav.py "que horas sao" --saida audio/t-horas.wav --com-som
    .venv\Scripts\python scripts/gerar_wav.py --autoteste
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.audio_util import (  # noqa: E402
    PASTA_AUDIO,
    PASTA_MODELOS_PIPER,
    TAXA_AMOSTRAGEM_PADRAO,
    caminho_para_mostrar,
    caminho_wav_de_saida,
    escrever_wav_pcm16,
    garantir_pasta,
    ler_wav_pcm16,
    reamostrar_pcm16,
)
from jarvis.canal_claude import verificar_executavel_seguro  # noqa: E402
from jarvis.consola import forcar_consola_utf8  # noqa: E402

NOME_DA_VOZ = "pt_PT-tugao-medium"
MODELO_ONNX = PASTA_MODELOS_PIPER / f"{NOME_DA_VOZ}.onnx"
CONFIG_ONNX = PASTA_MODELOS_PIPER / f"{NOME_DA_VOZ}.onnx.json"

#: Ambiente que obriga qualquer interprete Python filho — e o `piper.exe` e um
#: deles — a ler o stdin em UTF-8, que e o que o PiperEngine lhe escreve.
#: PYTHONUTF8=1 liga o modo UTF-8 inteiro (stdin, stdout e `open()`, o que
#: tambem ajuda o piper a ler o .onnx.json); PYTHONIOENCODING e o cinto e os
#: suspensorios para o caso de o modo UTF-8 vir desligado por politica.
#: Atribuicao direta e nao setdefault de proposito: um PYTHONUTF8=0 herdado do
#: ambiente do Sponsor traria o bug de volta em silencio.
AMBIENTE_UTF8 = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

#: Fragmentos que marcam uma variavel de ambiente como segredo. O `piper.exe`
#: e codigo de terceiros (GPL, carrega um ONNX vindo da rede) e nao tem nada
#: que fazer com o token vivo da sessao-mae do Claude Code nem com as chaves de
#: terceiros que o ambiente do Sponsor traz (D50(9): menor privilegio).
PADROES_DE_VARIAVEL_SENSIVEL = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "APIKEY",
    "_KEY",
)

#: Texto da sonda de encoding: so caracteres que existem em cp1252 e em UTF-8,
#: para que a diferenca medida seja mesmo a descodificacao e nao um caractere
#: impossivel de representar.
TEXTO_DA_SONDA = "acao ção áéíóú"

_encoding_ja_verificado = False


def variavel_sensivel(nome: str) -> bool:
    """True para variaveis de ambiente que um filho de terceiros nao deve ver."""
    maiusculas = nome.upper()
    if maiusculas.startswith("CLAUDE"):  # CLAUDECODE, CLAUDE_CODE_*, CLAUDE_PID...
        return True
    return any(padrao in maiusculas for padrao in PADROES_DE_VARIAVEL_SENSIVEL)


@contextlib.contextmanager
def ambiente_sem_segredos():
    """Tira os segredos de `os.environ` enquanto o bloco corre e repoe-os no fim.

    O `PiperEngine` arranca o `piper.exe` sem `env=`
    (RealtimeTTS/engines/piper_engine.py), ou seja com o ambiente deste
    processo tal e qual: sem isto, o token de mensagens da sessao-mae — o mesmo
    que a D48(5) tira de proposito aos filhos do canal — e as chaves de
    terceiros da D50(9) chegavam todos ao `piper.exe`.

    Gestor de contexto e nao limpeza global de proposito: a T4 vai chamar isto
    de dentro de um processo vivo e nao pode ficar com o ambiente mutilado
    depois de sintetizar uma frase.

    Devolve a lista ordenada dos nomes escondidos (nomes, nunca valores).
    """
    escondidas = {nome: valor for nome, valor in os.environ.items() if variavel_sensivel(nome)}
    for nome in escondidas:
        del os.environ[nome]
    try:
        yield sorted(escondidas)
    finally:
        os.environ.update(escondidas)


def caminho_do_piper_exe() -> Path:
    """O piper.exe DESTE venv (mesma pasta Scripts/ do interprete a correr).

    Caminho absoluto de proposito (TECHNOLOGY.md S4): o jarvis pode correr sem
    o venv ativado no PATH do processo, e o PiperEngine cai para "piper.exe"
    sem caminho nenhum se nao lhe disserem onde procurar.
    """
    candidato = Path(sys.executable).resolve().parent / "piper.exe"
    verificar_executavel_seguro(candidato)  # defesa em profundidade (D48.1)
    if not candidato.is_file():
        raise FileNotFoundError(
            f"piper.exe nao encontrado em '{candidato}'. Instalar com: "
            '.venv\\Scripts\\pip install "RealtimeTTS[piper]==0.8.5" piper-tts==1.8.0 (D47)'
        )
    return candidato


def sondar_stdin_de_um_filho(ambiente: dict[str, str] | None = None) -> tuple[str, str]:
    """Pergunta a um Python filho como e que ele descodifica o stdin.

    Arranca `sys.executable` — o mesmo interprete que o `piper.exe` deste venv
    embute, porque o console script e gerado para ele — e manda-lhe
    `TEXTO_DA_SONDA` em UTF-8 exatamente como o PiperEngine faz
    (`input=texto.encode("utf-8")`, `shell=False`). Devolve
    (encoding_do_stdin_do_filho, texto_que_o_filho_leu).
    """
    sonda = (
        "import sys; "
        "sys.stdout.buffer.write("
        "(sys.stdin.encoding + chr(0) + sys.stdin.read()).encode('utf-8'))"
    )
    resultado = subprocess.run(
        [sys.executable, "-c", sonda],
        input=TEXTO_DA_SONDA.encode("utf-8"),
        capture_output=True,
        check=True,
        shell=False,
        env=ambiente,
    )
    encoding, _, lido = resultado.stdout.decode("utf-8").partition(chr(0))
    return encoding, lido


def preparar_encoding_do_piper(revalidar: bool = False) -> str:
    """Poe o ambiente em UTF-8 para o `piper.exe` e PROVA que ficou.

    Nao basta escrever as variaveis: a falha original era silenciosa (exit 0,
    audio lixo), por isso esta funcao confirma no disco, com um processo filho
    real, que o texto UTF-8 chega inteiro ao outro lado. Se nao chegar, rebenta
    aqui com a causa escrita em vez de deixar sair um WAV corrompido.

    Devolve o encoding do stdin do filho (esperado: "utf-8").
    """
    global _encoding_ja_verificado
    os.environ.update(AMBIENTE_UTF8)
    if _encoding_ja_verificado and not revalidar:
        return "utf-8"

    encoding, lido = sondar_stdin_de_um_filho()
    if lido != TEXTO_DA_SONDA:
        raise RuntimeError(
            "o interprete que corre o piper.exe nao esta a ler o stdin em UTF-8 "
            f"(encoding={encoding!r}); o texto {TEXTO_DA_SONDA!r} chegou como {lido!r}. "
            "Sem isto a voz soa os nomes dos caracteres acentuados em vez da frase. "
            f"Confirmar que nada no ambiente sobrepoe {sorted(AMBIENTE_UTF8)}."
        )
    _encoding_ja_verificado = True
    return encoding


def sintetizar_para_wav_bruto(
    texto: str, caminho_bruto: Path, piper_exe: Path, *, com_som: bool = False
) -> int:
    """Escreve o WAV que sai do Piper, na taxa nativa da voz. Devolve a taxa em Hz.

    `com_som` (D61, opt-in explicito, nunca variavel de ambiente): False por
    omissao — so escreve `caminho_bruto`, nenhum dispositivo de audio e aberto
    (`StreamPlayer.open_stream` nem tenta quando `muted=True`). Com
    `com_som=True` toca a serio nas colunas AO MESMO TEMPO que escreve o
    ficheiro: a escrita do WAV no RealtimeTTS e independente do `muted`
    (`text_to_stream.py`, o `output_wavfile` grava em qualquer dos dois casos).
    """
    from RealtimeTTS import PiperEngine, PiperVoice, TextToAudioStream

    # Repetido aqui de proposito (a verificacao fica em cache, nao custa nada):
    # quem reutilizar esta funcao a partir da T5/T7 sem passar por gerar_wav()
    # tem de apanhar a mesma garantia de codificacao.
    preparar_encoding_do_piper()

    if not MODELO_ONNX.is_file() or not CONFIG_ONNX.is_file():
        raise FileNotFoundError(
            f"voz '{NOME_DA_VOZ}' nao encontrada em {PASTA_MODELOS_PIPER}. Descarregar com: "
            f".venv\\Scripts\\python -m piper.download_voices {NOME_DA_VOZ} "
            f"--download-dir {PASTA_MODELOS_PIPER} (a voz e 'tugão' com til; ver docs/MODELOS.md)"
        )

    voz = PiperVoice(model_file=str(MODELO_ONNX), config_file=str(CONFIG_ONNX))
    motor = PiperEngine(voice=voz, piper_path=str(piper_exe))
    # muted=not com_som: por omissao (com_som=False) nunca abre um dispositivo
    # de saida de audio (D61/S13); so com --com-som explicito e que este
    # script toca alguma coisa alem de escrever o ficheiro.
    # tokenizer="rule-based": o default "nltk+rule-based" manda o stream2sentence
    # fazer `nltk.download("punkt_tab")` na primeira corrida — 4,3 MB vindos da
    # rede, fora de models/ e fora do registo da D14e, e a primeira corrida numa
    # maquina offline deixava de funcionar. O divisor de frases interno chega
    # para o que este script faz (uma frase de cada vez).
    stream = TextToAudioStream(motor, muted=not com_som, language="pt", tokenizer="rule-based")
    stream.feed(texto)
    # O unico sitio onde nasce o processo do piper.exe: e aqui que o ambiente
    # deste processo tem de estar sem segredos (D50(9)).
    with ambiente_sem_segredos():
        stream.play(muted=not com_som, output_wavfile=str(caminho_bruto))
    _, taxa, _ = ler_wav_pcm16(caminho_bruto)
    return taxa


def gerar_wav(texto: str, saida: Path, *, com_som: bool = False) -> tuple[Path, float, int]:
    """Sintetiza `texto` em `saida` (16 kHz mono). Devolve (caminho, duracao_s, bytes).

    `com_som` (D61): False por omissao, nunca abre dispositivo de audio; True
    toca a serio nas colunas ALEM de escrever `saida`, so quando pedido de
    forma explicita (`--com-som` na CLI).
    """
    piper_exe = caminho_do_piper_exe()
    preparar_encoding_do_piper()
    garantir_pasta(saida.parent)
    # Temporario com nome unico na pasta de destino: duas execucoes em paralelo
    # sobre a mesma saida ja nao se pisam, e o sufixo .wav mantem-no coberto
    # pela regra `*.wav` do .gitignore mesmo fora de audio/.
    descritor, nome_bruto = tempfile.mkstemp(
        prefix=f".{saida.stem}-bruto-", suffix=".wav", dir=str(saida.parent)
    )
    os.close(descritor)
    caminho_bruto = Path(nome_bruto)
    try:
        sintetizar_para_wav_bruto(texto, caminho_bruto, piper_exe, com_som=com_som)
        dados, taxa_nativa, canais = ler_wav_pcm16(caminho_bruto)
        if canais != 1:
            raise ValueError(
                f"o Piper devolveu {canais} canais e este script escreve um cabecalho "
                "mono (criterio da T3: WAV mono de 16 kHz)"
            )
        dados_16k = reamostrar_pcm16(dados, taxa_nativa, TAXA_AMOSTRAGEM_PADRAO, canais)
        escrever_wav_pcm16(saida, dados_16k, TAXA_AMOSTRAGEM_PADRAO, canais=canais)
    finally:
        caminho_bruto.unlink(missing_ok=True)
    duracao_s = len(dados_16k) / 2 / TAXA_AMOSTRAGEM_PADRAO
    return saida, duracao_s, len(dados_16k)


# --- Autoteste / regressao do bug de codificacao --------------------------

#: Par de frases do autoteste: a mesma frase com e sem acentos. Com a
#: codificacao certa o Piper diz praticamente o mesmo nos dois casos e as
#: duracoes ficam perto; com a codificacao partida a versao acentuada explode
#: (o Piper passa a soar os nomes dos caracteres: "a til", "paragrafo", ...).
#: E o unico literal com acentos do repositorio e esta escrito como texto, nao
#: com escapes: o teste tem de mandar ao Piper os mesmos bytes que uma frase
#: real do Sponsor, e o ficheiro esta em UTF-8 (a codificacao de origem por
#: omissao no Python 3). O comentario ao lado da a leitura em ASCII.
FRASE_ACENTUADA = "Olá, são três horas e trinta."  # "Ola, sao tres horas e trinta."
FRASE_SEM_ACENTOS = "Ola, sao tres horas e trinta."

#: Acima deste racio duracao(acentuada)/duracao(sem acentos) o texto chegou
#: partido ao Piper. Medido nesta maquina: 1.0x-1.1x com a correcao, 5.2x sem
#: ela (7.89 s contra 1.52 s, numeros do veredicto da tentativa 1).
RACIO_MAXIMO_DE_DURACAO = 2.0


def _autoteste() -> int:
    """Prova a correcao do bug de codificacao, em dois niveis.

    1) sem tocar no Piper: um filho Python com e sem `AMBIENTE_UTF8`, que e o
       mecanismo exato do `piper.exe`;
    2) com o Piper a serio: sintetiza a mesma frase com e sem acentos e compara
       as duracoes — e a unica verificacao que apanha o bug de ponta a ponta, e
       a aceitacao da T3 ("que horas sao") nao a faz porque e 100% ASCII.
    """
    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    # 1. O mecanismo, isolado. Primeiro o ambiente SEM a correcao, so para
    # mostrar o que acontecia antes (nao e afirmacao: uma maquina com a pagina
    # de codigo ANSI em UTF-8 nao reproduz o bug, e isso nao e uma falha).
    ambiente_limpo = {k: v for k, v in os.environ.items() if k not in AMBIENTE_UTF8}
    encoding_antes, lido_antes = sondar_stdin_de_um_filho(ambiente_limpo)
    print(f"     [diagnostico] sem a correcao: stdin={encoding_antes!r} leu {lido_antes!r}")

    encoding_depois = preparar_encoding_do_piper(revalidar=True)
    verificar("encoding: o filho passa a ler o stdin em utf-8", encoding_depois, "utf-8")
    _, lido_depois = sondar_stdin_de_um_filho()
    verificar("encoding: o texto acentuado chega inteiro ao filho", lido_depois, TEXTO_DA_SONDA)
    verificar(
        "encoding: as duas variaveis ficam no ambiente deste processo",
        {chave: os.environ.get(chave) for chave in AMBIENTE_UTF8},
        AMBIENTE_UTF8,
    )

    # 1b. Menor privilegio do filho (D50(9)): enquanto o piper corre, o
    # ambiente deste processo nao tem segredo nenhum — e o filho herda-o tal e
    # qual, porque o PiperEngine arranca sem `env=`. Provado com um filho real,
    # arrancado do mesmo modo, a listar o que ve. Os dois nomes de teste sao
    # inventados para nao pisarem variaveis verdadeiras da sessao.
    NOMES_DE_TESTE = ("CLAUDE_TESTE_T3_MESSAGING_TOKEN", "JARVIS_TESTE_T3_API_KEY")
    listar = (
        "import os, sys; padroes = "
        + repr(PADROES_DE_VARIAVEL_SENSIVEL)
        + "; sys.stdout.write(','.join(sorted(k for k in os.environ "
        "if k.upper().startswith('CLAUDE') "
        "or any(p in k.upper() for p in padroes))))"
    )
    for nome in NOMES_DE_TESTE:
        os.environ[nome] = "valor-de-teste-inventado"
    try:
        with ambiente_sem_segredos() as escondidas:
            visiveis_no_filho = subprocess.run(
                [sys.executable, "-c", listar],
                capture_output=True,
                check=True,
                shell=False,
            ).stdout.decode("utf-8")
            verificar("ambiente: o filho do piper nao ve segredo nenhum", visiveis_no_filho, "")
            verificar(
                "ambiente: os dois segredos de teste estao na lista dos escondidos",
                sorted(nome for nome in escondidas if nome in NOMES_DE_TESTE),
                sorted(NOMES_DE_TESTE),
            )
            verificar(
                "ambiente: o PYTHONUTF8 do piper NAO e escondido com eles",
                os.environ.get("PYTHONUTF8"),
                "1",
            )
        verificar(
            "ambiente: fora do bloco fica tudo como estava (a T4 corre num processo vivo)",
            [os.environ.get(nome) for nome in NOMES_DE_TESTE],
            ["valor-de-teste-inventado", "valor-de-teste-inventado"],
        )
    finally:
        for nome in NOMES_DE_TESTE:
            os.environ.pop(nome, None)
    verificar(
        "ambiente: PATH, SystemRoot e TEMP nunca sao tocados",
        (variavel_sensivel("PATH"), variavel_sensivel("SystemRoot"), variavel_sensivel("TEMP")),
        (False, False, False),
    )

    # 2. O Piper a serio, que e o que o bug estragava.
    with tempfile.TemporaryDirectory() as pasta:
        alvo = Path(pasta)
        _, duracao_com, _ = gerar_wav(FRASE_ACENTUADA, alvo / "acentuada.wav")
        _, duracao_sem, _ = gerar_wav(FRASE_SEM_ACENTOS, alvo / "sem-acentos.wav")
        racio = duracao_com / duracao_sem if duracao_sem else float("inf")
        print(
            f"     [medido] acentuada {duracao_com:.2f} s / sem acentos {duracao_sem:.2f} s "
            f"= {racio:.2f}x (limite {RACIO_MAXIMO_DE_DURACAO}x)"
        )
        verificar(
            "piper: a frase acentuada nao dispara em duracao (texto chegou inteiro)",
            racio <= RACIO_MAXIMO_DE_DURACAO,
            True,
        )
        verificar("piper: a frase acentuada produz audio audivel (> 0.5 s)", duracao_com > 0.5, True)

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print(
        "OK: autoteste do gerar_wav completo "
        "(codificacao UTF-8 do piper, ambiente sem segredos, sintese acentuada)."
    )
    return 0


def main() -> int:
    forcar_consola_utf8()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("texto", nargs="?", help="frase a sintetizar em portugues europeu")
    parser.add_argument(
        "--saida",
        default=str(PASTA_AUDIO / "saida.wav"),
        help=(
            "caminho do WAV a escrever, dentro do repositorio e com sufixo .wav "
            "(default: audio/saida.wav)"
        ),
    )
    parser.add_argument(
        "--com-som",
        action="store_true",
        help=(
            "opt-in explicito para tocar nas colunas (D61), alem de escrever o WAV; "
            "sem esta flag nada toca — so o ficheiro e escrito"
        ),
    )
    parser.add_argument(
        "--autoteste",
        action="store_true",
        help="corre a regressao da codificacao UTF-8 do piper (nao escreve em audio/, nunca toca som)",
    )
    args = parser.parse_args()

    if args.autoteste:
        return _autoteste()

    if args.texto is None:
        parser.error("e preciso o texto a sintetizar (ou --autoteste)")

    if not args.texto.strip():
        print("ERRO: o texto nao pode ser vazio.", file=sys.stderr)
        return 1

    # O caminho vem de fora (D48(2)): confinado ao repositorio e com sufixo
    # .wav, o unico que o .gitignore apanha em toda a arvore.
    try:
        saida = caminho_wav_de_saida(args.saida)
    except ValueError as erro:
        print(f"ERRO: {erro}", file=sys.stderr)
        return 1

    print("=== jarvis - gerar_wav (Piper, voz pt_PT-tugao-medium) ===")
    print(f"texto           = {args.texto!r}")
    print(f"com som         = {'sim (D61 opt-in)' if args.com_som else 'nao (so ficheiro, D61)'}")
    t0 = time.perf_counter()
    try:
        caminho, duracao_s, n_bytes = gerar_wav(args.texto, saida, com_som=args.com_som)
    except Exception as erro:
        print(f"FALHOU: {erro}", file=sys.stderr)
        return 1
    decorrido = time.perf_counter() - t0

    print(f"ficheiro        = {caminho_para_mostrar(caminho)}")
    print("codificacao     = UTF-8 confirmada no stdin do piper (verificada nesta execucao)")
    print(f"taxa            = {TAXA_AMOSTRAGEM_PADRAO} Hz, mono, 16 bits")
    print(f"duracao         = {duracao_s:.2f} s ({n_bytes} bytes de audio)")
    print(f"tempo de sintese = {decorrido:.2f} s")
    print("OK: WAV escrito.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
