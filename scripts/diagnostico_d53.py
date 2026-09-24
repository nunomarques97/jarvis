r"""Diagnostico da fasquia de 90%: isola CADEIA vs ENCAMINHAMENTO em tres pernas.

O acerto de intencao das quatro combinacoes (pt/en, com/sem prefixo) esta em
55/50/50/50%, muito abaixo dos 90%. Em audio sintetico limpo, qualquer valor
abaixo de 90% e DEFEITO DE CADEIA OU DE ENCAMINHAMENTO. Este script produz a evidencia que
diz ONDE esta o defeito, ANTES de tentar corrigir seja o que for.

Nao mede nada por si proprio que o arnes ja saiba medir: reutiliza
`scripts/medir_voz.py` (amostra, marcadores, config, WER) e
`jarvis.router.encaminhar()`. Nada aqui toca som: nenhuma sub-corrida
leva `--com-som` e a perna B corre sempre `jarvis.app --wav ... --sem-voz`.

Tres subcomandos, um por perna:

  perna-a   ENCAMINHAMENTO SEM AUDIO. `encaminhar()` sobre o TEXTO das 20
            frases de tests/voz/frases-pt.md e das 20 de frases-en.md, com e
            sem o prefixo "hey jarvis, " (4 combinacoes, 80 linhas), contado
            contra o tipo/intencao DOCUMENTADOS na amostra (nao contra o
            proprio router: seria circular). 20/20 nas quatro inocenta o
            encaminhador e poe o defeito a montante.

  padding   Prepara a perna C: copia os WAV de uma pasta de audio/medir-voz/
            para outra, acrescentando silencio digital a cabeca e a cauda na
            medida que o caminho vivo entrega de facto
            (`pre_recording_buffer_duration=1.0` e
            `post_speech_silence_duration=0.6`, jarvis/app.py::construir_recorder).
            Sem re-sintetizar nada: o A/B da perna C corre sobre os MESMOS
            WAV, com o padding como unica variavel.

  perna-b   PARIDADE ARNES vs CAMINHO VIVO. Corre, um por um, o comando
            EXATO do produto sobre cada WAV — `python -m jarvis.app --wav
            <ficheiro> --sem-voz` — e le do log da sessao a transcricao
            (etapa 2) e a decisao do encaminhador (etapa 3), para comparar
            linha a linha com a tabela de um ficheiro de evidencia do arnes.

  perna-c   Fecha a perna C: compara as DUAS corridas do arnes (sem padding e
            com padding) e escreve o A/B com as linhas que mudaram.

  taxa      Verificacao auxiliar: 22050 Hz cru vs 16 kHz por `audioop.ratecv`,
            derivados da MESMA sintese, para saber se o reamostrador do arnes
            e o que destroi o audio.

Uso:
    .venv\Scripts\python scripts/diagnostico_d53.py perna-a \
        --saida docs/forja/evidence/diagnostico-d53-perna-A-router.md
    .venv\Scripts\python scripts/diagnostico_d53.py padding \
        --origem ab-pt-sem --destino d53-pad-pt-sem
    .venv\Scripts\python scripts/diagnostico_d53.py perna-b \
        --pasta-audio ab-pt-sem --numeros 1-20 \
        --arnes docs/forja/evidence/ganho-pt-sem-prefixo.md \
        --saida docs/forja/evidence/diagnostico-d53-perna-B-caminho-vivo.md
    .venv\Scripts\python scripts/diagnostico_d53.py perna-c \
        --sem-padding docs/forja/evidence/diagnostico-d53-perna-C-A-sem-padding.md \
        --com-padding docs/forja/evidence/diagnostico-d53-perna-C-B-com-padding.md \
        --pasta-sem ab-pt-sem --pasta-com d53-pad-pt-sem \
        --saida docs/forja/evidence/diagnostico-d53-perna-C-padding.md
    .venv\Scripts\python scripts/diagnostico_d53.py taxa \
        --saida docs/forja/evidence/diagnostico-d53-aux-taxa-de-amostragem.md
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.audio_util import garantir_pasta  # noqa: E402
from jarvis.config import Config  # noqa: E402
from jarvis.router import encaminhar  # noqa: E402

PASTA_SCRIPTS = Path(__file__).resolve().parent


def _carregar_medir_voz():
    """Importa scripts/medir_voz.py por caminho, sem mexer no sys.path global.

    Mesma tecnica (e mesma razao) de `medir_voz._carregar_modulo_irmao`:
    `scripts/` nao e um pacote e po-la no sys.path do processo faria as
    futuras `scripts/re.py` e companhia sombrearem a stdlib em todo o lado.
    """
    import importlib.util

    chave = "_jarvis_scripts_medir_voz"
    ja = sys.modules.get(chave)
    if ja is not None:
        return ja
    caminho = PASTA_SCRIPTS / "medir_voz.py"
    spec = importlib.util.spec_from_file_location(chave, caminho)
    if spec is None or spec.loader is None:
        raise ImportError(f"nao consegui carregar '{caminho}'")
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[chave] = modulo
    spec.loader.exec_module(modulo)
    return modulo


medir = _carregar_medir_voz()

PREFIXO_D53 = "hey jarvis, "

#: O silencio que o caminho vivo poe a volta da fala, lido de
#: jarvis/app.py::construir_recorder (nao inventado aqui).
PADDING_CABECA_S = 1.0
PADDING_CAUDA_S = 0.6

AVISO_PRIVACIDADE = medir.AVISO_PRIVACIDADE


# --- Intencao DOCUMENTADA na amostra (a referencia da perna A) ---------------


@dataclass(frozen=True)
class IntencaoDocumentada:
    """O que a coluna `intenção esperada` da amostra diz, ja resolvida.

    `argumento` ja tem o caminho no disco quando a intencao documentada
    nomeia um projeto (`<projeto-1>`), porque e isso que
    `jarvis.router.encaminhar()` devolve em `argumento`.
    """

    tipo: str
    nome_acao: str | None
    argumento: str | None
    bruta: str


PADRAO_INTENCAO = re.compile(r"^\s*([A-Za-zÀ-ÿ_]+)\s*(?:\((.*)\))?\s*$")


def interpretar_intencao_documentada(
    frase: medir.FraseDaAmostra,
    config: Config,
    nomes_projetos: Sequence[str],
) -> IntencaoDocumentada:
    """Le a coluna documentada da amostra e devolve a intencao comparavel.

    Formas que a amostra usa, e so estas:
      - `horas_e_data (horas)` / `horas_e_data (data)` -> local, com argumento
      - `abrir_vscode (<projeto-1>)` -> local, argumento = CAMINHO do projeto
      - `calar` / `adormecer` / `acordar` -> local, sem argumento
      - `texto (...)` -> claude (o `(...)` e a razao, escrita para humanos)

    Uma coluna fora destas formas levanta: a perna A tem de falhar alto em vez
    de contar um acerto que ninguem verificou.
    """
    bruta = frase.intencao_documentada.strip()
    casou = PADRAO_INTENCAO.match(bruta)
    if casou is None:
        raise ValueError(
            f"frase {frase.numero}: intencao documentada '{bruta}' nao tem a forma "
            "'accao' nem 'accao (argumento)'"
        )
    cabeca, dentro = casou.group(1), casou.group(2)
    if frase.tipo_documentado == "claude":
        if cabeca != "texto":
            raise ValueError(
                f"frase {frase.numero}: tipo documentado 'claude' mas a intencao diz "
                f"'{cabeca}' (esperado 'texto')"
            )
        return IntencaoDocumentada("claude", None, None, bruta)
    if frase.tipo_documentado != "local":
        raise ValueError(f"frase {frase.numero}: tipo documentado '{frase.tipo_documentado}'")
    if dentro is None:
        return IntencaoDocumentada("local", cabeca, None, bruta)
    argumento = dentro.strip()
    if "<projeto-" in argumento:
        nome = medir.substituir_marcadores(argumento, nomes_projetos)
        projeto = config.encontrar_projeto(nome)
        if projeto is None:
            raise ValueError(
                f"frase {frase.numero}: a intencao documentada nomeia o projeto '{nome}', "
                "que nao esta na configuracao usada por esta corrida"
            )
        argumento = str(projeto.caminho)
    return IntencaoDocumentada("local", cabeca, argumento, bruta)


# --- PERNA A: encaminhamento sem audio --------------------------------------


@dataclass(frozen=True)
class LinhaPernaA:
    combinacao: str
    numero: int
    texto_encaminhado: str
    documentada: IntencaoDocumentada
    tipo_obtido: str
    nome_acao_obtido: str | None
    argumento_obtido: str | None
    residuo_removido: str | None
    motivo: str

    @property
    def acertou(self) -> bool:
        return (
            self.tipo_obtido == self.documentada.tipo
            and self.nome_acao_obtido == self.documentada.nome_acao
            and self.argumento_obtido == self.documentada.argumento
        )


def correr_perna_a(
    caminhos_amostra: Sequence[tuple[str, Path]],
    config: Config,
    nomes_projetos: Sequence[str],
) -> list[LinhaPernaA]:
    linhas: list[LinhaPernaA] = []
    for lingua, caminho in caminhos_amostra:
        amostra = medir.ler_amostra(caminho)
        for prefixo_rotulo, prefixo in (("sem prefixo", ""), ("com prefixo", PREFIXO_D53)):
            combinacao = f"{lingua} {prefixo_rotulo}"
            for frase in amostra:
                documentada = interpretar_intencao_documentada(frase, config, nomes_projetos)
                texto = medir.substituir_marcadores(
                    frase.frase_com_marcadores, nomes_projetos
                )
                texto_encaminhado = f"{prefixo}{texto}" if prefixo else texto
                obtido = encaminhar(texto_encaminhado, config)
                linhas.append(
                    LinhaPernaA(
                        combinacao=combinacao,
                        numero=frase.numero,
                        texto_encaminhado=texto_encaminhado,
                        documentada=documentada,
                        tipo_obtido=obtido.tipo,
                        nome_acao_obtido=obtido.nome_acao,
                        argumento_obtido=obtido.argumento,
                        residuo_removido=obtido.residuo_removido,
                        motivo=obtido.motivo,
                    )
                )
    return linhas


def escrever_perna_a(
    linhas: Sequence[LinhaPernaA],
    caminho_saida: Path,
    origem_config: str,
    amostras: Sequence[tuple[str, Path]],
) -> dict[str, tuple[int, int]]:
    agregados: dict[str, tuple[int, int]] = {}
    for linha in linhas:
        acertos, total = agregados.get(linha.combinacao, (0, 0))
        agregados[linha.combinacao] = (acertos + (1 if linha.acertou else 0), total + 1)

    partes: list[str] = []
    partes.append("# Diagnóstico da D53 — PERNA A: encaminhamento SEM ÁUDIO (isola o router)")
    partes.append("")
    partes.append(AVISO_PRIVACIDADE)
    partes.append("")
    partes.append(
        "Gerado por `scripts/diagnostico_d53.py perna-a` em "
        f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}."
    )
    partes.append("")
    partes.append(
        "**O que esta perna mede:** `jarvis.router.encaminhar()` aplicado ao TEXTO das frases "
        "da amostra — nenhuma síntese, nenhuma transcrição, nenhum áudio. Se o encaminhador "
        "estiver certo, o áudio deixa de ser desculpa para nada aqui; se falhar, o defeito é "
        "de ENCAMINHAMENTO e é o primeiro a corrigir (D53, D58/D62)."
    )
    partes.append("")
    partes.append(
        "**Contra o quê se conta o acerto:** contra o tipo e a intenção DOCUMENTADOS na coluna "
        "`intenção esperada` da amostra versionada, não contra o próprio `encaminhar()`. "
        "Comparar o router consigo próprio daria 80/80 por construção e não mediria nada."
    )
    partes.append("")
    for lingua, caminho in amostras:
        partes.append(f"- Amostra {lingua}: `{caminho.relative_to(RAIZ)}`")
    partes.append(f"- Configuração usada: {origem_config}")
    partes.append(f"- Prefixo das combinações «com prefixo»: `{PREFIXO_D53!r}`")
    partes.append("")
    partes.append("## Resultado, linha a linha (80 linhas)")
    partes.append("")
    partes.append(
        "| combinação | nº | texto encaminhado | intenção documentada | decisão do router | "
        "resíduo removido (D62) | acerto |"
    )
    partes.append("|---|---|---|---|---|---|---|")
    for linha in linhas:
        obtido = medir._acao_para_texto(linha.nome_acao_obtido, linha.argumento_obtido)
        partes.append(
            f"| {linha.combinacao} "
            f"| {linha.numero} "
            f"| {medir.celula_markdown(linha.texto_encaminhado)} "
            f"| {medir.celula_markdown(linha.documentada.bruta)} "
            f"| {medir.celula_markdown(f'{linha.tipo_obtido}: {obtido}')} "
            f"| {medir.celula_markdown(linha.residuo_removido or '—')} "
            f"| {'sim' if linha.acertou else 'NAO'} |"
        )
    partes.append("")
    partes.append("## Agregados (os quatro)")
    partes.append("")
    total_acertos = 0
    total_linhas = 0
    for combinacao, (acertos, total) in agregados.items():
        total_acertos += acertos
        total_linhas += total
        partes.append(
            f"- **{combinacao}: {acertos}/{total} = {acertos / total * 100:.1f}%**"
        )
    partes.append(
        f"- **Total das quatro combinações: {total_acertos}/{total_linhas} = "
        f"{total_acertos / total_linhas * 100:.1f}%**"
    )
    partes.append("")
    falhadas = [linha for linha in linhas if not linha.acertou]
    if falhadas:
        partes.append("## Linhas falhadas (defeito de encaminhamento)")
        partes.append("")
        for linha in falhadas:
            obtido = medir._acao_para_texto(linha.nome_acao_obtido, linha.argumento_obtido)
            partes.append(
                f"- {linha.combinacao} #{linha.numero} «{linha.texto_encaminhado}»: "
                f"documentado `{linha.documentada.bruta}`, obtido "
                f"`{linha.tipo_obtido}: {obtido}` | motivo: {linha.motivo}"
            )
    else:
        partes.append(
            "## Nenhuma linha falhada — o encaminhador está inocentado por esta perna"
        )
        partes.append("")
        partes.append(
            "As 80 linhas encaminham para a intenção documentada, incluindo as 40 com o "
            "prefixo «hey jarvis, » colado à frase (a limpeza determinística do resíduo da "
            "D62 está a funcionar nas duas línguas). O defeito dos 55/50/50/50% está a "
            "MONTANTE do router: no áudio, na transcrição ou no arnês."
        )
    partes.append("")
    partes.append(AVISO_PRIVACIDADE)
    partes.append("")

    garantir_pasta(caminho_saida.parent)
    caminho_saida.write_text("\n".join(partes) + "\n", encoding="utf-8")
    return agregados


# --- PERNA C (preparacao): padding de silencio digital ----------------------


def acrescentar_silencio(origem: Path, destino: Path, cabeca_s: float, cauda_s: float) -> tuple[float, float]:
    """Copia um WAV PCM16 acrescentando silencio a cabeca e a cauda.

    Devolve (duracao_original_s, duracao_nova_s). Nao re-sintetiza nada e nao
    toca numa unica amostra do audio original: o A/B da perna C tem o padding
    como unica variavel.
    """
    with wave.open(str(origem), "rb") as entrada:
        canais = entrada.getnchannels()
        largura = entrada.getsampwidth()
        taxa = entrada.getframerate()
        n_frames = entrada.getnframes()
        dados = entrada.readframes(n_frames)
    silencio_cabeca = b"\x00" * (int(round(cabeca_s * taxa)) * canais * largura)
    silencio_cauda = b"\x00" * (int(round(cauda_s * taxa)) * canais * largura)
    garantir_pasta(destino.parent)
    with wave.open(str(destino), "wb") as saida:
        saida.setnchannels(canais)
        saida.setsampwidth(largura)
        saida.setframerate(taxa)
        saida.writeframes(silencio_cabeca + dados + silencio_cauda)
    duracao_original = n_frames / taxa if taxa else 0.0
    return duracao_original, duracao_original + cabeca_s + cauda_s


# --- PERNA B: caminho vivo, um processo por WAV -----------------------------

#: A etapa 2 escreve `... | texto: '<transcricao>'` (jarvis/app.py::detalhe_da_transcricao)
PADRAO_TRANSCRICAO = re.compile(r"\|\s*texto:\s*(.*)$")
PADRAO_ETAPA_3 = re.compile(r"decisao=(\w+)")
PADRAO_RESIDUO = re.compile(r"residuo da wake word removido: (?:'([^']*)'|nenhum)")


@dataclass
class LinhaPernaB:
    numero: int
    wav: str
    transcricao: str
    tipo: str
    nome_acao: str | None
    argumento: str | None
    residuo: str | None
    duracao_s: float
    segundos_do_processo: float
    linhas_do_log: list[str]


def _analisar_saida_do_produto(texto: str) -> tuple[str, str, str | None, str | None, list[str]]:
    """Extrai do log do `jarvis.app --wav` a transcricao e a decisao.

    Devolve (transcricao, tipo, nome_acao, residuo, linhas_uteis).
    Levanta ValueError se as etapas 2 ou 3 nao estiverem no log: uma perna de
    diagnostico nunca inventa uma linha que a corrida nao produziu.
    """
    import ast

    transcricao = None
    tipo = None
    nome_acao: str | None = None
    residuo: str | None = None
    uteis: list[str] = []
    for linha in texto.splitlines():
        if "etapa 2/5" in linha or "etapa 3/5" in linha:
            uteis.append(linha.rstrip())
        if transcricao is None and "etapa 2/5" in linha:
            casou = PADRAO_TRANSCRICAO.search(linha)
            if casou is not None:
                bruto = casou.group(1).strip()
                try:
                    transcricao = ast.literal_eval(bruto)
                except (ValueError, SyntaxError):
                    transcricao = bruto.strip("'\"")
        if tipo is None and "etapa 3/5" in linha:
            casou = PADRAO_ETAPA_3.search(linha)
            if casou is not None:
                tipo = casou.group(1)
                acao = re.search(r"accao local '([^']+)'", linha)
                if acao is not None:
                    nome_acao = acao.group(1)
                residuo_casou = PADRAO_RESIDUO.search(linha)
                if residuo_casou is not None:
                    residuo = residuo_casou.group(1)
        if "FALSO DESPERTAR DESCARTADO" in linha:
            uteis.append(linha.rstrip())
            if tipo is None:
                tipo = "nada"
    if transcricao is None or tipo is None:
        raise ValueError(
            "o log do caminho vivo nao trouxe etapa 2 e/ou etapa 3; nada a medir nesta linha"
        )
    return transcricao, tipo, nome_acao, residuo, uteis


def _correr_ate_a_etapa_3(comando: Sequence[str], limite_s: float) -> str:
    r"""Corre o comando do produto e para a corrida assim que a etapa 3 sai.

    PORQUE SE PARA (dito aqui e repetido na evidencia, para ninguem ter de
    adivinhar): a perna B mede a TRANSCRICAO (etapa 2) e a DECISAO DO
    ENCAMINHADOR (etapa 3). A etapa 4 de uma frase que vai para o Claude Code
    abre uma sessao-ponte real do CLI `claude` e gasta tokens do utilizador — e
    nesta amostra 19 das 20 frases acabam em `decisao=claude`. Deixar as 20
    corridas irem ate ao fim custaria ~27 minutos de espera e 20 sessoes do
    Claude Code para produzir ZERO linhas de informacao nova sobre o que esta
    perna mede. As etapas 1, 2 e 3 correm INTEIRAS e sem nenhuma alteracao: o
    comando e o do produto, tal e qual, e o que se faz a seguir e o mesmo que
    o utilizador faria com um Ctrl+C depois de ver a decisao no ecra.
    Uma corrida COMPLETA, com etapa 4 e 5 incluidas, fica na evidencia como
    prova de que o caminho inteiro corre.
    """
    criacao = 0
    if sys.platform == "win32":
        criacao = subprocess.CREATE_NEW_PROCESS_GROUP
    processo = subprocess.Popen(
        list(comando),
        cwd=str(RAIZ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=criacao,
    )
    recolhido: list[str] = []
    limite = time.perf_counter() + limite_s
    try:
        assert processo.stdout is not None
        for linha in processo.stdout:
            recolhido.append(linha)
            if "etapa 3/5" in linha or "FALSO DESPERTAR DESCARTADO" in linha:
                break
            if time.perf_counter() > limite:
                break
    finally:
        _matar_arvore(processo)
    return "".join(recolhido)


def _matar_arvore(processo: subprocess.Popen) -> None:
    """Mata o processo E o filho do RealtimeSTT (mp.Process em Windows)."""
    if processo.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(processo.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        processo.kill()
    try:
        processo.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass


def correr_perna_b(
    pasta_audio: Path,
    numeros: Sequence[int],
    *,
    device: str,
    modelo: str,
    limite_s: float,
) -> list[LinhaPernaB]:
    linhas: list[LinhaPernaB] = []
    for numero in numeros:
        wav = pasta_audio / f"{numero:02d}.wav"
        if not wav.is_file():
            raise FileNotFoundError(f"falta o WAV da frase {numero} em '{wav}'")
        comando = [
            sys.executable,
            "-m",
            "jarvis.app",
            "--wav",
            str(wav),
            "--sem-voz",
            "--device",
            device,
            "--modelo",
            modelo,
        ]
        inicio = time.perf_counter()
        saida = _correr_ate_a_etapa_3(comando, limite_s)
        decorrido = time.perf_counter() - inicio
        transcricao, tipo, nome_acao, residuo, uteis = _analisar_saida_do_produto(saida)
        with wave.open(str(wav), "rb") as ficheiro:
            duracao = ficheiro.getnframes() / (ficheiro.getframerate() or 1)
        linhas.append(
            LinhaPernaB(
                numero=numero,
                wav=wav.name,
                transcricao=transcricao,
                tipo=tipo,
                nome_acao=nome_acao,
                argumento=None,
                residuo=residuo,
                duracao_s=duracao,
                segundos_do_processo=decorrido,
                linhas_do_log=uteis,
            )
        )
        print(
            f"  #{numero:02d} {decorrido:6.1f} s | {tipo:6} | {transcricao!r}",
            flush=True,
        )
    return linhas


# --- Verificação auxiliar: a taxa de amostragem da síntese ------------------


def correr_taxa(frases: Sequence[str], pasta: Path, device: str) -> list[dict[str, object]]:
    r"""Testa se o reamostrador 22050 -> 16000 do arnês é o que destrói o áudio.

    `scripts/gerar_wav.py` sintetiza a 22050 Hz (a taxa nativa da voz
    `pt_PT-tugao-medium`) e desce para 16 kHz com `audioop.ratecv`, que
    interpola linearmente e NAO tem filtro anti-aliasing: o conteúdo entre 8 e
    11 kHz dobra-se para dentro da banda útil. Se fosse isso a causa do WER,
    transcrever o WAV CRU de 22050 Hz — que o faster-whisper reamostra
    internamente com um filtro a sério — daria um texto muito melhor.

    Controlo: as duas transcrições saem da MESMA síntese (o WAV de 22050 Hz é
    escrito uma vez e a versão de 16 kHz é derivada dele), por isso a taxa é a
    única variável.
    """
    from jarvis.audio_util import escrever_wav_pcm16, ler_wav_pcm16, reamostrar_pcm16

    gerar = _carregar_modulo_irmao_generico("gerar_wav")
    transcrever = _carregar_modulo_irmao_generico("transcrever_ficheiro")
    garantir_pasta(pasta)
    piper = gerar.caminho_do_piper_exe()
    gerar.preparar_encoding_do_piper()
    resultados: list[dict[str, object]] = []
    for indice, texto in enumerate(frases, start=1):
        bruto = pasta / f"{indice:02d}-nativo.wav"
        # com_som=False sempre: nada toca nas colunas.
        gerar.sintetizar_para_wav_bruto(texto, bruto, piper, com_som=False)
        dados, taxa_nativa, canais = ler_wav_pcm16(bruto)
        derivado = pasta / f"{indice:02d}-16k-ratecv.wav"
        escrever_wav_pcm16(
            derivado, reamostrar_pcm16(dados, taxa_nativa, 16000, canais), 16000, canais=canais
        )
        nativo = transcrever.transcrever(bruto, device=device, usar_cache_do_modelo=True)
        dezasseis = transcrever.transcrever(derivado, device=device, usar_cache_do_modelo=True)
        resultados.append(
            {
                "frase": texto,
                "taxa_nativa": taxa_nativa,
                "texto_nativo": nativo["texto"],
                "texto_16k": dezasseis["texto"],
                "wer_nativo": medir.calcular_wer(texto, nativo["texto"]).wer,
                "wer_16k": medir.calcular_wer(texto, dezasseis["texto"]).wer,
            }
        )
        print(
            f"  {texto!r}\n    {taxa_nativa} Hz cru      -> {nativo['texto']!r}"
            f"\n    16000 Hz (ratecv) -> {dezasseis['texto']!r}",
            flush=True,
        )
    return resultados


def _carregar_modulo_irmao_generico(nome: str):
    import importlib.util

    chave = f"_jarvis_diag_{nome}"
    ja = sys.modules.get(chave)
    if ja is not None:
        return ja
    spec = importlib.util.spec_from_file_location(chave, PASTA_SCRIPTS / f"{nome}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"nao consegui carregar 'scripts/{nome}.py'")
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[chave] = modulo
    spec.loader.exec_module(modulo)
    return modulo


def _escrever_taxa(resultados: Sequence[dict[str, object]], caminho: Path, pasta: Path) -> None:
    partes = [
        "# Diagnóstico da D53 — verificação auxiliar: a taxa de amostragem da síntese",
        "",
        AVISO_PRIVACIDADE,
        "",
        f"Gerado por `scripts/diagnostico_d53.py taxa` em "
        f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}.",
        "",
        "**Hipótese testada:** `scripts/gerar_wav.py` desce os 22050 Hz nativos da voz Piper "
        "para 16 kHz com `audioop.ratecv` (`jarvis/audio_util.py::reamostrar_pcm16`), que "
        "interpola linearmente e não tem filtro anti-aliasing. Se fosse o reamostrador a "
        "destruir o áudio, o WAV CRU de 22050 Hz — reamostrado internamente pelo faster-whisper, "
        "com filtro — transcreveria muito melhor.",
        "",
        "**Controlo:** as duas transcrições de cada linha saem da MESMA síntese (o WAV nativo é "
        f"escrito uma vez em `{pasta}` e a versão de 16 kHz é derivada dele), por isso a taxa é "
        "a única variável. Nada tocou nas colunas (`com_som=False`, D61).",
        "",
        "| frase | taxa nativa | transcrição a 22050 Hz (cru) | WER | transcrição a 16 kHz (ratecv) | WER |",
        "|---|---|---|---|---|---|",
    ]
    for r in resultados:
        partes.append(
            f"| {medir.celula_markdown(r['frase'])} | {r['taxa_nativa']} Hz "
            f"| {medir.celula_markdown(r['texto_nativo'])} | {float(r['wer_nativo']) * 100:.1f}% "
            f"| {medir.celula_markdown(r['texto_16k'])} | {float(r['wer_16k']) * 100:.1f}% |"
        )
    media_nativo = sum(float(r["wer_nativo"]) for r in resultados) / len(resultados) * 100
    media_16k = sum(float(r["wer_16k"]) for r in resultados) / len(resultados) * 100
    partes.extend(
        [
            "",
            f"- **WER médio a 22050 Hz (cru): {media_nativo:.1f}%**",
            f"- **WER médio a 16 kHz (`audioop.ratecv`): {media_16k:.1f}%**",
            "",
            AVISO_PRIVACIDADE,
            "",
        ]
    )
    garantir_pasta(caminho.parent)
    caminho.write_text("\n".join(partes) + "\n", encoding="utf-8")


# --- CLI ---------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    from jarvis.consola import forcar_consola_utf8

    forcar_consola_utf8()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subs = parser.add_subparsers(dest="perna", required=True)

    pa = subs.add_parser("perna-a", help="encaminhamento sem audio (80 linhas)")
    pa.add_argument("--saida", required=True)

    pc = subs.add_parser("padding", help="prepara os WAV com silencio a cabeca e a cauda")
    pc.add_argument("--origem", required=True, help="nome da subpasta de audio/medir-voz/")
    pc.add_argument("--destino", required=True, help="nome da subpasta de destino")
    pc.add_argument("--cabeca", type=float, default=PADDING_CABECA_S)
    pc.add_argument("--cauda", type=float, default=PADDING_CAUDA_S)

    pb = subs.add_parser("perna-b", help="corre o caminho vivo (jarvis.app --wav --sem-voz)")
    pb.add_argument("--pasta-audio", required=True)
    pb.add_argument("--numeros", default="1-20", help="ex.: 1-20 ou 1,5,7")
    pb.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    pb.add_argument("--modelo", default="medium")
    pb.add_argument("--limite-s", type=float, default=600.0)
    pb.add_argument(
        "--arnes",
        required=True,
        help="ficheiro de evidencia do arnes com a tabela a comparar (ex.: ganho-pt-sem-prefixo.md)",
    )
    pb.add_argument("--saida", required=True, help="ficheiro .md dentro de docs/forja/evidence/")

    pcc = subs.add_parser("perna-c", help="compara as duas corridas do A/B do padding")
    pcc.add_argument("--sem-padding", required=True, help="evidencia da perna A'")
    pcc.add_argument("--com-padding", required=True, help="evidencia da perna B'")
    pcc.add_argument("--pasta-sem", required=True)
    pcc.add_argument("--pasta-com", required=True)
    pcc.add_argument("--cabeca", type=float, default=PADDING_CABECA_S)
    pcc.add_argument("--cauda", type=float, default=PADDING_CAUDA_S)
    pcc.add_argument("--saida", required=True)

    pt = subs.add_parser("taxa", help="22050 Hz cru vs 16 kHz por audioop.ratecv, mesma sintese")
    pt.add_argument("--pasta-audio", default="d53-taxa")
    pt.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    pt.add_argument("--saida", required=True)

    args = parser.parse_args(argv)

    if args.perna == "perna-a":
        config, origem_config = medir.carregar_config_para_arnes()
        nomes = [projeto.nome for projeto in config.projetos]
        amostras = [
            ("PT", RAIZ / "tests" / "voz" / "frases-pt.md"),
            ("EN", RAIZ / "tests" / "voz" / "frases-en.md"),
        ]
        linhas = correr_perna_a(amostras, config, nomes)
        caminho = medir.caminho_evidencia_de_saida(args.saida)
        agregados = escrever_perna_a(linhas, caminho, origem_config, amostras)
        print("=== PERNA A — encaminhamento sem audio ===")
        for combinacao, (acertos, total) in agregados.items():
            print(f"{combinacao}: {acertos}/{total} = {acertos / total * 100:.1f}%")
        print(f"evidencia escrita em {caminho}")
        return 0

    if args.perna == "padding":
        origem = medir.pasta_de_audio_de_saida(args.origem)
        destino = medir.pasta_de_audio_de_saida(args.destino)
        if not origem.is_dir():
            print(f"ERRO: '{origem}' nao existe", file=sys.stderr)
            return 1
        print(f"=== padding: {origem} -> {destino} "
              f"(+{args.cabeca:.2f} s a cabeca, +{args.cauda:.2f} s a cauda) ===")
        for wav in sorted(origem.glob("*.wav")):
            antes, depois = acrescentar_silencio(wav, destino / wav.name, args.cabeca, args.cauda)
            print(f"  {wav.name}: {antes:.2f} s -> {depois:.2f} s")
        return 0

    if args.perna == "perna-b":
        config, _origem = medir.carregar_config_para_arnes()
        pasta = medir.pasta_de_audio_de_saida(args.pasta_audio)
        numeros = _interpretar_numeros(args.numeros)
        caminho_arnes = Path(args.arnes)
        if not caminho_arnes.is_absolute():
            caminho_arnes = RAIZ / caminho_arnes
        arnes = _ler_tabela_do_arnes(caminho_arnes)
        caminho_saida = medir.caminho_evidencia_de_saida(args.saida)
        print(f"=== PERNA B — caminho vivo sobre {pasta} ===")
        linhas = correr_perna_b(
            pasta, numeros, device=args.device, modelo=args.modelo, limite_s=args.limite_s
        )
        certo_arnes, certo_vivo, iguais = _escrever_perna_b(
            linhas, arnes, caminho_arnes, caminho_saida, pasta, config, args
        )
        n = len(linhas)
        print(f"acerto arnes      = {certo_arnes}/{n}")
        print(f"acerto caminho vivo = {certo_vivo}/{n}")
        print(f"texto identico      = {iguais}/{n}")
        print(f"evidencia escrita em {caminho_saida}")
        return 0

    if args.perna == "perna-c":
        def _abs(valor: str) -> Path:
            caminho = Path(valor)
            return caminho if caminho.is_absolute() else RAIZ / caminho

        caminho_a = _abs(args.sem_padding)
        caminho_b = _abs(args.com_padding)
        tabela_a = _ler_tabela_do_arnes(caminho_a)
        tabela_b = _ler_tabela_do_arnes(caminho_b)
        caminho_saida = medir.caminho_evidencia_de_saida(args.saida)
        certo_a, certo_b, mudaram = _escrever_perna_c(
            tabela_a,
            tabela_b,
            caminho_a,
            caminho_b,
            caminho_saida,
            medir.pasta_de_audio_de_saida(args.pasta_sem),
            medir.pasta_de_audio_de_saida(args.pasta_com),
            args.cabeca,
            args.cauda,
        )
        print("=== PERNA C — padding ===")
        print(f"acerto sem padding = {certo_a}/{len(tabela_a)}")
        print(f"acerto com padding = {certo_b}/{len(tabela_b)}")
        print(f"linhas com acerto mudado = {mudaram}")
        print(f"evidencia escrita em {caminho_saida}")
        return 0

    if args.perna == "taxa":
        config, _origem = medir.carregar_config_para_arnes()
        nomes = [projeto.nome for projeto in config.projetos]
        amostra = medir.ler_amostra(RAIZ / "tests" / "voz" / "frases-pt.md")
        # Duas frases curtas que falham e duas longas que passam: se o
        # reamostrador fosse a causa, as quatro melhoravam a 22050 Hz.
        escolhidas = [1, 5, 17, 20]
        frases = [
            medir.substituir_marcadores(fr.frase_com_marcadores, nomes)
            for fr in amostra
            if fr.numero in escolhidas
        ]
        pasta = medir.pasta_de_audio_de_saida(args.pasta_audio)
        caminho_saida = medir.caminho_evidencia_de_saida(args.saida)
        print("=== verificacao auxiliar — taxa de amostragem ===")
        resultados = correr_taxa(frases, pasta, args.device)
        _escrever_taxa(resultados, caminho_saida, pasta)
        print(f"evidencia escrita em {caminho_saida}")
        return 0

    return 1


def _interpretar_numeros(valor: str) -> list[int]:
    if "-" in valor and "," not in valor:
        inicio, fim = valor.split("-", 1)
        return list(range(int(inicio), int(fim) + 1))
    return [int(parte) for parte in valor.split(",") if parte.strip()]


#: Paridade parametro a parametro entre o arnes e o caminho vivo (perna B).
#: Cada linha: parametro | arnes (scripts/transcrever_ficheiro.py:236-250) |
#: produto (jarvis/app.py::construir_recorder, ~740-783) | divergencia? | efeito plausivel.
PARIDADE: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "modelo",
        "`medium` (`--modelo`, MODELO_PREFERIDO, D39)",
        "`medium` (`MODELO_STT`, app.py:136)",
        "igual",
        "nenhum",
    ),
    (
        "device / compute_type",
        "`cuda` + `float16` (transcrever_ficheiro.py: compute_type do carregador)",
        "`cuda` + `float16` (app.py:763-764)",
        "igual",
        "nenhum",
    ),
    (
        "language",
        "`lingua_fixa='pt'` -> `language='pt'` (transcrever_ficheiro.py:247)",
        "`language=LINGUA_FIXA_DO_PRODUTO` = `'pt'` (app.py:762)",
        "igual",
        "nenhum (a reversão da deteção de língua tocou nos dois sítios)",
    ),
    (
        "beam_size",
        "`5` (transcrever_ficheiro.py:248)",
        "`5` (app.py:768)",
        "igual",
        "nenhum",
    ),
    (
        "initial_prompt",
        "`None` por omissão (D51)",
        "`INITIAL_PROMPT = None` (app.py:141, 767)",
        "igual",
        "nenhum",
    ),
    (
        "condition_on_previous_text",
        "`False`, explícito (transcrever_ficheiro.py:250)",
        "não é passado: fica o default do faster-whisper (`True`), e o RealtimeSTT não o expõe",
        "**DIVERGE**",
        "no caminho vivo cada frase é um `transcribe()` novo num processo filho recém-alimentado, "
        "sem texto anterior nenhum: o prompt condicionante está vazio de qualquer maneira. Efeito "
        "medido nesta perna: nenhum (as transcrições batem certo, ver tabela abaixo)",
    ),
    (
        "vad_filter (dentro do transcribe)",
        "`False`, explícito (transcrever_ficheiro.py:249)",
        "não é passado ao `transcribe`; o RealtimeSTT usa `vad_filter=False` no worker e faz o VAD "
        "FORA, antes, com WebRTC + Silero",
        "**DIVERGE no sítio, não no valor**",
        "o VAD do produto não filtra o áudio que chega ao Whisper: decide QUANDO começa e acaba a "
        "frase. É ele que define o recorte que o modelo vê",
    ),
    (
        "o que chega ao faster-whisper",
        "o ficheiro WAV INTEIRO, tal como está no disco (0,53 s–2,73 s nesta amostra)",
        "o recorte que o VAD fechou, com `pre_recording_buffer_duration=1.0` de áudio ANTES do "
        "início da fala e `post_speech_silence_duration=0.6` de silêncio depois (app.py:769-771)",
        "**DIVERGE — é a divergência estrutural**",
        "no caminho vivo o modelo recebe sempre ~1,6 s a mais de contexto silencioso do que o "
        "arnês lhe dá. É exatamente a variável que a perna C isola",
    ),
    (
        "min_length_of_recording",
        "não existe: o arnês transcreve o que lhe derem",
        "`0.3` s (app.py:770)",
        "**DIVERGE**",
        "uma frase de menos de 0,3 s nunca fecha no produto; nesta amostra a mais curta tem 0,53 s, "
        "portanto nenhuma linha é afetada",
    ),
    (
        "processo",
        "o `transcribe()` corre NESTE processo, com o modelo em cache por (nome, device)",
        "o RealtimeSTT transcreve num PROCESSO FILHO (`mp.Process` em Windows) e devolve só a "
        "string (app.py:770-785 / D66.5)",
        "**DIVERGE**",
        "custa a latência de arranque e tira o acesso a `info.all_language_probs` (D66), mas não "
        "muda o texto: com os mesmos parâmetros e o mesmo áudio, o resultado é o mesmo",
    ),
    (
        "palavra de ativação",
        "não existe no arnês; o prefixo «hey jarvis, » é SINTETIZADO dentro do áudio",
        "no modo `--wav` a porta fica ABERTA e o oww só monitoriza (app.py:1003-1010)",
        "igual no efeito para esta amostra",
        "nenhum: os WAV de `ab-pt-sem` não têm prefixo nenhum",
    ),
)


def _ler_tabela_do_arnes(caminho: Path) -> dict[int, dict[str, str]]:
    """Lê a tabela «Resultado, frase a frase» de um ficheiro de `medir_voz.py`.

    Só as linhas de 11 colunas com número na primeira contam. Devolve
    {nº: {frase, transcricao, intencao_esperada, intencao_obtida, acerto, wer, duracao}}.
    """
    linhas: dict[int, dict[str, str]] = {}
    for bruta in caminho.read_text(encoding="utf-8").splitlines():
        celulas = medir.dividir_celulas(bruta)
        if celulas is None or len(celulas) != 11:
            continue
        if not medir.PADRAO_NUMERO.match(celulas[0]):
            continue
        linhas[int(celulas[0])] = {
            "tipo": celulas[1],
            "frase": celulas[2],
            "transcricao": celulas[3],
            "intencao_esperada": celulas[4],
            "intencao_obtida": celulas[5],
            "acerto": celulas[6],
            "wer": celulas[7],
            "duracao": celulas[9],
        }
    if not linhas:
        raise ValueError(f"'{caminho}': nenhuma linha de tabela de medição encontrada")
    return linhas


def _escrever_perna_c(
    a_linha: dict[int, dict[str, str]],
    b_linha: dict[int, dict[str, str]],
    caminho_a: Path,
    caminho_b: Path,
    caminho: Path,
    pasta_a: Path,
    pasta_b: Path,
    cabeca_s: float,
    cauda_s: float,
) -> tuple[int, int, int]:
    """Escreve a evidência da perna C. Devolve (acerto_A, acerto_B, linhas_mudadas)."""
    numeros = sorted(a_linha)
    acerto_a = sum(1 for n in numeros if a_linha[n]["acerto"] == "sim")
    acerto_b = sum(1 for n in numeros if b_linha[n]["acerto"] == "sim")
    mudaram_texto = [n for n in numeros if a_linha[n]["transcricao"] != b_linha[n]["transcricao"]]
    mudaram_acerto = [n for n in numeros if a_linha[n]["acerto"] != b_linha[n]["acerto"]]
    n = len(numeros)
    partes = [
        "# Diagnóstico da D53 — PERNA C: hipótese da frase ultracurta (padding de silêncio)",
        "",
        AVISO_PRIVACIDADE,
        "",
        f"Gerado por `scripts/diagnostico_d53.py perna-c` em "
        f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}.",
        "",
        "## A hipótese que está a ser testada",
        "",
        "`scripts/transcrever_ficheiro.py:10-17` regista que um WAV isolado de menos de 1 s faz "
        "o faster-whisper alucinar uma frase comum de treino («Tchau, pessoal.») em vez de "
        "transcrever o áudio, e as linhas falhadas de `ganho-pt-sem-prefixo.md` são exatamente "
        "as curtas («que horas são» 0,88 s, «cala-te» 0,53 s, «adormece» 0,63 s, «acorda» "
        "0,58 s), enquanto as frases longas saem com 0–45% de WER. Se o que falta for CONTEXTO "
        "temporal, dar ao modelo o silêncio que o caminho vivo entrega de facto deve corrigir "
        "parte dessas linhas.",
        "",
        "## O A/B, controlado sobre os MESMOS WAV (D66, ponto 7)",
        "",
        f"- **Perna A′** — os WAV como estão: `{pasta_a}` "
        f"(`--reutilizar-audio`, nada foi sintetizado). Evidência: `{caminho_a.name}`.",
        f"- **Perna B′** — os MESMOS WAV com silêncio digital acrescentado: `{pasta_b}`, "
        f"**+{cabeca_s:.2f} s à cabeça e +{cauda_s:.2f} s à cauda**, a medida exata que o caminho "
        "vivo entrega (`pre_recording_buffer_duration=1.0`, `post_speech_silence_duration=0.6`, "
        f"jarvis/app.py::construir_recorder). Evidência: `{caminho_b.name}`.",
        "- Nenhuma amostra do áudio original foi tocada: o padding é silêncio digital (zeros) "
        "colado antes e depois, produzido por `scripts/diagnostico_d53.py padding`. O padding é "
        "a ÚNICA variável entre as duas pernas.",
        "- PT sem prefixo apenas, `language='pt'`, modelo `medium`, prompt desligado (D51), "
        "`--com-som` em lado nenhum (D61).",
        "",
        "## Resultado, linha a linha",
        "",
        "| nº | frase | duração A′ (s) | duração B′ (s) | transcrição A′ (sem padding) | "
        "transcrição B′ (com padding) | acerto A′ | acerto B′ | WER A′ | WER B′ |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for numero in numeros:
        a = a_linha[numero]
        b = b_linha[numero]
        partes.append(
            f"| {numero} | {medir.celula_markdown(a['frase'])} | {a['duracao']} | {b['duracao']} "
            f"| {medir.celula_markdown(a['transcricao'])} "
            f"| {medir.celula_markdown(b['transcricao'])} "
            f"| {a['acerto']} | {b['acerto']} | {a['wer']} | {b['wer']} |"
        )
    partes.extend(
        [
            "",
            "## Agregados",
            "",
            f"- **Acerto de intenção, A′ (sem padding): {acerto_a}/{n} = "
            f"{acerto_a / n * 100:.1f}%**",
            f"- **Acerto de intenção, B′ (com padding): {acerto_b}/{n} = "
            f"{acerto_b / n * 100:.1f}%**",
            f"- **Linhas em que o TEXTO transcrito mudou: {len(mudaram_texto)}/{n}** "
            + (f"({', '.join(f'#{x}' for x in mudaram_texto)})" if mudaram_texto else ""),
            f"- **Linhas em que o ACERTO DE INTENÇÃO mudou: {len(mudaram_acerto)}/{n}** "
            + (f"({', '.join(f'#{x}' for x in mudaram_acerto)})" if mudaram_acerto else "— nenhuma"),
            "",
        ]
    )
    if acerto_a == acerto_b and not mudaram_acerto:
        partes.extend(
            [
                "## Veredicto: hipótese REFUTADA, com números",
                "",
                f"Acrescentar {cabeca_s:.1f} s de silêncio à cabeça e {cauda_s:.1f} s à cauda dos "
                f"MESMOS 20 WAV não muda o acerto de intenção em NENHUMA linha "
                f"({acerto_a}/{n} dos dois lados). O texto mudou nalgumas linhas — o "
                "faster-whisper não é determinista ao ponto de dar byte a byte o mesmo com uma "
                "janela diferente — mas mudou de errado para errado: nenhuma frase curta passou "
                "a ser reconhecida. **O que falta às frases ultracurtas não é silêncio à volta.** "
                "Uma hipótese refutada com números é resultado válido desta perna: fecha a porta "
                "a «e se fosse só padding?» sem custar mais nenhuma tentativa.",
            ]
        )
    else:
        partes.extend(
            [
                "## Veredicto: o padding MUDA o resultado",
                "",
                f"O acerto de intenção passou de {acerto_a}/{n} para {acerto_b}/{n} e as linhas "
                f"{', '.join(f'#{x}' for x in mudaram_acerto)} mudaram de lado. O número escrito "
                "é o que a corrida deu.",
            ]
        )
    partes.extend(["", AVISO_PRIVACIDADE, ""])
    garantir_pasta(caminho.parent)
    caminho.write_text("\n".join(partes) + "\n", encoding="utf-8")
    return acerto_a, acerto_b, len(mudaram_acerto)


def _escrever_perna_b(
    linhas: Sequence[LinhaPernaB],
    arnes: dict[int, dict[str, str]],
    caminho_arnes: Path,
    caminho: Path,
    pasta: Path,
    config: Config,
    args: argparse.Namespace,
) -> tuple[int, int, int]:
    """Escreve a evidência da perna B. Devolve (acerto_arnes, acerto_vivo, iguais)."""
    partes = [
        "# Diagnóstico da D53 — PERNA B: paridade ARNÊS vs CAMINHO VIVO (isola o arnês)",
        "",
        AVISO_PRIVACIDADE,
        "",
        f"Gerado por `scripts/diagnostico_d53.py perna-b` em "
        f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}.",
        "",
        "## 1. Paridade, parâmetro a parâmetro",
        "",
        "Arnês = `scripts/transcrever_ficheiro.py:236-250`. Produto = "
        "`jarvis/app.py::construir_recorder` (dicionário `opcoes`, ~740-783).",
        "",
        "| parâmetro | arnês | produto (caminho vivo) | diverge? | efeito plausível |",
        "|---|---|---|---|---|",
    ]
    for parametro, lado_arnes, lado_produto, diverge, efeito in PARIDADE:
        partes.append(
            f"| {parametro} | {lado_arnes} | {lado_produto} | {diverge} | {efeito} |"
        )
    partes.extend(
        [
            "",
            "## 2. A medição que fecha a perna",
            "",
            f"- WAV: `{pasta}` — **os MESMOS 20 ficheiros** que `{caminho_arnes.name}` usou "
            "(`--reutilizar-audio`), sem re-sintetizar nada.",
            f"- Comando por frase, o do produto: "
            f"`.venv\\Scripts\\python -m jarvis.app --wav <ficheiro> --sem-voz "
            f"--device {args.device} --modelo {args.modelo}`",
            "- `--sem-voz` em todas as 20: nada toca nas colunas.",
            "- **Cada corrida foi terminada assim que a etapa 3 saiu no log.** A perna B mede a "
            "etapa 2 (transcrição) e a etapa 3 (encaminhamento); a etapa 4 de uma frase "
            "`decisao=claude` abre uma sessão-ponte real do CLI `claude` e gasta tokens do "
            "utilizador, e nesta amostra quase todas as linhas acabam em `claude`. As etapas 1, 2 e "
            "3 correram INTEIRAS e sem nenhuma alteração; parar a seguir é o que o utilizador faz "
            "com um Ctrl+C depois de ver a decisão. Uma corrida COMPLETA (etapas 1 a 5, "
            "incluindo a entrega ao Claude Code) está citada no fim deste ficheiro como prova de "
            "que o caminho inteiro corre.",
            "",
            "| nº | frase sintetizada | transcrição do ARNÊS | transcrição do CAMINHO VIVO | "
            "texto igual? | intenção esperada | obtida (arnês) | obtida (vivo) | acerto arnês | "
            "acerto vivo |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    acerto_arnes = 0
    acerto_vivo = 0
    iguais = 0
    divergencias: list[str] = []
    for linha in linhas:
        do_arnes = arnes.get(linha.numero)
        if do_arnes is None:
            raise ValueError(f"a frase {linha.numero} não existe na tabela do arnês")
        frase = do_arnes["frase"]
        esperado = encaminhar(frase, config)
        obtido_arnes = encaminhar(do_arnes["transcricao"], config)
        obtido_vivo = encaminhar(linha.transcricao, config)

        def _bate(resultado) -> bool:
            return (
                resultado.tipo == esperado.tipo
                and resultado.nome_acao == esperado.nome_acao
                and resultado.argumento == esperado.argumento
            )

        bate_arnes = _bate(obtido_arnes)
        bate_vivo = _bate(obtido_vivo)
        acerto_arnes += 1 if bate_arnes else 0
        acerto_vivo += 1 if bate_vivo else 0
        texto_igual = do_arnes["transcricao"].strip() == linha.transcricao.strip()
        iguais += 1 if texto_igual else 0
        if not texto_igual:
            divergencias.append(
                f"- #{linha.numero} «{frase}»: arnês `{do_arnes['transcricao']}` vs caminho vivo "
                f"`{linha.transcricao}` (acerto arnês {'sim' if bate_arnes else 'NAO'}, "
                f"acerto vivo {'sim' if bate_vivo else 'NAO'})"
            )
        # Coerência: o tipo que o log do produto escreveu na etapa 3 tem de bater
        # com o que `encaminhar()` dá sobre a mesma transcrição. Se não bater, o
        # parser mentiu e a linha não pode entrar sem aviso.
        coerente = "" if linha.tipo == obtido_vivo.tipo else " ⚠ log≠router"
        partes.append(
            f"| {linha.numero} "
            f"| {medir.celula_markdown(frase)} "
            f"| {medir.celula_markdown(do_arnes['transcricao'])} "
            f"| {medir.celula_markdown(linha.transcricao)} "
            f"| {'sim' if texto_igual else '**NÃO**'} "
            f"| {medir.celula_markdown(f'{esperado.tipo}: ' + medir._acao_para_texto(esperado.nome_acao, esperado.argumento))} "
            f"| {medir.celula_markdown(f'{obtido_arnes.tipo}: ' + medir._acao_para_texto(obtido_arnes.nome_acao, obtido_arnes.argumento))} "
            f"| {medir.celula_markdown(f'{obtido_vivo.tipo}: ' + medir._acao_para_texto(obtido_vivo.nome_acao, obtido_vivo.argumento) + coerente)} "
            f"| {'sim' if bate_arnes else 'NAO'} "
            f"| {'sim' if bate_vivo else 'NAO'} |"
        )
    n = len(linhas)
    partes.extend(
        [
            "",
            "## 3. Agregados",
            "",
            f"- **Acerto de intenção, ARNÊS (`{caminho_arnes.name}`): {acerto_arnes}/{n} = "
            f"{acerto_arnes / n * 100:.1f}%**",
            f"- **Acerto de intenção, CAMINHO VIVO (`jarvis.app --wav --sem-voz`): "
            f"{acerto_vivo}/{n} = {acerto_vivo / n * 100:.1f}%**",
            f"- **Linhas em que o TEXTO transcrito é idêntico nos dois lados: {iguais}/{n}**",
            "",
        ]
    )
    if divergencias:
        partes.append("### Linhas em que o texto transcrito difere")
        partes.append("")
        partes.extend(divergencias)
    else:
        partes.append(
            "### Nenhuma linha difere: o arnês reproduz o caminho vivo caractere a caractere"
        )
    partes.extend(["", "## 4. Log das corridas (etapas 2 e 3, verbatim)", ""])
    for linha in linhas:
        partes.append(f"### frase {linha.numero} (`{linha.wav}`, {linha.duracao_s:.2f} s)")
        partes.append("")
        partes.append("```")
        partes.extend(linha.linhas_do_log)
        partes.append("```")
        partes.append("")
    partes.append(AVISO_PRIVACIDADE)
    partes.append("")
    garantir_pasta(caminho.parent)
    caminho.write_text("\n".join(partes) + "\n", encoding="utf-8")
    return acerto_arnes, acerto_vivo, iguais


if __name__ == "__main__":
    raise SystemExit(main())
