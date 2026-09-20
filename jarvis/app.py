r"""Orquestrador do jarvis: ouve, transcreve, encaminha, age e responde.

O processo de consola da D11/D30: arranca-se a mao, nao tem interface grafica,
nao e servico do Windows, nao arranca com o sistema, e fechar a janela desliga
o microfone E cala a voz (D60: Ctrl+C, saida do processo e o comando "cala-te"
passam todos pelo mesmo `jarvis.voz.calar_agora`, que mata a sintese em curso
antes de parar a reproducao, e depois disso nada novo e falado).

A consola deixa obvio a olho quando esta a ouvir (o cabecalho e a linha de
estado) e e, ao mesmo tempo, a prova: cada frase escreve um unico registo com
timestamps e latencias, na consola E em `logs/jarvis-<data>.log`.

Dois modos, o MESMO pipeline (TECHNOLOGY.md S1/S5):

  (a) microfone ao vivo — `AudioToTextRecorder` com `use_microphone=True`,
      `wakeword_backend="oww"` e o modelo pre-treinado `hey_jarvis`, VAD do
      RealtimeSTT e transcricao faster-whisper no GPU;

  (b) `--wav <ficheiro>` — o mesmo `AudioToTextRecorder`, com
      `use_microphone=False`, alimentado por `feed_audio()` com os frames do
      WAV ao ritmo real (D33: prova sem microfone humano).

AS CINCO ETAPAS, o que cada uma mede e de onde vem o tempo:

  1/5 palavra de ativacao — openWakeWord (modelo `hey_jarvis`, S2). Ao vivo e
      a porta do RealtimeSTT (`on_wakeword_detected`). No modo ficheiro ver
      "PORTA DA PALAVRA DE ATIVACAO" mais abaixo.
  2/5 transcricao — faster-whisper `medium` (`--modelo` troca-o), medida do FIM
      DA FALA (VAD a fechar a frase) ate o texto existir, que e a latencia que
      o Sponsor sente (D6).
  3/5 encaminhamento — `jarvis.router.encaminhar` (T4), determinista (D9).
  4/5 accao local (T5) ou entrega ao Claude Code pelo canal da T2 (D49).
  5/5 resposta falada — `jarvis.voz.falar` (T5, Piper pt-PT), com o recuo
      documentado para texto na consola se a voz falhar (D35.4).

Mais a latencia TOTAL e, sempre que uma frase e descartada sem accao, uma
linha explicita `FALSO DESPERTAR DESCARTADO` (D5/D6): um falso despertar nunca
executa uma accao nem gasta um token.

PORTA DA PALAVRA DE ATIVACAO NO MODO FICHEIRO. O backend `oww` deteta mesmo a
partir de ficheiro — esta provado neste repositorio com um WAV que comeca por
"hey jarvis" (`--exigir-wake-word`). Mas os WAV de prova da T3
(`audio/t-horas.wav`, `audio/t-ruido.wav`) nao trazem palavra de ativacao
nenhuma, por isso o modo `--wav` corre por omissao com a porta ABERTA e injeta
a partir da etapa 2, exatamente como o criterio da T6 previu. Nao se finge a
etapa 1: o MESMO modelo openWakeWord corre sobre os MESMOS frames em paralelo
(`MonitorWakeWord`) e a etapa 1 regista o que ele mediu de facto — o instante
da deteccao, ou o score maximo que o ficheiro atingiu e a razao de nao ter
disparado. Com `--exigir-wake-word` a porta fecha-se e o pipeline so transcreve
depois de a palavra de ativacao disparar, como ao vivo.

O QUE ESTE PROCESSO NUNCA FAZ: nao liga o microfone sem o Sponsor o arrancar
(D30); nao poe texto vindo do microfone, de ficheiro ou da configuracao numa
linha de comandos do Windows (D48.2 — as accoes locais recebem caminhos ja
resolvidos da config e a frase vai ao Claude Code por stdin em JSON); nao liga
o `initial_prompt` da transcricao, que fica DESLIGADO no caminho vivo e cujo
estado aparece no log de cada frase (D51); e nao envia nada para fora do PC
alem da frase que o router mandar ao Claude Code pelo canal ja decidido (D49).

Uso:

    .venv\Scripts\python -m jarvis.app                          # microfone
    .venv\Scripts\python -m jarvis.app --wav audio/t-horas.wav  # ficheiro
    .venv\Scripts\python -m jarvis.app --wav audio/x.wav --exigir-wake-word
    .venv\Scripts\python -m jarvis.app --prova-wake-word audio/x.wav
    .venv\Scripts\python -m jarvis.app --autoteste
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import datetime
import logging
import multiprocessing
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from jarvis import acoes_locais, canal_claude, voz
from jarvis.audio_util import (
    PASTA_MODELOS_FASTER_WHISPER,
    RAIZ,
    garantir_pasta,
    ler_wav_pcm16,
    reamostrar_pcm16,
    registar_dlls_do_torch,
)
from jarvis.config import CAMINHO_CONFIG_PADRAO, Config, ConfigError, carregar_config
from jarvis.consola import forcar_consola_utf8
from jarvis.lingua import (
    LINGUA_FIXA_DO_PRODUTO,
    decidir_lingua_do_top1,
    lingua_fixada,
)
from jarvis.resposta_falada import (
    FRASE_RECURSO_SEM_CORTE_SEGURO,
    MAXIMO_ABSOLUTO_FALADO,
    MAXIMO_CARACTERES_FALADOS,
    PREFIXO_DA_RESPOSTA_DO_CLAUDE,
    cortar_no_limite,
    resumo_falado,
)
from jarvis.router import ResultadoRouter, encaminhar

# --- Constantes do envelope (nada configuravel por texto vindo de fora) -----

#: Pasta dos logs. Ja esta no .gitignore (`logs/`): nenhuma transcricao entra
#: no repositorio publico (D1/D10).
PASTA_LOGS = RAIZ / "logs"

#: Modelos do openWakeWord descarregados para models/ (D14e, ver docs/MODELOS.md).
PASTA_MODELOS_OWW = RAIZ / "models" / "openwakeword"
MODELO_WAKE_WORD = PASTA_MODELOS_OWW / "hey_jarvis_v0.1.onnx"
MODELO_MELSPEC = PASTA_MODELOS_OWW / "melspectrogram.onnx"
MODELO_EMBEDDING = PASTA_MODELOS_OWW / "embedding_model.onnx"

#: Limiar do openWakeWord. O mesmo valor e dado ao RealtimeSTT
#: (`wake_words_sensitivity`) e ao monitor, para os dois caminhos decidirem
#: pelo mesmo numero.
SENSIBILIDADE_WAKE_WORD = 0.6

#: Transcricao (D39/S3): `medium` por omissao. A lista e FECHADA pela mesma
#: razao de scripts/transcrever_ficheiro.py: sem ela, `--modelo alguem/repo-mau`
#: mandava o faster-whisper descarregar pesos arbitrarios do Hugging Face para
#: models/, fora do registo da D14e.
#:
#: `large-v3-turbo` entrou na lista na T9 (S8/D65) para poder ser MEDIDO contra
#: o `medium` nas quatro combinacoes da D53. Entrar na lista fechada nao e
#: adocao: o default continua a ser decidido pelos numeros (ver docs/MODELOS.md).
MODELO_STT = "medium"
MODELOS_STT_PERMITIDOS = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]

#: D51: o initial_prompt fica DESLIGADO no caminho vivo e o estado do prompt
#: aparece no log de cada frase. Mesmos rotulos de scripts/transcrever_ficheiro.py.
INITIAL_PROMPT = None
ESTADO_PROMPT_DESLIGADO = "desligado"

TAXA_AMOSTRAGEM = 16_000
#: 512 amostras por chunk = 1024 bytes, o `buffer_size` do RealtimeSTT.
BYTES_POR_CHUNK = 1024
#: Cauda de silencio injetada depois do ficheiro, para o VAD fechar a frase.
SILENCIO_DEPOIS_DO_FICHEIRO_S = 1.5
#: Tempo que o modo ficheiro ainda espera pela transcricao depois de alimentar
#: tudo. Esgotado, a frase e abortada e o log escreve o falso despertar.
ESPERA_MAXIMA_DEPOIS_DO_FICHEIRO_S = 8.0
# O contrato do que chega a voz (limites, prefixo de origem, filtro por exclusao
# e frases de recurso) vive em jarvis/resposta_falada.py (D48.4/D59): a sessao
# filha do Claude Code corre sem ferramentas e pode alucinar que as usou, por
# isso o log leva a resposta INTEIRA em bruto e a voz so o que passa o filtro.
# Este modulo importa de la e nao define limites proprios.

#: Limite por frase entregue ao Claude Code.
LIMITE_CLAUDE_S = 120.0

#: As tres accoes da lista branca da D4 que sao ESTADO DESTE PROCESSO e nao
#: accoes do sistema: o `jarvis.acoes_locais.executar()` recusa-as de proposito
#: (nao ha processo vivo nenhum numa biblioteca) e sao tratadas aqui.
ACOES_DE_ESTADO = frozenset({"calar", "adormecer", "acordar"})

NOMES_DAS_ETAPAS = {
    1: "1/5 palavra de ativacao",
    2: "2/5 transcricao          ",
    3: "3/5 encaminhamento       ",
    4: "4/5 accao/entrega        ",
    5: "5/5 resposta falada      ",
}

LARGURA_DA_SEPARACAO = 78


def agora_iso(quando: datetime.datetime | None = None) -> str:
    """Timestamp local com milissegundos, o mesmo na consola e no ficheiro."""
    quando = quando or datetime.datetime.now()
    return quando.strftime("%Y-%m-%d %H:%M:%S.") + f"{quando.microsecond // 1000:03d}"


def texto_para_a_consola(texto: str, codificacao: str | None = None) -> str:
    """O mesmo texto, garantidamente imprimivel na consola em uso.

    Mesma convencao (e mesma razao) de scripts/transcrever_ficheiro.py: perder
    um acento na consola e mau, perder o registo da frase por causa dele e
    pior. O ficheiro de log fica sempre em UTF-8, com os acentos intactos.
    """
    codificacao = codificacao or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        texto.encode(codificacao)
    except (UnicodeEncodeError, LookupError):
        return texto.encode(codificacao, errors="replace").decode(codificacao, errors="replace")
    return texto


def caminho_do_log(quando: datetime.datetime | None = None, pasta: Path = PASTA_LOGS) -> Path:
    """`logs/jarvis-<data>.log` — um ficheiro por dia, sempre em append."""
    quando = quando or datetime.datetime.now()
    return pasta / f"jarvis-{quando:%Y-%m-%d}.log"


class LogDaSessao:
    """O log unico: a mesma linha na consola E no ficheiro do dia.

    Seguro entre threads de proposito: as callbacks do RealtimeSTT (palavra de
    ativacao, inicio e fim da fala) correm na thread do gravador, e o resto do
    pipeline na thread principal.
    """

    def __init__(
        self,
        pasta: Path = PASTA_LOGS,
        consola=None,
        quando: datetime.datetime | None = None,
        relogio_de_parede: Callable[[], datetime.datetime] = datetime.datetime.now,
    ) -> None:
        self.caminho = caminho_do_log(quando, pasta)
        self.consola = consola if consola is not None else sys.stdout
        self._relogio_de_parede = relogio_de_parede
        self._tranca = threading.Lock()
        garantir_pasta(self.caminho.parent)
        self._ficheiro = self.caminho.open("a", encoding="utf-8")

    def linha(self, texto: str) -> str:
        """Escreve uma linha com timestamp. Devolve a linha completa."""
        completa = f"{agora_iso(self._relogio_de_parede())} {texto}"
        self._escrever(completa)
        return completa

    def bruto(self, texto: str = "") -> None:
        """Escreve sem timestamp (cabecalhos e separadores)."""
        self._escrever(texto)

    def _escrever(self, texto: str) -> None:
        with self._tranca:
            print(texto_para_a_consola(texto), file=self.consola, flush=True)
            self._ficheiro.write(texto + "\n")
            self._ficheiro.flush()

    def fechar(self) -> None:
        with self._tranca:
            if not self._ficheiro.closed:
                self._ficheiro.close()


def formatar_etapa(numero_da_frase: int, etapa: int, latencia_ms: float, detalhe: str) -> str:
    """A linha de uma etapa, exatamente como sai na consola e no ficheiro."""
    return (
        f"frase #{numero_da_frase} | etapa {NOMES_DAS_ETAPAS[etapa]} "
        f"| {latencia_ms:7.0f} ms | {detalhe}"
    )


@dataclass(frozen=True)
class Marca:
    """Uma etapa medida: quanto demorou e o que aconteceu nela."""

    etapa: int
    latencia_ms: float
    detalhe: str


@dataclass
class RegistoDaFrase:
    """O registo unico de uma frase: cinco etapas, latencias e total.

    As latencias sao calculadas a partir de um relogio monotonico injetavel
    (`relogio`), por isso o formato e a aritmetica testam-se sem audio nenhum.
    """

    numero: int
    log: LogDaSessao | None = None
    relogio: Callable[[], float] = time.perf_counter
    inicio: float = field(default=0.0)
    marcas: list[Marca] = field(default_factory=list)
    #: Instante em que o VAD fechou a frase. E a referencia da etapa 2 e da
    #: latencia total que o Sponsor sente (D6).
    fim_da_fala: float | None = None
    inicio_da_fala: float | None = None
    descartada: bool = False
    fechada: bool = False
    _referencia: float = field(default=0.0)

    def __post_init__(self) -> None:
        if not self.inicio:
            self.inicio = self.relogio()
        self._referencia = self.inicio

    def reiniciar_relogio(self) -> None:
        """Poe o inicio da frase agora (depois de carregar modelos, por ex.)."""
        self.inicio = self.relogio()
        self._referencia = self.inicio

    # -- marcacao das etapas

    def marcar(
        self, etapa: int, detalhe: str, desde: float | None = None, instante: float | None = None
    ) -> Marca:
        """Fecha uma etapa: latencia desde a etapa anterior (ou desde `desde`).

        `instante` fixa o momento em que a etapa aconteceu de facto, para o
        caso de so se poder escrever a linha um pouco depois.
        """
        instante = self.relogio() if instante is None else instante
        referencia = self._referencia if desde is None else desde
        marca = Marca(etapa=etapa, latencia_ms=(instante - referencia) * 1000, detalhe=detalhe)
        self.marcas.append(marca)
        self._referencia = instante
        self._emitir(formatar_etapa(self.numero, etapa, marca.latencia_ms, detalhe))
        return marca

    def marcar_inicio_da_fala(self) -> None:
        self.inicio_da_fala = self.relogio()

    def marcar_fim_da_fala(self) -> None:
        self.fim_da_fala = self.relogio()

    def nota(self, texto: str) -> None:
        """Uma linha do registo desta frase que nao e uma das cinco etapas."""
        self._emitir(f"frase #{self.numero} | {texto}")

    def descartar(self, motivo: str) -> None:
        """A linha explicita da D5/D6: um falso despertar que nao fez nada."""
        self.descartada = True
        self._emitir(
            f"frase #{self.numero} | FALSO DESPERTAR DESCARTADO | {motivo} "
            "| nenhuma accao executada, nada enviado ao Claude Code (D5/D6)"
        )

    def fechar(self, resultado: str) -> str:
        """A linha final: latencia total, pelas duas referencias que importam."""
        self.fechada = True
        agora = self.relogio()
        total_desde_o_inicio = (agora - self.inicio) * 1000
        if self.fim_da_fala is not None:
            desde_a_fala = f"{(agora - self.fim_da_fala) * 1000:.0f} ms desde o fim da fala"
        else:
            desde_a_fala = "sem fala detetada"
        linha = (
            f"frase #{self.numero} | TOTAL | {total_desde_o_inicio:7.0f} ms "
            f"desde o inicio da escuta | {desde_a_fala} | {resultado}"
        )
        self._emitir(linha)
        self._emitir("-" * LARGURA_DA_SEPARACAO)
        return linha

    def _emitir(self, texto: str) -> None:
        if self.log is not None:
            self.log.linha(texto)


# --- Palavra de ativacao: o mesmo modelo, fora da porta do RealtimeSTT ------


class MonitorWakeWord:
    """Corre o modelo openWakeWord sobre os frames, sem gatilho nenhum.

    Existe para a etapa 1 do modo ficheiro ser uma MEDIDA e nao uma suposicao:
    quando a porta do RealtimeSTT esta aberta (o WAV nao tem palavra de
    ativacao), o mesmo `hey_jarvis` corre sobre os mesmos frames e diz o que
    viu — o instante em que disparou, ou o score maximo que o ficheiro atingiu.
    """

    def __init__(
        self,
        modelo: Path = MODELO_WAKE_WORD,
        sensibilidade: float = SENSIBILIDADE_WAKE_WORD,
        relogio: Callable[[], float] = time.perf_counter,
    ) -> None:
        for ficheiro in (modelo, MODELO_MELSPEC, MODELO_EMBEDDING):
            if not ficheiro.is_file():
                raise FileNotFoundError(
                    f"modelo do openWakeWord em falta: '{ficheiro}'. Descarregar com: "
                    '.venv\\Scripts\\python -c "import openwakeword.utils as u; '
                    "u.download_models(['hey_jarvis'], target_directory='models/openwakeword')\""
                )
        import numpy as np
        from openwakeword.model import Model

        self._np = np
        self.sensibilidade = sensibilidade
        self.relogio = relogio
        self.modelo = Model(
            wakeword_models=[str(modelo)],
            inference_framework="onnx",
            melspec_model_path=str(MODELO_MELSPEC),
            embedding_model_path=str(MODELO_EMBEDDING),
        )
        self.score_maximo = 0.0
        self.instante_da_deteccao: float | None = None

    @property
    def detetou(self) -> bool:
        return self.instante_da_deteccao is not None

    def processar(self, chunk: bytes) -> float:
        """Alimenta um chunk PCM16 mono de 16 kHz. Devolve o score desse chunk."""
        pcm = self._np.frombuffer(chunk, dtype=self._np.int16)
        if pcm.size == 0:
            return 0.0
        previsao = self.modelo.predict(pcm)
        score = max(previsao.values()) if previsao else 0.0
        self.score_maximo = max(self.score_maximo, float(score))
        if score >= self.sensibilidade and self.instante_da_deteccao is None:
            self.instante_da_deteccao = self.relogio()
        return float(score)


def prova_da_wake_word(caminho: Path, sensibilidade: float = SENSIBILIDADE_WAKE_WORD) -> dict:
    """Corre o openWakeWord DIRETAMENTE sobre um WAV e devolve o que mediu.

    E a prova a parte da etapa 1 que o criterio da T6 pede quando o WAV do
    pipeline nao tem palavra de ativacao: mesmo modelo, mesmo limiar, um
    ficheiro que comeca por "hey jarvis".
    """
    monitor = MonitorWakeWord(sensibilidade=sensibilidade)
    scores: list[float] = []
    for chunk in frames_do_wav(caminho):
        scores.append(monitor.processar(chunk))
    return {
        "ficheiro": str(caminho),
        "chunks": len(scores),
        "score_maximo": monitor.score_maximo,
        "sensibilidade": sensibilidade,
        "detetou": monitor.detetou,
        "chunks_acima_do_limiar": sum(1 for s in scores if s >= sensibilidade),
    }


# --- Injeccao de ficheiro (S5): os frames do WAV no MESMO pipeline ----------


def frames_do_wav(
    caminho: Path, bytes_por_chunk: int = BYTES_POR_CHUNK
) -> Iterator[bytes]:
    """Frames PCM16 mono de 16 kHz, do tamanho que o RealtimeSTT consome.

    Aceita qualquer WAV PCM de 16 bits: reamostra para 16 kHz e junta os canais
    quando preciso (os WAV da T3 ja sao mono/16 kHz, mas um WAV gravado pelo
    microfone do Sponsor pode nao ser). O ultimo chunk e completado com
    silencio para todos terem o mesmo tamanho — o `feed_audio` do RealtimeSTT
    so entrega ao pipeline buffers completos.
    """
    dados, taxa, canais = ler_wav_pcm16(caminho)
    if canais not in (1, 2):
        raise ValueError(f"{caminho}: esperava 1 ou 2 canais, encontrei {canais}")
    if canais == 2:
        import audioop  # stdlib; aviso de depreciacao esperado em 3.12+

        dados = audioop.tomono(dados, 2, 0.5, 0.5)
    dados = reamostrar_pcm16(dados, taxa, TAXA_AMOSTRAGEM, canais=1)
    if not dados:
        return
    for inicio in range(0, len(dados), bytes_por_chunk):
        chunk = dados[inicio : inicio + bytes_por_chunk]
        if len(chunk) < bytes_por_chunk:
            chunk = chunk + b"\x00" * (bytes_por_chunk - len(chunk))
        yield chunk


def chunks_de_silencio(segundos: float, bytes_por_chunk: int = BYTES_POR_CHUNK) -> Iterator[bytes]:
    """Silencio digital, para o VAD fechar a frase depois do ficheiro."""
    amostras = int(segundos * TAXA_AMOSTRAGEM)
    total = amostras * 2
    for _ in range(max(total // bytes_por_chunk, 0)):
        yield b"\x00" * bytes_por_chunk


def duracao_do_chunk_s(bytes_por_chunk: int = BYTES_POR_CHUNK) -> float:
    """Quanto tempo de audio cabe num chunk (PCM16 mono a 16 kHz)."""
    return bytes_por_chunk / 2 / TAXA_AMOSTRAGEM


# --- O processo -------------------------------------------------------------


@dataclass
class EstadoDoProcesso:
    """O que so existe enquanto o jarvis esta a correr (D4.d/D4.e).

    `jarvis.acoes_locais.executar()` recusa `calar`, `adormecer` e `acordar` de
    proposito: uma biblioteca sem processo vivo nao tem nada para calar nem
    para adormecer. Quem as executa e este processo.
    """

    adormecido: bool = False
    mudo: bool = False


class Jarvis:
    """Cola as pecas das tasks anteriores e escreve o registo de cada frase.

    As dependencias entram pelo construtor (`falar`, `executar`, `abrir_canal`)
    para os testes correrem a cadeia inteira sem Piper, sem VS Code e sem
    Claude Code — o default de cada uma e a peca real.
    """

    def __init__(
        self,
        config: Config,
        log: LogDaSessao,
        *,
        estado: EstadoDoProcesso | None = None,
        falar: Callable[..., voz.ResultadoFala] = voz.falar,
        executar: Callable[..., acoes_locais.ResultadoAcao] = acoes_locais.executar,
        abrir_canal: Callable[[], object] = canal_claude.abrir_canal,
        com_voz: bool = True,
        calar: Callable[..., voz.ResultadoSilencio] = voz.calar_agora,
    ) -> None:
        self.config = config
        self.log = log
        self.estado = estado or EstadoDoProcesso()
        self._falar = falar
        # O MESMO mecanismo dos tres gatilhos (D60(2)); entra pelo construtor
        # so para os testes o poderem espiar sem Piper nem dispositivo de som.
        self._calar = calar
        self._executar = executar
        self._abrir_canal = abrir_canal
        self.com_voz = com_voz
        self.canal = None
        self.frases = 0

    # -- etapa 3: encaminhamento

    def encaminhar(self, texto: str, registo: RegistoDaFrase) -> ResultadoRouter:
        resultado = encaminhar(texto, self.config)
        alvo = {
            "local": f"accao local '{resultado.nome_acao}'",
            "claude": "texto para o Claude Code",
            "nada": "nada",
        }[resultado.tipo]
        # D62: o que a limpeza do residuo da palavra de ativacao tirou do
        # inicio da frase, explicavel ao Sponsor numa frase deste log.
        residuo = (
            f"'{resultado.residuo_removido}'" if resultado.residuo_removido else "nenhum"
        )
        registo.marcar(
            3,
            f"decisao={resultado.tipo} -> {alvo} | confianca={'nao medida' if not resultado.confianca_verificada else 'verificada'}"
            f" | residuo da wake word removido: {residuo}"
            f" | motivo: {resultado.motivo}",
        )
        return resultado

    # -- etapa 4: accao local, estado do processo, ou entrega ao Claude Code

    def _accao_de_estado(self, nome_acao: str) -> str:
        if nome_acao == "calar":
            # D60(2): "cala-te" cala a frase que esta a ser dita NAQUELE
            # momento, pelo mesmo mecanismo do Ctrl+C, e so depois marca o
            # estado para as respostas futuras (que era tudo o que fazia).
            # `definitivo=False`: um "acorda" a seguir volta a poder falar.
            self.calar_agora("cala-te (lista branca D4.d)", definitivo=False)
            self.estado.mudo = True
            return "Fico calado."
        if nome_acao == "adormecer":
            self.estado.adormecido = True
            return "Vou dormir. Diz 'acorda' quando precisares."
        self.estado.adormecido = False
        self.estado.mudo = False
        return "Estou acordado."

    def _entregar_ao_claude(self, texto: str, registo: RegistoDaFrase) -> str:
        if self.canal is None:
            registo.nota(
                "canal do Claude Code: a abrir a sessao-ponte (degrau "
                f"{canal_claude.transporte_em_vigor()}, D49) | aviso de privacidade: a frase "
                "fica gravada em claro no historico da sessao, fora deste repositorio (D50.10)"
            )
            self.canal = self._abrir_canal()
        resposta = self.canal.perguntar(texto, LIMITE_CLAUDE_S)
        registo.nota(
            "resposta do Claude Code (sessao filha sem ferramentas, D48.3/D48.4 - "
            f"texto do modelo, nao facto verificado): {resposta!r}"
        )
        return resumo_falado(resposta)

    def agir(self, resultado: ResultadoRouter, registo: RegistoDaFrase) -> str | None:
        """Executa a etapa 4 e devolve o texto da resposta (None = nao ha)."""
        if self.estado.adormecido and resultado.nome_acao != "acordar":
            registo.marcar(4, "ignorada: o jarvis esta adormecido (D4.e); diz 'acorda' para voltar")
            return None

        if resultado.tipo == "local":
            nome_acao = resultado.nome_acao or ""
            if nome_acao in ACOES_DE_ESTADO:
                resposta = self._accao_de_estado(nome_acao)
                registo.marcar(
                    4,
                    f"estado do processo: {nome_acao} | adormecido={self.estado.adormecido} "
                    f"mudo={self.estado.mudo} | zero tokens",
                )
                return resposta
            try:
                feito = self._executar(resultado, self.config)
            except acoes_locais.AcaoError as erro:
                registo.marcar(4, f"accao local RECUSADA: {erro}")
                return "Não consegui executar esse comando."
            detalhe = f"accao local '{feito.nome_acao}' executada={feito.executou} | zero tokens"
            if feito.comando:
                detalhe += f" | comando: {feito.comando}"
            registo.marcar(4, detalhe)
            return feito.texto

        try:
            resposta = self._entregar_ao_claude(resultado.texto or "", registo)
        except Exception as erro:  # noqa: BLE001 - o canal nunca derruba o jarvis
            registo.marcar(4, f"entrega ao Claude Code FALHOU: {erro!r}")
            return "Não consegui falar com o Claude Code."
        registo.marcar(
            4,
            f"entregue ao Claude Code pelo degrau {canal_claude.transporte_em_vigor()} (D49) | "
            f"{len(resposta)} caracteres para a voz (resposta inteira na linha acima)",
        )
        return resposta

    # -- etapa 5: resposta falada

    def responder(self, texto: str | None, registo: RegistoDaFrase) -> None:
        if not texto:
            registo.marcar(5, "sem resposta a dar")
            return
        falado = " ".join(texto.split())
        if len(falado) > MAXIMO_ABSOLUTO_FALADO:  # ultima rede, nunca deve disparar
            # D59.3: mesmo este corte de emergencia nunca parte uma palavra ao meio. Nao havendo
            # um unico espaco dentro do limite (uma "palavra" de centenas de caracteres, que
            # nunca e linguagem natural), diz-se a frase de recurso em vez de ler meia palavra —
            # e sem ficar calado (inaceitavel n.4). O texto inteiro ja foi para o log.
            falado = cortar_no_limite(falado, MAXIMO_ABSOLUTO_FALADO) or (
                FRASE_RECURSO_SEM_CORTE_SEGURO
            )
        if voz.esta_calado():
            # Depois de um Ctrl+C (ou da saida do processo) nada novo e
            # falado: nem o resto da frase, nem uma despedida, nem esta
            # resposta do Claude Code que acabou de chegar (D60(1)).
            registo.marcar(5, f"silenciado a pedido (D60): nada e falado | texto: {falado!r}")
            return
        if self.estado.mudo or not self.com_voz:
            razao = "modo calado (D4.d)" if self.estado.mudo else "--sem-voz"
            registo.marcar(5, f"voz desligada ({razao}); resposta so na consola: {falado!r}")
            return
        # com_som=True: o UNICO opt-in explicito da D61 que liga as colunas a
        # serio. E o jarvis a serio (este processo): tem de falar, por isso
        # liga-se aqui, sempre — o interruptor continua a ser --sem-voz (que
        # ja fez `self.com_voz` chegar a False e devolver mais acima, nunca
        # chegando a esta linha) e o "cala-te"/adormecido (idem). Sem este
        # opt-in, `jarvis.voz.falar()` recusa-se a abrir o dispositivo (D61).
        resultado = self._falar(falado, com_som=True)
        if resultado.falou:
            registo.marcar(5, f"falado em pt-PT (Piper): {falado!r}")
        else:
            registo.marcar(
                5,
                f"voz falhou, resposta em texto na consola (D35.4): {resultado.motivo_falha} "
                f"| texto: {falado!r}",
            )

    # -- a frase inteira, da transcricao a resposta

    def tratar_transcricao(self, texto: str, registo: RegistoDaFrase) -> ResultadoRouter:
        resultado = self.encaminhar(texto, registo)
        if resultado.tipo == "nada":
            registo.descartar(resultado.motivo)
            registo.fechar("descartada: zero accoes, zero tokens")
            return resultado
        resposta = self.agir(resultado, registo)
        self.responder(resposta, registo)
        if resultado.tipo == "local":
            fim = f"accao local '{resultado.nome_acao}' (zero tokens)"
        else:
            fim = "entregue ao Claude Code"
        if self.estado.adormecido and resultado.nome_acao != "acordar":
            fim = "ignorada (jarvis adormecido)"
        registo.fechar(fim)
        return resultado

    def calar_agora(
        self, motivo: str, *, definitivo: bool, ja_calado: bool = False
    ) -> voz.ResultadoSilencio:
        """Cala a voz JA e escreve as duas linhas com timestamps (D60(4)(b)).

        E o unico caminho de silenciamento do processo: usam-no o handler de
        Ctrl+C, a saida do processo e a accao "calar" da lista branca da D4.d
        (e o equivalente ingles quando existir). `ja_calado=True` faz o mesmo
        trabalho sem repetir as duas linhas no log.
        """
        return self._calar(
            motivo, definitivo=definitivo, registar=None if ja_calado else self.log.linha
        )

    def fechar(self) -> None:
        canal = self.canal
        self.canal = None
        if canal is not None:
            fechar = getattr(canal, "fechar", None)
            if callable(fechar):
                fechar()


# --- Construcao do gravador (o MESMO nos dois modos) ------------------------


def construir_recorder(
    *,
    use_microphone: bool,
    com_wake_word: bool,
    modelo: str = MODELO_STT,
    device: str = "cuda",
    callbacks: dict | None = None,
):
    """O `AudioToTextRecorder` do RealtimeSTT, igual nos dois modos.

    A unica diferenca entre o microfone ao vivo e a injeccao de ficheiro e
    `use_microphone`; a porta da palavra de ativacao (`com_wake_word`) e a
    unica diferenca entre o modo `--wav` por omissao e `--exigir-wake-word`.

    LIMITE CONHECIDO, do RealtimeSTT e nao deste codigo: o construtor espera
    pelo modelo com `main_transcription_ready_event.wait()` SEM limite de tempo
    (audio_recorder.py:968). Se o processo filho nao conseguir carregar o
    modelo — sem os pesos em models/faster-whisper e sem rede, por exemplo —,
    isto fica pendurado em vez de levantar, e por isso nao ha aqui nenhum
    recuo automatico para um modelo mais pequeno: seria codigo que nunca
    correria. Quem precisar de outro modelo passa `--modelo`.
    """
    if device == "cuda":
        registar_dlls_do_torch()  # tem de correr antes do faster_whisper (D42)
    from RealtimeSTT import AudioToTextRecorder

    opcoes = dict(
        model=modelo,
        download_root=str(PASTA_MODELOS_FASTER_WHISPER),
        # T8, criterio 6: a lingua volta a ser FIXA aqui.
        #
        # A T8 ligou a deteccao automatica neste mesmo sitio (`language=None`,
        # que e o que o RealtimeSTT traduz para deteccao: ele faz
        # `language=self.language if self.language else None` antes de chamar o
        # faster-whisper, audio_recorder.py:200 e :2402) e mediu-a. O A/B
        # controlado — os MESMOS 20 WAV por combinacao, transcritos com a
        # lingua fixa e com deteccao — deu o acerto de intencao em portugues a
        # DESCER (21/40 -> 20/40), e esse e o gatilho automatico do criterio 6
        # e da ordem de corte da D53 item 4: reverte-se.
        #
        # A causa medida: com deteccao, o argmax LIVRE do faster-whisper (~100
        # linguas) e que descodifica, e caiu fora de {pt, en} em 3 das 20
        # frases portuguesas sem prefixo — uma delas, uma frase inteira e
        # limpa, saiu em grego e deixou de ser encaminhada.
        #
        # O ingles nao depende disto: o encaminhamento casa sempre contra AS
        # DUAS listas brancas (T7, D58b), e o mesmo A/B deu ZERO linhas
        # inglesas a mudar de acerto.
        language=LINGUA_FIXA_DO_PRODUTO,
        device=device,
        compute_type="float16" if device == "cuda" else "int8",
        use_microphone=use_microphone,
        spinner=False,
        no_log_file=True,
        initial_prompt=INITIAL_PROMPT,  # D51: desligado no caminho vivo
        beam_size=5,
        post_speech_silence_duration=0.6,
        min_length_of_recording=0.3,
        pre_recording_buffer_duration=1.0,
    )
    if com_wake_word:
        if not MODELO_WAKE_WORD.is_file():
            raise FileNotFoundError(
                f"modelo da palavra de ativacao em falta: '{MODELO_WAKE_WORD}' "
                "(ver docs/MODELOS.md)"
            )
        opcoes.update(
            wakeword_backend="oww",
            wake_words="hey jarvis",
            openwakeword_model_paths=str(MODELO_WAKE_WORD),
            openwakeword_inference_framework="onnx",
            wake_words_sensitivity=SENSIBILIDADE_WAKE_WORD,
            wake_word_timeout=5.0,
        )
    opcoes.update(callbacks or {})
    return AudioToTextRecorder(**opcoes)


def detalhe_da_transcricao(recorder, texto: str) -> str:
    """A linha da etapa 2: device real, modelo, prompt (D51), lingua e o texto.

    LINGUA (T8/D58b, criterio 2): escreve a lingua de CADA frase. Hoje o
    recorder e construido com a lingua FIXA (criterio 6 da T8: o A/B controlado
    mostrou o acerto em portugues a descer com a deteccao ligada), e entao a
    linha diz `lingua=pt FIXA (...)` — sem probabilidade nenhuma, porque nao ha
    nenhuma medida e escrever `p=0.00 hesitou` daria a entender que uma
    deteccao correu e falhou.

    Se o recorder for construido sem lingua (`language=None`, a deteccao que a
    T8 mediu e que volta a ligar-se no dia em que houver numeros que a
    sustentem), a mesma linha escreve a lingua detetada, a probabilidade e se a
    deteccao HESITOU (probabilidade nao acima do limiar de 0,5 da S10). Os dois
    caminhos continuam testados.

    ACHADO da T8, registado aqui porque e onde ele se ve: o RealtimeSTT 0.3.104
    so expoe o TOP-1 da deteccao (`detected_language` /
    `detected_language_probability`, `audio_recorder.py:1533-1534`) e deita
    fora o resto do `info` do faster-whisper — o `all_language_probs` nunca
    chega ate aqui, nem por `recorder.text()` nem por `recorder.transcribe()`,
    que tambem devolve so a string. Por isso o caminho vivo aplica o argmax
    restrito a {pt, en} sobre o unico numero que tem: se o top-1 for uma
    terceira lingua, ela nao entra na escolha do produto (fica o portugues por
    omissao, com o motivo escrito). Sem monkeypatch ao RealtimeSTT, como a S10
    previu no ponto 1 de "Como adotar".

    LINGUA-TERCEIRA (D66, ponto 3): o que o caminho vivo NAO consegue e evitar
    que essa terceira lingua descodifique o audio — o RealtimeSTT tambem chama
    o faster-whisper com `language=None`, logo o top-1 que ele devolve E a
    lingua com que o texto foi descodificado. Quando cai fora de {pt, en}, a
    linha desta etapa leva a marca `lingua-terceira(<codigo> descodificou)`, na
    medida exata em que a biblioteca deixa: aqui so ha o top-1, nao ha o
    `all_language_probs` e nao ha forma de repetir a transcricao. A frase segue
    o caminho normal, nunca e descartada.

    Nada disto muda o encaminhamento: a frase casa sempre contra as DUAS
    listas brancas (T7), hesite a deteccao ou nao.
    """
    # `recorder.language` e o que foi pedido na construcao (RealtimeSTT guarda
    # o argumento tal e qual): com ele preenchido nao houve deteccao nenhuma, e
    # o `detected_language` que a biblioteca escreve nesse caso e so o eco do
    # que lhe demos, a 1.0 de probabilidade — um numero que ninguem mediu.
    lingua_pedida = (getattr(recorder, "language", "") or "").strip()
    if lingua_pedida:
        deteccao = lingua_fixada(lingua_pedida)
    else:
        deteccao = decidir_lingua_do_top1(
            getattr(recorder, "detected_language", None),
            getattr(recorder, "detected_language_probability", 0.0),
        )
    detalhe_da_lingua = deteccao.para_log()
    if deteccao.hesitou or deteccao.lingua_terceira:
        detalhe_da_lingua += f" [{deteccao.motivo}]"
    return (
        f"device={getattr(recorder, 'device', '?')} modelo={getattr(recorder, 'main_model_type', '?')} "
        f"prompt={ESTADO_PROMPT_DESLIGADO} (D51) {detalhe_da_lingua} "
        f"| texto: {texto!r}"
    )


# --- Ruido de terceiros no encerramento (QA-close-1.md, finding 4b) ---------
#
# Onde nasce o ruido: `RealtimeSTT/audio_recorder.py:134` faz
# `logging.error(f"Error receiving data from connection: {e}", exc_info=True)`
# dentro de `TranscriptionWorker.poll_connection`, quando o pipe ja foi fechado
# pelo `recorder.shutdown()`. Em Windows esse worker NAO corre neste processo:
# `_start_thread` (audio_recorder.py:996) usa `mp.Process` sempre que o sistema
# nao e Linux, por isso a mensagem e escrita no logger raiz do PROCESSO FILHO e
# sai pelo stderr herdado. Um filtro instalado so aqui no pai nunca e consultado
# pelo filho — e por isso que o silenciador tem duas metades:
#
#   1. `instalar_silenciador_no_processo_filho()`, chamado no corpo deste modulo,
#      que so faz alguma coisa quando o modulo esta a ser executado DENTRO de um
#      filho de multiprocessing. Em Windows (start method `spawn`) o filho volta
#      a executar o modulo `__main__` do pai como `__mp_main__`
#      (`multiprocessing/spawn.py`, `prepare()` -> `_fixup_main_from_name`), e o
#      `__main__` do caminho real e precisamente este modulo
#      (`python -m jarvis.app`); quando o ponto de entrada e outro, esse outro
#      importa na mesma `jarvis.app` para chegar a `correr_wav`/`correr_microfone`.
#      De qualquer das formas este corpo corre no filho, antes de o worker existir.
#   2. `silenciar_ruido_do_shutdown()`, usado so a volta das duas chamadas
#      `recorder.shutdown()`. Esta metade NAO resolve o caso Windows (o emissor
#      esta noutro processo): serve de defesa em profundidade e cobre o caso
#      Linux, onde `_start_thread` usa uma `threading.Thread` deste processo e
#      portanto o mesmo logger raiz.
#
# Em ambas as metades descarta-se um unico registo — o que traz as DUAS partes
# da mensagem conhecida — e tudo o resto, de qualquer nivel, passa intacto.

_RUIDO_DE_ENCERRAMENTO = ("Error receiving data from connection", "WinError 6")


class _FiltroDoRuidoDeEncerramento(logging.Filter):
    """Descarta so o traceback conhecido do WinError 6 no shutdown do RealtimeSTT.

    QA-close-1.md, finding 4b: intermitente, aparece DEPOIS de a resposta ja
    ter sido dada, em `recorder.shutdown()`. Qualquer outro registo, de
    qualquer nivel, passa sem tocar (tem de trazer as DUAS partes da mensagem
    exata do QA, nunca so uma).
    """

    MENSAGEM_1, MENSAGEM_2 = _RUIDO_DE_ENCERRAMENTO

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - API do logging
        mensagem = record.getMessage()
        return not (self.MENSAGEM_1 in mensagem and self.MENSAGEM_2 in mensagem)


def _num_processo_filho_de_multiprocessing() -> bool:
    """Este modulo esta a ser executado dentro de um filho de multiprocessing?

    Em `spawn` (Windows) o corpo do modulo corre durante a preparacao do filho,
    antes de `_bootstrap`, por isso `parent_process()` ainda devolve `None`;
    nessa janela o sinal disponivel e `_inheriting` (posto por
    `multiprocessing/spawn.py::_main`) e o nome do processo, que `prepare()` ja
    substituiu por "Process-N". Testam-se os tres: qualquer um chega.
    """
    if multiprocessing.parent_process() is not None:
        return True
    processo = multiprocessing.current_process()
    if getattr(processo, "_inheriting", False):
        return True
    return processo.name != "MainProcess"


def instalar_silenciador_no_processo_filho() -> bool:
    """No processo filho do RealtimeSTT: descarta so o WinError 6 conhecido.

    Devolve True se instalou (e portanto se estamos num filho). No processo
    principal nao faz absolutamente nada — nenhum filtro, nenhum handler,
    nenhum toque no stderr. Qualquer falha e engolida de proposito: no pior
    caso o filtro nao fica instalado e o programa comporta-se exatamente como
    antes, com a linha de ruido a aparecer.
    """
    try:
        if not _num_processo_filho_de_multiprocessing():
            return False
        logging.getLogger().addFilter(_FiltroDoRuidoDeEncerramento())
    except Exception:  # noqa: BLE001 - nunca partir um filho por causa de ruido
        return False
    return True


@contextlib.contextmanager
def silenciar_ruido_do_shutdown() -> Iterator[None]:
    """So a volta de `recorder.shutdown()`: tira do stderr o WinError 6 conhecido.

    Defesa em profundidade e cobertura do caso Linux (worker em thread deste
    processo). Em Windows o emissor esta noutro processo e quem trata dele e
    `instalar_silenciador_no_processo_filho()`.

    Instala o filtro no logger raiz e remove-o sempre no `finally`, mesmo que
    o shutdown levante. Se a instalacao ou a remocao falharem por qualquer
    razao, o pior caso e o filtro nao ter efeito (ou ficar por instalar) — o
    `shutdown()` corre exatamente como corria antes disto existir; nunca
    engole excecoes do proprio shutdown.
    """
    logger_raiz = logging.getLogger()
    filtro = _FiltroDoRuidoDeEncerramento()
    try:
        logger_raiz.addFilter(filtro)
    except Exception:  # noqa: BLE001 - nunca impedir o shutdown por causa disto
        yield
        return
    try:
        yield
    finally:
        logger_raiz.removeFilter(filtro)


# Corre no corpo do modulo de proposito: em `spawn` esta e a unica janela em que
# ainda se chega ao filho antes de o worker do RealtimeSTT comecar a falar.
_SILENCIADOR_INSTALADO_NO_FILHO = instalar_silenciador_no_processo_filho()


# --- Modo (b): ficheiro -----------------------------------------------------


def correr_wav(
    caminho: Path,
    jarvis: Jarvis,
    *,
    exigir_wake_word: bool = False,
    device: str = "cuda",
    modelo: str = MODELO_STT,
) -> int:
    """Injeta um WAV no mesmo pipeline e escreve o registo das cinco etapas."""
    log = jarvis.log
    if not caminho.is_file():
        log.linha(f"ERRO: ficheiro nao encontrado: '{caminho}'")
        return 1
    try:
        frames = list(frames_do_wav(caminho))
    except (ValueError, OSError) as erro:
        log.linha(f"ERRO: nao consegui ler '{caminho}': {erro}")
        return 1
    if not frames:
        log.linha(f"ERRO: '{caminho}' nao tem audio nenhum")
        return 1

    duracao_s = len(frames) * duracao_do_chunk_s()
    log.bruto("")
    log.bruto("=" * LARGURA_DA_SEPARACAO)
    log.bruto("  JARVIS - MODO FICHEIRO (o microfone NAO e usado)")
    log.bruto(f"  WAV: {caminho}  ({duracao_s:.2f} s, 16 kHz mono)")
    log.bruto(
        "  porta da palavra de ativacao: "
        + (
            "FECHADA (oww hey_jarvis; so transcreve depois de disparar)"
            if exigir_wake_word
            else "ABERTA (injeccao a partir da etapa 2, D33) + monitor oww nos mesmos frames"
        )
    )
    log.bruto("=" * LARGURA_DA_SEPARACAO)

    jarvis.frases += 1
    registo = RegistoDaFrase(numero=jarvis.frases, log=log)
    primeira_deteccao = threading.Event()

    def ao_detetar_wake_word() -> None:
        if not primeira_deteccao.is_set():
            primeira_deteccao.set()
            registo.marcar(
                1,
                f"DETETADA no ficheiro pelo backend oww do RealtimeSTT "
                f"(modelo hey_jarvis, limiar {SENSIBILIDADE_WAKE_WORD})",
            )

    recorder = construir_recorder(
        use_microphone=False,
        com_wake_word=exigir_wake_word,
        device=device,
        modelo=modelo,
        callbacks={
            "on_wakeword_detected": ao_detetar_wake_word,
            "on_recording_start": registo.marcar_inicio_da_fala,
            "on_recording_stop": registo.marcar_fim_da_fala,
        },
    )
    monitor = None if exigir_wake_word else MonitorWakeWord()
    registo.reiniciar_relogio()  # os modelos ja estao carregados: o relogio da frase comeca aqui
    log.linha(
        f"frase #{registo.numero} | a injetar {len(frames)} chunks de "
        f"{BYTES_POR_CHUNK} bytes ({duracao_s:.2f} s) no pipeline "
        f"(feed_audio, use_microphone=False, S5)"
    )

    # Ritmo real do audio: alimentar de rajada encheria a fila do RealtimeSTT
    # (`handle_buffer_overflow` deita chunks fora acima de 100) e as latencias
    # medidas deixariam de querer dizer nada.
    espera = duracao_do_chunk_s()
    alimentacao_terminada = threading.Event()

    def alimentar() -> None:
        try:
            for chunk in frames:
                recorder.feed_audio(chunk)
                if monitor is not None:
                    ja_tinha_detetado = monitor.detetou
                    monitor.processar(chunk)
                    if monitor.detetou and not ja_tinha_detetado:
                        registo.marcar(
                            1,
                            "DETETADA nos mesmos frames pelo modelo hey_jarvis "
                            f"(score {monitor.score_maximo:.4f} >= limiar "
                            f"{SENSIBILIDADE_WAKE_WORD}); neste modo a porta esta aberta, "
                            "por isso a cadeia nao esperou por ela",
                            instante=monitor.instante_da_deteccao,
                        )
                time.sleep(espera)
            if monitor is not None and not monitor.detetou:
                registo.marcar(
                    1,
                    "NAO detetada no ficheiro (score maximo "
                    f"{monitor.score_maximo:.4f} < limiar {SENSIBILIDADE_WAKE_WORD}): "
                    "o WAV nao tem palavra de ativacao; a cadeia segue por injeccao a partir "
                    "da etapa 2 (D33) e a etapa 1 prova-se a parte com --prova-wake-word",
                )
            for chunk in chunks_de_silencio(SILENCIO_DEPOIS_DO_FICHEIRO_S):
                recorder.feed_audio(chunk)
                time.sleep(espera)
        finally:
            alimentacao_terminada.set()

    alimentador = threading.Thread(target=alimentar, name="alimentador-wav", daemon=True)
    resultado_texto: list[str] = []

    def transcrever() -> None:
        resultado_texto.append(recorder.text())

    leitor = threading.Thread(target=transcrever, name="transcricao", daemon=True)
    try:
        alimentador.start()
        leitor.start()
        alimentacao_terminada.wait(timeout=duracao_s + SILENCIO_DEPOIS_DO_FICHEIRO_S + 30)
        leitor.join(timeout=ESPERA_MAXIMA_DEPOIS_DO_FICHEIRO_S)
        if leitor.is_alive():
            registo.nota(
                f"sem frase fechada {ESPERA_MAXIMA_DEPOIS_DO_FICHEIRO_S:.0f} s depois do fim do "
                "ficheiro: o VAD nao encontrou fala util, a abortar a escuta"
            )
            recorder.abort()
            leitor.join(timeout=30)
        texto = (resultado_texto[0] if resultado_texto else "").strip()
        if exigir_wake_word and not primeira_deteccao.is_set():
            # A etapa 1 fica no log mesmo quando a porta nunca abriu: e a
            # diferenca entre "nao detetou" e "nao se sabe se correu".
            registo.marcar(
                1,
                "NAO detetada com a porta FECHADA: o modelo hey_jarvis nao disparou em nenhum "
                "frame deste WAV, por isso o pipeline nunca chegou a transcrever nada",
            )
        # `desde=fim_da_fala` so quando houve mesmo fala: sem VAD a disparar, a
        # etapa 2 mede-se desde a etapa anterior e o TOTAL diz "sem fala".
        registo.marcar(2, detalhe_da_transcricao(recorder, texto), desde=registo.fim_da_fala)
        jarvis.tratar_transcricao(texto, registo)
    finally:
        with silenciar_ruido_do_shutdown():
            recorder.shutdown()
        jarvis.fechar()
    return 0


# --- Modo (a): microfone ao vivo -------------------------------------------


def correr_microfone(jarvis: Jarvis, *, device: str = "cuda", modelo: str = MODELO_STT) -> int:
    """O ciclo ao vivo: uma frase de cada vez, sempre com o mesmo registo."""
    log = jarvis.log
    estado_da_frase: dict[str, RegistoDaFrase] = {}

    def novo_registo() -> RegistoDaFrase:
        jarvis.frases += 1
        registo = RegistoDaFrase(numero=jarvis.frases, log=log)
        estado_da_frase["registo"] = registo
        return registo

    def registo_atual() -> RegistoDaFrase | None:
        return estado_da_frase.get("registo")

    def ao_detetar_wake_word() -> None:
        registo = registo_atual()
        if registo is None or registo.marcas:
            return
        registo.marcar(
            1,
            f"DETETADA ao microfone (openWakeWord hey_jarvis, limiar {SENSIBILIDADE_WAKE_WORD})",
        )
        log.bruto(">>> A OUVIR A FRASE <<<")

    def ao_expirar_wake_word() -> None:
        """Palavra de ativacao sem frase atras: e o falso despertar da D5."""
        registo = registo_atual()
        if registo is None or not registo.marcas or registo.inicio_da_fala is not None:
            return
        registo.descartar(
            "palavra de ativacao seguida de silencio: nenhuma frase para transcrever"
        )
        registo.fechar("descartada: zero accoes, zero tokens")
        novo_registo()

    def ao_comecar_a_fala() -> None:
        registo = registo_atual()
        if registo is not None:
            registo.marcar_inicio_da_fala()

    def ao_acabar_a_fala() -> None:
        registo = registo_atual()
        if registo is not None:
            registo.marcar_fim_da_fala()

    recorder = construir_recorder(
        use_microphone=True,
        com_wake_word=True,
        device=device,
        modelo=modelo,
        callbacks={
            "on_wakeword_detected": ao_detetar_wake_word,
            "on_wakeword_timeout": ao_expirar_wake_word,
            "on_recording_start": ao_comecar_a_fala,
            "on_recording_stop": ao_acabar_a_fala,
        },
    )
    log.bruto("")
    log.bruto("=" * LARGURA_DA_SEPARACAO)
    log.bruto("   JARVIS ESTA A OUVIR   -   diz:  \"hey jarvis, que horas sao?\"")
    log.bruto(f"   microfone LIGADO   |   transcricao em {getattr(recorder, 'device', '?')}")
    log.bruto("   fechar esta janela (ou Ctrl+C) cala a voz e desliga o microfone (D30/D60)")
    log.bruto("=" * LARGURA_DA_SEPARACAO)
    novo_registo()
    try:
        while True:
            texto = (recorder.text() or "").strip()
            # Pode nao ser o registo com que a volta comecou: um falso
            # despertar (wake word sem frase) fecha o anterior e abre outro.
            registo = registo_atual()
            if registo is None:  # defensivo: nunca acontece com novo_registo()
                registo = novo_registo()
            if not registo.marcas:
                registo.marcar(1, "sem palavra de ativacao registada nesta frase")
            registo.marcar(2, detalhe_da_transcricao(recorder, texto), desde=registo.fim_da_fala)
            jarvis.tratar_transcricao(texto, registo)
            novo_registo()
            log.bruto(">>> A OUVIR - diz \"hey jarvis\" <<<")
    except KeyboardInterrupt:
        log.bruto("")
        # PRIMEIRO calar (D60): matar a sintese em curso e parar a reproducao,
        # ANTES do `recorder.shutdown()` do `finally`, que demora segundos. Era
        # aqui que o jarvis so desligava o microfone e continuava a falar.
        jarvis.calar_agora("Ctrl+C (D30/D60)", definitivo=True, ja_calado=voz.esta_calado())
        log.linha("Ctrl+C: voz calada, a desligar o microfone e a fechar o jarvis (D30/D60)")
        return 0
    finally:
        with silenciar_ruido_do_shutdown():
            recorder.shutdown()
        jarvis.fechar()


# --- Arranque ---------------------------------------------------------------


def carregar_config_tolerante(caminho: Path, log: LogDaSessao) -> Config:
    """A config privada, ou uma config vazia com o aviso escrito no log.

    Sem `config.toml` o jarvis continua a servir os comandos que nao precisam
    de projeto (horas, data, calar, adormecer, acordar); "abre o VS Code no
    <projeto>" deixa de ter projetos por onde escolher e, pela D4, vai como
    texto para o Claude Code em vez de adivinhar — nunca "o mais parecido".
    """
    try:
        return carregar_config(caminho)
    except ConfigError as erro:
        log.linha(
            f"AVISO: configuracao privada por carregar ({erro}). O jarvis segue SEM projetos: "
            "os comandos com projeto nao viram accao local (D4)."
        )
        return Config(microfone="", projetos=())


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jarvis.app",
        description=__doc__.splitlines()[0],
    )
    parser.add_argument(
        "--wav",
        metavar="FICHEIRO",
        help="injeta um WAV no mesmo pipeline em vez de abrir o microfone (D33)",
    )
    parser.add_argument(
        "--exigir-wake-word",
        action="store_true",
        help=(
            "no modo --wav, fecha a porta da palavra de ativacao: o pipeline so transcreve "
            "depois de o oww disparar a partir do ficheiro"
        ),
    )
    parser.add_argument(
        "--prova-wake-word",
        metavar="FICHEIRO",
        help="corre so o modelo openWakeWord sobre um WAV e imprime o que mediu",
    )
    parser.add_argument(
        "--config",
        default=None,
        metavar="FICHEIRO",
        help="ficheiro de configuracao privada (por omissao: config.toml na raiz)",
    )
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    parser.add_argument(
        "--modelo",
        default=MODELO_STT,
        choices=MODELOS_STT_PERMITIDOS,
        help="modelo do faster-whisper (por omissao: medium, D39/S3)",
    )
    parser.add_argument(
        "--sem-voz",
        action="store_true",
        help="nao usa o Piper: as respostas saem so na consola e no log",
    )
    parser.add_argument(
        "--autoteste",
        action="store_true",
        help="corre a regressao das partes puras (sem GPU, sem microfone, sem Piper)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)

    if args.autoteste:
        return _autoteste()

    if args.prova_wake_word:
        medido = prova_da_wake_word(Path(args.prova_wake_word))
        print("=== jarvis - prova da etapa 1 (openWakeWord hey_jarvis) ===")
        print(f"ficheiro    = {medido['ficheiro']}")
        print(f"chunks      = {medido['chunks']} de {BYTES_POR_CHUNK} bytes")
        print(f"limiar      = {medido['sensibilidade']}")
        print(f"score maximo= {medido['score_maximo']:.4f}")
        print(f"chunks acima do limiar = {medido['chunks_acima_do_limiar']}")
        print("DETETADA" if medido["detetou"] else "NAO DETETADA")
        return 0 if medido["detetou"] else 1

    log = LogDaSessao()
    log.linha(f"jarvis a arrancar | log em {log.caminho}")
    config = carregar_config_tolerante(
        Path(args.config) if args.config else CAMINHO_CONFIG_PADRAO, log
    )
    log.linha(
        f"configuracao: {len(config.projetos)} projeto(s) conhecido(s) | "
        f"voz={'ligada' if not args.sem_voz else 'desligada'} | device pedido={args.device}"
    )
    jarvis = Jarvis(config, log, com_voz=not args.sem_voz)
    # Ultima rede da saida do processo (D60(2)): mesmo que o processo acabe por
    # um caminho que nao passe pelo `finally` abaixo, nao fica um `piper.exe`
    # vivo a falar. Sem `registar`: nesse ponto o log ja pode estar fechado.
    atexit.register(voz.calar_agora, "saida do processo (atexit)", definitivo=True)

    def ao_ctrl_c(numero_do_sinal, _quadro):
        """Cala ANTES de a excecao desenrolar a pilha (D60).

        Sem isto, o `KeyboardInterrupt` sobe primeiro por dentro de
        `jarvis.voz.falar()`, que larga o registo da voz ativa no seu
        `finally` — e o handler la em baixo ja nao teria o `piper.exe` a mao
        para matar. Um handler de sinal corre na thread principal, entre
        bytecodes, que e o instante mais cedo possivel.
        """
        jarvis.calar_agora("Ctrl+C (D30/D60)", definitivo=True)
        raise KeyboardInterrupt

    try:
        handler_anterior = signal.signal(signal.SIGINT, ao_ctrl_c)
    except ValueError:
        # Fora da thread principal nao ha handlers de sinal. O Ctrl+C continua
        # a ser apanhado pelo `except KeyboardInterrupt` de `correr_microfone`
        # e pelo `finally` daqui: so se perde o instante mais cedo.
        handler_anterior = None
    try:
        if args.wav:
            return correr_wav(
                Path(args.wav),
                jarvis,
                exigir_wake_word=args.exigir_wake_word,
                device=args.device,
                modelo=args.modelo,
            )
        return correr_microfone(jarvis, device=args.device, modelo=args.modelo)
    finally:
        if handler_anterior is not None:
            signal.signal(signal.SIGINT, handler_anterior)
        # Gatilho 3 (D60(2)): a saida do processo cala pelo MESMO mecanismo. Se
        # o Ctrl+C ja calou, isto corre na mesma (nao ha nada para matar) mas
        # sem repetir as duas linhas no log.
        jarvis.calar_agora("saida do processo", definitivo=True, ja_calado=voz.esta_calado())
        log.linha("jarvis terminado")
        log.fechar()


# --- Autoteste das partes puras (sem GPU, sem microfone, sem Piper) --------


def _autoteste() -> int:
    """Verifica o formato do log, a aritmetica das latencias e o corte do WAV."""
    import tempfile

    from jarvis.audio_util import escrever_wav_pcm16

    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    verificar(
        "formato da etapa",
        formatar_etapa(3, 2, 812.4, "texto: 'que horas sao'"),
        "frase #3 | etapa 2/5 transcricao           |     812 ms | texto: 'que horas sao'",
    )
    verificar(
        "timestamp com milissegundos",
        agora_iso(datetime.datetime(2026, 9, 20, 6, 12, 1, 123456)),
        "2026-09-20 06:12:01.123",
    )
    verificar(
        "nome do ficheiro de log",
        caminho_do_log(datetime.datetime(2026, 9, 20), Path("logs")).name,
        "jarvis-2026-09-20.log",
    )

    tempos = iter([0.0, 1.0, 1.5])
    registo = RegistoDaFrase(numero=1, relogio=lambda: next(tempos))
    verificar("latencia da primeira etapa", round(registo.marcar(1, "x").latencia_ms), 1000)
    verificar("latencia da segunda etapa", round(registo.marcar(2, "y").latencia_ms), 500)

    with tempfile.TemporaryDirectory() as pasta:
        caminho = Path(pasta) / "t.wav"
        escrever_wav_pcm16(caminho, b"\x01\x00" * 1000, 16000, 1)
        pedacos = list(frames_do_wav(caminho))
        verificar("chunks de um WAV de 1000 amostras", len(pedacos), 2)
        verificar("ultimo chunk completado com silencio", len(pedacos[-1]), BYTES_POR_CHUNK)
        verificar("silencio de 1 s", len(list(chunks_de_silencio(1.0))), 31)

    if falhas:
        print("\nFALHAS:")
        for falha in falhas:
            print(f"  - {falha}")
        return 1
    print("\nOK: autoteste das partes puras do orquestrador.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
