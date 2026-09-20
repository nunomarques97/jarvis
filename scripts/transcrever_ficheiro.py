r"""Transcreve um ficheiro WAV no GPU com faster-whisper (D33: prova sem microfone).

Modelo `medium`, com fallback automatico para `small` se o `medium` nao
carregar ou nao transcrever no device pedido (D39/TECHNOLOGY.md S3). Device
por omissao `cuda`; a regra da D42 (T1) aplica-se sempre que o device e cuda:
importar torch e registar as suas DLLs ANTES de importar faster_whisper /
ctranslate2, senao o CTranslate2 nao encontra o cuBLAS/cuDNN que vieram no
wheel do torch e rebenta so no primeiro encode.

ACHADO desta task, com evidencia no relatorio: um WAV isolado de menos de 1 s
(como "que horas sao" sintetizado pelo Piper, ~0.7 s) faz o faster-whisper
alucinar uma frase comum de treino (ex.: "Tchau, pessoal.") em vez de
transcrever o audio — reproduzido em medium, small E large-v3, com e sem VAD,
com e sem padding de silencio/ruido, com varias taxas de temperatura, sempre
com o mesmo resultado errado. Isto e uma limitacao conhecida do Whisper para
frases isoladas ultra-curtas (o encoder preenche sempre a janela de 30 s), nao
um erro deste script.

PROMPT DESLIGADO POR OMISSAO (D51). Um `initial_prompt` com o vocabulario de
comandos (PROMPT_VOCABULARIO_PADRAO, abaixo) faz a frase curta acertar, mas e
polarizacao de vocabulario, nao capacidade de transcrever: com ele ligado, 3 s
de ruido branco sem fala nenhuma saiam como uma frase de comando inteira, que e
o inaceitavel n.2 do PRODUCT-PROFILE. Por isso, e por a D7 so autorizar o
initial_prompt DEPOIS de medir e dentro da banda 75-90%:

  - `transcrever(...)` tem `initial_prompt=None` por omissao: quem o quiser
    passa-o a olho, nunca por heranca silenciosa (T4/T6);
  - o CLI corre sem prompt a menos que se peca `--com-prompt` ou `--prompt`;
  - o estado do prompt e SEMPRE impresso e vem no dict devolvido, para o log
    com timestamps da T4/T6 o carregar sem depender do CLI.

O prompt desligado nao impede o ruido de alucinar (fica "Obrigado por
assistir!" em vez de um comando): quem garante que ruido nunca vira acao e o
VAD mais o encaminhador determinista com lista branca (D4/D5/D9).

Uso:
    .venv\Scripts\python scripts/transcrever_ficheiro.py audio/t-horas.wav
    .venv\Scripts\python scripts/transcrever_ficheiro.py audio/t-horas.wav --com-prompt
    .venv\Scripts\python scripts/transcrever_ficheiro.py --autoteste
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.audio_util import (  # noqa: E402
    PASTA_MODELOS_FASTER_WHISPER,
    registar_dlls_do_torch,
)

MODELO_PREFERIDO = "medium"
MODELO_FALLBACK = "small"

#: Lista fechada de modelos, como o --device ja tinha. Sem ela, a string ia
#: direta para `faster_whisper.WhisperModel` e o `download_model` aceita
#: qualquer `repo/id` do Hugging Face: `--modelo alguem/repo-mau` descarregava
#: pesos arbitrarios para models/, fora do registo da D14e e da decisao do
#: Scout (D39/TECHNOLOGY.md S3), e mandava-os ao parser binario do CTranslate2.
MODELOS_PERMITIDOS = ["tiny", "base", "small", "medium", "large-v3"]

#: Vocabulario de comandos do jarvis (D4, sem nomes de projetos privados —
#: D10). NAO e usado por omissao (D51): fica aqui para quem o queira passar
#: explicitamente, em `--com-prompt` ou em `transcrever(initial_prompt=...)`.
#: Generico de proposito: e o vocabulario inteiro da lista branca mais o
#: encaminhamento livre, nao a resposta da frase de teste.
PROMPT_VOCABULARIO_PADRAO = (
    "Comandos por voz em portugues europeu para o jarvis: que horas sao, "
    "abre o VS Code, abre esta pasta, cala-te, adormece, acorda, "
    "ou uma pergunta qualquer para o Claude Code."
)

#: Rotulos do estado do prompt (D51(c)): os mesmos no ecra e no dict, para o
#: log com timestamps da T4/T6 nao ter de reinventar nomes.
ESTADO_PROMPT_DESLIGADO = "desligado"
ESTADO_PROMPT_PADRAO = "ligado (vocabulario padrao)"
ESTADO_PROMPT_PERSONALIZADO = "ligado (personalizado)"


def estado_do_prompt(initial_prompt: str | None) -> str:
    """Rotulo do estado do prompt, o mesmo no output e no dict (D51(c))."""
    if not initial_prompt:
        return ESTADO_PROMPT_DESLIGADO
    if initial_prompt == PROMPT_VOCABULARIO_PADRAO:
        return ESTADO_PROMPT_PADRAO
    return ESTADO_PROMPT_PERSONALIZADO


def texto_para_a_consola(texto: str, codificacao: str | None = None) -> str:
    """O mesmo texto, garantidamente imprimivel na consola em uso.

    O que nao couber na pagina de codigo da consola vira `?` em vez de rebentar
    com UnicodeEncodeError: perder um acento na consola e mau, perder uma
    transcricao ja feita por causa dele e pior.
    """
    codificacao = codificacao or sys.stdout.encoding or "utf-8"
    try:
        texto.encode(codificacao)
    except (UnicodeEncodeError, LookupError):
        return texto.encode(codificacao, errors="replace").decode(codificacao, errors="replace")
    return texto


def tipo_de_computo(device: str) -> str:
    """float16 no GPU (D39: equilibrio latencia/precisao), int8 fora dele."""
    return "float16" if device == "cuda" else "int8"


#: Modelos ja carregados neste processo, por (nome, device). So e consultado
#: quando quem chama pede `usar_cache=True`: o comportamento por omissao fica
#: exatamente como estava (um carregamento por chamada), porque e nele que a
#: T3/T6 mediram a latencia de carregamento. Quem corre a mesma transcricao
#: dezenas de vezes seguidas no mesmo processo — o arnes da T7, 20 frases —
#: paga hoje ~5 s de carregamento por frase sem nenhum ganho.
_MODELOS_EM_CACHE: dict[tuple[str, str], object] = {}


def limpar_cache_de_modelos() -> int:
    """Esvazia a cache de modelos e devolve quantos estavam la dentro.

    Enquanto a cache tiver um modelo, ele ocupa VRAM ate o processo acabar
    (nit 8 do Reviewer, T7 a2). Num CLI que termina a seguir isso e inofensivo;
    num processo longo (a T4/T6, se alguma vez ligarem `usar_cache_do_modelo`)
    passa a ser a diferenca entre libertar o GPU e nao o libertar. Tambem e o
    que os testes usam para nao deixar estado de um teste no seguinte.
    """
    quantos = len(_MODELOS_EM_CACHE)
    _MODELOS_EM_CACHE.clear()
    return quantos


def carregar_modelo(nome: str, device: str, usar_cache: bool = False):
    chave = (nome, device)
    if usar_cache:
        em_cache = _MODELOS_EM_CACHE.get(chave)
        if em_cache is not None:
            return em_cache

    import faster_whisper

    modelo = faster_whisper.WhisperModel(
        nome,
        device=device,
        compute_type=tipo_de_computo(device),
        download_root=str(PASTA_MODELOS_FASTER_WHISPER),
    )
    if usar_cache:
        _MODELOS_EM_CACHE[chave] = modelo
    return modelo


def device_real_do_modelo(modelo, device_pedido: str) -> str:
    """O device onde o CTranslate2 ficou mesmo a correr (nao o pedido)."""
    interno = getattr(modelo, "model", None)
    return getattr(interno, "device", device_pedido)


def transcrever(
    caminho: Path,
    device: str = "cuda",
    modelo_preferido: str = MODELO_PREFERIDO,
    modelo_fallback: str = MODELO_FALLBACK,
    initial_prompt: str | None = None,
    usar_cache_do_modelo: bool = False,
) -> dict:
    """Carrega o modelo (com fallback) e transcreve `caminho`. Devolve um resumo em dict.

    `initial_prompt=None` por omissao (D51): sem polarizacao de vocabulario a
    menos que quem chama a peca. Quem a quiser passa
    `initial_prompt=PROMPT_VOCABULARIO_PADRAO` explicitamente.

    `usar_cache_do_modelo=False` por omissao (comportamento inalterado: um
    carregamento por chamada, que e o que a T3/T6 mediram). A `True`, o modelo
    fica em cache por (nome, device) durante o processo — e o que o arnes da T7
    usa para nao pagar 20 carregamentos do `medium` numa corrida de 20 frases.

    `condition_on_previous_text=False` de proposito: cada ficheiro desta task e
    uma frase isolada, nao uma sessao continua, e deixar isto a True so
    aumentaria o risco de a alucinacao de um segmento contaminar o seguinte.
    """
    if device == "cuda":
        registar_dlls_do_torch()  # tem de correr antes do import do faster_whisper (D42)

    if not initial_prompt:
        initial_prompt = None  # "" e None sao a mesma coisa para o faster-whisper

    modelo_usado = modelo_preferido
    aviso_fallback = ""
    # Tres relogios: o total (que era a unica coisa medida ate aqui) e os dois
    # numeros que o compoem. Carregar o modelo e ~3/4 do total e acontece uma
    # vez por processo; a T4 vai manter o modelo carregado, por isso a latencia
    # que interessa a experiencia e so a segunda.
    t0 = time.perf_counter()
    inicio_do_modelo_usado = t0
    fim_do_carregamento = t0
    try:
        modelo = carregar_modelo(modelo_preferido, device, usar_cache=usar_cache_do_modelo)
        fim_do_carregamento = time.perf_counter()
        segmentos, info = modelo.transcribe(
            str(caminho),
            language="pt",
            beam_size=5,
            vad_filter=False,
            initial_prompt=initial_prompt,
            condition_on_previous_text=False,
        )
        segmentos = list(segmentos)
    except Exception as erro_preferido:
        aviso_fallback = (
            f"'{modelo_preferido}' falhou em {device} ({erro_preferido!r}); "
            f"a tentar fallback '{modelo_fallback}' (D39)"
        )
        modelo_usado = modelo_fallback
        inicio_do_modelo_usado = time.perf_counter()
        try:
            modelo = carregar_modelo(modelo_fallback, device, usar_cache=usar_cache_do_modelo)
            fim_do_carregamento = time.perf_counter()
            segmentos, info = modelo.transcribe(
                str(caminho),
                language="pt",
                beam_size=5,
                vad_filter=False,
                initial_prompt=initial_prompt,
                condition_on_previous_text=False,
            )
            segmentos = list(segmentos)
        except Exception as erro_fallback:
            # Sem isto o erro do 'medium' perdia-se e so o do 'small' chegava
            # a quem esta a ler: os dois modelos falham por razoes diferentes
            # (ex.: falta de VRAM num, ficheiro corrompido no outro).
            raise RuntimeError(
                f"'{modelo_preferido}' falhou em {device} ({erro_preferido!r}) e o fallback "
                f"'{modelo_fallback}' tambem falhou ({erro_fallback!r})"
            ) from erro_fallback
    fim = time.perf_counter()
    latencia_ms = (fim - t0) * 1000
    latencia_carregamento_ms = (fim_do_carregamento - inicio_do_modelo_usado) * 1000
    latencia_transcricao_ms = (fim - fim_do_carregamento) * 1000

    texto = " ".join(segmento.text.strip() for segmento in segmentos).strip()
    return {
        "texto": texto,
        "modelo": modelo_usado,
        "device": device_real_do_modelo(modelo, device),
        "latencia_ms": latencia_ms,
        # Os dois numeros que compoem o total (o carregamento e do modelo que
        # ficou mesmo a transcrever; se houve fallback, o total inclui alem
        # disso a tentativa falhada).
        "latencia_carregamento_ms": latencia_carregamento_ms,
        "latencia_transcricao_ms": latencia_transcricao_ms,
        "duracao_audio_s": info.duration,
        "aviso_fallback": aviso_fallback,
        # D51(c): o estado do prompt viaja no dict, para o log com timestamps
        # da T4/T6 o registar sem passar pelo CLI.
        "prompt_estado": estado_do_prompt(initial_prompt),
        "initial_prompt": initial_prompt,
    }


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ficheiro", nargs="?", help="caminho do WAV a transcrever")
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    parser.add_argument("--modelo", default=MODELO_PREFERIDO, choices=MODELOS_PERMITIDOS)
    parser.add_argument("--fallback", default=MODELO_FALLBACK, choices=MODELOS_PERMITIDOS)
    grupo = parser.add_mutually_exclusive_group()
    grupo.add_argument(
        "--com-prompt",
        action="store_true",
        help=(
            "liga o initial_prompt com o vocabulario de comandos do jarvis "
            "(PROMPT_VOCABULARIO_PADRAO); por omissao o prompt esta DESLIGADO (D51)"
        ),
    )
    grupo.add_argument(
        "--prompt",
        default=None,
        metavar="TEXTO",
        help="liga o initial_prompt com um vocabulario proprio, em vez do padrao",
    )
    parser.add_argument(
        "--autoteste",
        action="store_true",
        help="corre a regressao do estado do prompt (nao carrega modelo nem toca no GPU)",
    )
    return parser


def prompt_dos_argumentos(args: argparse.Namespace) -> str | None:
    """O initial_prompt pedido na linha de comandos. Por omissao: nenhum (D51(b))."""
    if args.com_prompt:
        return PROMPT_VOCABULARIO_PADRAO
    return args.prompt or None


def main() -> int:
    parser = construir_parser()
    args = parser.parse_args()

    if args.autoteste:
        return _autoteste()

    if args.ficheiro is None:
        parser.error("e preciso o caminho do WAV a transcrever (ou --autoteste)")

    caminho = Path(args.ficheiro)
    if not caminho.is_file():
        print(f"ERRO: ficheiro nao encontrado: {caminho}", file=sys.stderr)
        return 1

    prompt = prompt_dos_argumentos(args)

    print("=== jarvis - transcrever_ficheiro (faster-whisper) ===")
    print(f"ficheiro        = {caminho}")
    # D51(c): sempre impresso, nos dois modos, antes de haver transcricao —
    # uma transcricao sem rasto do estado do prompt nao conta como evidencia.
    print(f"prompt          = {estado_do_prompt(prompt)}")
    try:
        resultado = transcrever(caminho, args.device, args.modelo, args.fallback, prompt)
    except Exception as erro:
        print(f"FALHOU: {erro!r}", file=sys.stderr)
        return 1

    if resultado["aviso_fallback"]:
        print(f"aviso           = {resultado['aviso_fallback']}")
    print(f"modelo          = {resultado['modelo']}")
    print(f"device          = {resultado['device']}")
    print(f"duracao audio   = {resultado['duracao_audio_s']:.2f} s")
    print(
        f"latencia        = {resultado['latencia_ms']:.1f} ms "
        f"(carregar o modelo {resultado['latencia_carregamento_ms']:.1f} ms "
        f"+ transcrever {resultado['latencia_transcricao_ms']:.1f} ms)"
    )
    # A transcricao e o unico texto aqui que pode ter caracteres fora da pagina
    # de codigo da consola (o Whisper devolve UTF-8 e o stdout do Windows e
    # cp1252 quando isto corre num pipe): sem isto, um caractere fora do mapa
    # rebentava com UnicodeEncodeError DEPOIS de a transcricao estar feita.
    print(f"transcricao     = {texto_para_a_consola(resultado['texto'])}")
    print("OK: transcricao feita.")
    return 0


# --- Autoteste / regressao do default do prompt (D51) ---------------------


def _autoteste() -> int:
    """Prova a D51 sem carregar modelo nenhum: default da funcao, default do
    CLI, rotulos do estado e o que a ajuda promete.
    """
    import contextlib
    import inspect
    import io

    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    # 1. D51(a): a assinatura da FUNCAO nao polariza nada por omissao. E esta
    # a verificacao que apanha a regressao real: a T4/T6 chamam transcrever()
    # sem passar prompt nenhum.
    omissao = inspect.signature(transcrever).parameters["initial_prompt"].default
    verificar("D51(a) transcrever(initial_prompt=...) por omissao e None", omissao, None)

    # 2. D51(b): o comando nu do criterio nao liga o prompt; as duas flags
    # ligam-no, e sao mutuamente exclusivas.
    parser = construir_parser()
    verificar(
        "D51(b) comando nu: prompt desligado",
        prompt_dos_argumentos(parser.parse_args(["audio/t-horas.wav"])),
        None,
    )
    verificar(
        "D51(b) --com-prompt: vocabulario padrao",
        prompt_dos_argumentos(parser.parse_args(["audio/t-horas.wav", "--com-prompt"])),
        PROMPT_VOCABULARIO_PADRAO,
    )
    verificar(
        "D51(b) --prompt TEXTO: vocabulario proprio",
        prompt_dos_argumentos(parser.parse_args(["audio/t-horas.wav", "--prompt", "abc"])),
        "abc",
    )
    exclusivas = True
    try:
        # stderr silenciado: o argparse imprime o uso ao recusar, e esse ruido
        # no meio do autoteste parece uma falha sem o ser.
        with contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["x.wav", "--com-prompt", "--prompt", "abc"])
        exclusivas = False
    except SystemExit:
        pass
    verificar("D51(b) --com-prompt e --prompt nao se combinam", exclusivas, True)

    # 3. D51(c): os tres rotulos, exatamente como a decisao os escreve.
    verificar("D51(c) rotulo sem prompt", estado_do_prompt(None), "desligado")
    verificar("D51(c) rotulo com prompt vazio", estado_do_prompt(""), "desligado")
    verificar(
        "D51(c) rotulo com o vocabulario padrao",
        estado_do_prompt(PROMPT_VOCABULARIO_PADRAO),
        "ligado (vocabulario padrao)",
    )
    verificar(
        "D51(c) rotulo com vocabulario proprio",
        estado_do_prompt("so numeros e horas"),
        "ligado (personalizado)",
    )

    # 4. A ajuda nao pode mentir (exigencia literal da D51(b)).
    ajuda = parser.format_help()
    verificar("D51(b) o --help diz que o prompt esta desligado por omissao", "DESLIGADO" in ajuda, True)
    verificar("D51(b) o --help ja nao anuncia um --sem-prompt", "--sem-prompt" in ajuda, False)

    # 5. Lista fechada de modelos: a string ia direta para o Hugging Face.
    def modelo_recusado(valor: str) -> bool:
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["x.wav", "--modelo", valor])
        except SystemExit:
            return True
        return False

    def fallback_recusado(valor: str) -> bool:
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["x.wav", "--fallback", valor])
        except SystemExit:
            return True
        return False

    verificar(
        "modelos: os dois modelos da D39 sao aceites",
        [modelo_recusado("medium"), modelo_recusado("small")],
        [False, False],
    )
    verificar("modelos: um repo qualquer do Hugging Face e recusado", modelo_recusado("alguem/repo-mau"), True)
    verificar("modelos: um caminho local tambem e recusado", modelo_recusado("../../modelo"), True)
    verificar(
        "modelos: o --fallback tem a mesma lista fechada",
        [fallback_recusado("small"), fallback_recusado("alguem/repo-mau")],
        [False, True],
    )

    # 6. A transcricao nunca se perde por causa da consola. Escapes \uXXXX de
    # proposito: o que esta a ser testado e a codificacao, e um literal com
    # acentos aqui tornaria o teste refem de como o ficheiro foi gravado.
    acentuada = "Ol\u00e1, s\u00e3o tr\u00eas horas"
    fora_do_mapa = "horas \u65e5\u672c"
    verificar("consola: texto que cabe na pagina de codigo passa intacto", texto_para_a_consola(acentuada, "cp1252"), acentuada)
    verificar("consola: caracteres fora do mapa viram ? em vez de rebentar", texto_para_a_consola(fora_do_mapa, "cp1252"), "horas ??")
    verificar("consola: em utf-8 nada e substituido", texto_para_a_consola(fora_do_mapa, "utf-8"), fora_do_mapa)

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print(
        "OK: autoteste do transcrever_ficheiro completo "
        "(D51 a/b/c, lista fechada de modelos, protecao de codificacao da consola)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
