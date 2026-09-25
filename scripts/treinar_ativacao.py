r"""Treina localmente o modelo openWakeWord da palavra de ativacao portuguesa, "boas jarvis".

So e preciso com o jarvis em portugues ([ouvido] lingua = "pt"): em ingles usa-se
o modelo pre-treinado "hey jarvis". Nada e descarregado nem instalado: o treino
usa so o que ja esta no .venv e em models/.

  caracteristicas  as do proprio openWakeWord (models/openwakeword/
                   melspectrogram.onnx + embedding_model.onnx): janelas de
                   2 s -> 16 x 96, exatamente o que o detetor do ouvido da
                   ao modelo em cada passo de 80 ms.
  positivos        (1) as 20 repeticoes do CONJUNTO DE TREINO do Sponsor
                   (recordings/ativacao/pt-treino/, gravadas com
                   `scripts/avaliar_ativacao.py --gravar-palavra --lingua pt
                   --conjunto treino`), cada uma aumentada varias vezes
                   (ganho, ruido, posicao na janela); (2) "boas jarvis"
                   sintetizado pela voz Piper pt_PT de models/piper/ com
                   velocidade, entoacao e ruido variados.
  negativos        frases portuguesas sintetizadas (as do guiao de voz real,
                   sem a palavra de ativacao, palavras parecidas: "boas",
                   "jarvis", "boa noite"..., e conversa de casa), as gravacoes do Sponsor do
                   guiao de voz real que NAO comecam pela palavra de ativacao,
                   e ruido gerado (branco, rosa, castanho).
  modelo           um MLP pequeno (scikit-learn) sobre as caracteristicas,
                   com a normalizacao dobrada nos pesos, escrito em ONNX
                   (Flatten -> Gemm -> Relu -> ... -> Sigmoid) por um codificador
                   protobuf minimo deste script, porque o pacote `onnx` nao
                   esta no .venv. O ficheiro e verificado com onnxruntime
                   contra o scikit-learn e carregado pelo detetor do ouvido
                   antes de ser dado como pronto.

O conjunto de AVALIACAO (recordings/ativacao/pt/) e o ruido de
recordings/ativacao/ruido/ NUNCA entram no treino: sao eles que medem o modelo
em `scripts/avaliar_ativacao.py --lingua pt`, e usa-los aqui inflacionava os
numeros.

Saida: models/openwakeword/boas_jarvis.onnx e, ao lado, boas_jarvis.onnx.json
(o cartao: datas, contagens, parametros, semente, versoes e sha256; sem
transcricoes nem caminhos). models/ esta no .gitignore. O sha256 impresso
regista-se em docs/MODELOS.md.

Sem as 20 repeticoes de treino do Sponsor o script recusa-se a treinar.
`--ensaio-sintetico` treina so com voz sintetica para provar a cadeia, mas
nunca para o caminho do modelo real (um modelo assim nao diz nada sobre a voz
do Sponsor).

Uso:
    .venv\Scripts\python scripts/treinar_ativacao.py
    .venv\Scripts\python scripts/treinar_ativacao.py --ensaio-sintetico --saida <pasta-fora-do-repo>\ensaio.onnx
    .venv\Scripts\python scripts/treinar_ativacao.py --autoteste
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import struct
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.audio_util import PASTA_MODELOS_PIPER, TAXA_AMOSTRAGEM_PADRAO, reamostrar_pcm16  # noqa: E402
from jarvis.consola import forcar_consola_utf8  # noqa: E402
from jarvis.ouvido import (  # noqa: E402
    MODELO_EMBEDDING,
    MODELO_MELSPEC,
    MODELOS_DE_ATIVACAO,
    PALAVRAS_DE_ATIVACAO,
    retirar_palavra_de_ativacao,
)

PASTA_SCRIPTS = Path(__file__).resolve().parent


def _carregar_modulo_irmao(nome: str):
    """Importa `scripts/<nome>.py` por caminho, com a chave de cache dos outros arneses."""
    chave = f"_jarvis_scripts_{nome}"
    ja_carregado = sys.modules.get(chave)
    if ja_carregado is not None:
        return ja_carregado
    caminho = PASTA_SCRIPTS / f"{nome}.py"
    spec = importlib.util.spec_from_file_location(chave, caminho)
    if spec is None or spec.loader is None:
        raise ImportError(f"nao foi possivel carregar o modulo irmao '{caminho}'")
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[chave] = modulo
    try:
        spec.loader.exec_module(modulo)
    except BaseException:
        del sys.modules[chave]
        raise
    return modulo


avaliar = _carregar_modulo_irmao("avaliar_ativacao")
gravar_voz = avaliar.gravar_voz

LINGUA = "pt"
PALAVRA = PALAVRAS_DE_ATIVACAO[LINGUA]
MODELO_DE_SAIDA = MODELOS_DE_ATIVACAO[LINGUA]
PASTA_MODELOS = RAIZ / "models"
VOZ_PIPER = PASTA_MODELOS_PIPER / "pt_PT-tugao-medium.onnx"

TAXA = TAXA_AMOSTRAGEM_PADRAO
#: 2 s de audio dao as 16 janelas de caracteristicas que o modelo ve.
AMOSTRAS_DA_JANELA = 2 * TAXA
JANELAS = 16
DIMENSAO = 96
#: O fim da palavra fica ate isto antes do fim da janela (o detetor dispara
#: logo a seguir a palavra acabar).
FOLGA_DO_FIM_S = 0.3

SEMENTE = 1790
AUMENTOS_POR_GRAVACAO = 15
AUMENTOS_POR_SINTETICA = 2
SINTETICAS_POSITIVAS = 300
RECORTES_POR_GRAVACAO_NEGATIVA = 3
JANELAS_DE_RUIDO = 600
FRACAO_DE_VALIDACAO = 0.2
#: Janelas sem ruido de fundo nenhum (como o silencio entre frases no microfone).
FRACAO_SEM_RUIDO = 0.2
#: Negativos falados por cada positivo sintetizado.
NEGATIVOS_POR_POSITIVO = 2
CAMADAS_ESCONDIDAS = (64,)

#: Textos da palavra para a voz sintetica (a pontuacao muda a entoacao).
TEXTOS_POSITIVOS = ("boas jarvis", "boas, jarvis", "boas jarvis!", "Boas, Jarvis.", "boas jarvis?")
#: Palavras e frases parecidas que NAO sao a palavra de ativacao.
TEXTOS_PARECIDOS = (
    "boas", "jarvis", "olá jarvis", "o jarvis", "abre o jarvis", "boas notícias", "boas festas",
    "boa tarde", "boas vindas", "bom dia", "vamos lá", "obrigado jarvis", "boas ideias", "jarvis, pára",
    "boa noite", "boas noites", "boa sorte", "boas férias", "boa viagem", "o jarvis é um assistente",
    "boas notícias do trabalho", "boa noite a todos",
)
#: Conversa de casa, fora do guiao de comandos (o ruido que mais desperta).
TEXTOS_DO_DIA_A_DIA = (
    "o jantar está pronto", "vou ao mercado comprar pão", "a reunião ficou para amanhã",
    "já viste as horas", "liga-me quando chegares", "está a chover lá fora", "onde puseste as chaves",
    "obrigado pela ajuda", "tudo bem contigo", "fecha a janela por favor",
)
#: Vezes que cada frase parecida e sintetizada (com entoacao diferente de cada vez).
REPETICOES_DOS_PARECIDOS = 3


# --- ONNX minimo: codificador protobuf ----------------------------------------------

ONNX_FLOAT = 1


def _varint(valor: int) -> bytes:
    if valor < 0:
        valor += 1 << 64
    saida = bytearray()
    while True:
        byte = valor & 0x7F
        valor >>= 7
        if valor:
            saida.append(byte | 0x80)
        else:
            saida.append(byte)
            return bytes(saida)


def _campo_int(numero: int, valor: int) -> bytes:
    return _varint(numero << 3) + _varint(valor)


def _campo_bytes(numero: int, valor: bytes | str) -> bytes:
    dados = valor.encode("utf-8") if isinstance(valor, str) else valor
    return _varint((numero << 3) | 2) + _varint(len(dados)) + dados


def _tensor(nome: str, matriz) -> bytes:
    import numpy as np

    dados = np.ascontiguousarray(matriz, dtype="<f4")
    return (
        b"".join(_campo_int(1, d) for d in dados.shape)
        + _campo_int(2, ONNX_FLOAT)
        + _campo_bytes(8, nome)
        + _campo_bytes(9, dados.tobytes())
    )


def _no(tipo: str, entradas: Sequence[str], saida: str) -> bytes:
    return (
        b"".join(_campo_bytes(1, e) for e in entradas)
        + _campo_bytes(2, saida)
        + _campo_bytes(3, f"{tipo}_{saida}")
        + _campo_bytes(4, tipo)
    )


def _valor(nome: str, forma: Sequence[int | str]) -> bytes:
    dimensoes = b"".join(
        _campo_bytes(1, _campo_bytes(2, d) if isinstance(d, str) else _campo_int(1, d)) for d in forma
    )
    tensor = _campo_int(1, ONNX_FLOAT) + _campo_bytes(2, dimensoes)
    return _campo_bytes(1, nome) + _campo_bytes(2, _campo_bytes(1, tensor))


def onnx_do_mlp(pesos: Sequence, vieses: Sequence, janelas: int = JANELAS, dimensao: int = DIMENSAO) -> bytes:
    """ModelProto de x[N, janelas, dimensao] -> Flatten -> (Gemm, Relu)* -> Gemm -> Sigmoid -> y[N, 1]."""
    nos = [_no("Flatten", ["x"], "h0")]
    iniciais = []
    atual = "h0"
    for indice, (peso, vies) in enumerate(zip(pesos, vieses), start=1):
        iniciais += [_tensor(f"W{indice}", peso), _tensor(f"b{indice}", vies)]
        saida = f"g{indice}"
        nos.append(_no("Gemm", [atual, f"W{indice}", f"b{indice}"], saida))
        if indice < len(pesos):
            nos.append(_no("Relu", [saida], f"r{indice}"))
            atual = f"r{indice}"
        else:
            atual = saida
    nos.append(_no("Sigmoid", [atual], "y"))
    grafo = (
        b"".join(_campo_bytes(1, n) for n in nos)
        + _campo_bytes(2, "palavra_de_ativacao")
        + b"".join(_campo_bytes(5, t) for t in iniciais)
        + _campo_bytes(11, _valor("x", ["N", janelas, dimensao]))
        + _campo_bytes(12, _valor("y", ["N", 1]))
    )
    return (
        _campo_int(1, 8)  # ir_version
        + _campo_bytes(2, "jarvis-treinar-ativacao")
        + _campo_bytes(7, grafo)
        + _campo_bytes(8, _campo_bytes(1, "") + _campo_int(2, 13))  # opset 13
    )


def pesos_do_mlp(classificador, media, desvio) -> tuple[list, list]:
    """Os pesos do MLP com a normalizacao (x - media) / desvio dobrada na primeira camada."""
    import numpy as np

    pesos = [np.asarray(w, dtype=np.float64) for w in classificador.coefs_]
    vieses = [np.asarray(b, dtype=np.float64) for b in classificador.intercepts_]
    pesos[0] = pesos[0] / desvio[:, None]
    vieses[0] = vieses[0] - (media / desvio) @ (pesos[0] * desvio[:, None])
    return pesos, vieses


def prever_onnx(caminho_ou_bytes, x) -> "object":
    import numpy as np
    import onnxruntime

    fonte = caminho_ou_bytes if isinstance(caminho_ou_bytes, (bytes, bytearray)) else str(caminho_ou_bytes)
    sessao = onnxruntime.InferenceSession(fonte, providers=["CPUExecutionProvider"])
    return sessao.run(None, {"x": np.asarray(x, dtype=np.float32)})[0][:, 0]


# --- Audio: recortes, aumento, ruido --------------------------------------------------


def para_float(pcm16: bytes):
    import numpy as np

    return np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0


def para_int16(audio):
    import numpy as np

    return (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)


def zona_de_fala(audio, taxa: int = TAXA) -> tuple[int, int]:
    """[inicio, fim) da fala por energia em blocos de 10 ms (limiar relativo ao pico)."""
    import numpy as np

    bloco = taxa // 100
    n = len(audio) // bloco
    if n == 0:
        return 0, len(audio)
    energia = np.sqrt((audio[: n * bloco].reshape(n, bloco) ** 2).mean(axis=1))
    chao = np.percentile(energia, 10)
    limiar = max(chao * 3, energia.max() * 0.1)
    ativos = np.flatnonzero(energia >= limiar)
    if ativos.size == 0:
        return 0, len(audio)
    return int(ativos[0] * bloco), int(min(len(audio), (ativos[-1] + 1) * bloco))


def ruido_gerado(rng, amostras: int, tipo: str):
    """Ruido branco, rosa ou castanho, com pico 1."""
    import numpy as np

    branco = rng.standard_normal(amostras)
    if tipo == "rosa":
        espectro = np.fft.rfft(branco)
        frequencias = np.arange(1, len(espectro) + 1)
        sinal = np.fft.irfft(espectro / np.sqrt(frequencias), n=amostras)
    elif tipo == "castanho":
        sinal = np.cumsum(branco)
        sinal -= np.linspace(sinal[0], sinal[-1], amostras)
    else:
        sinal = branco
    return (sinal / (np.abs(sinal).max() or 1.0)).astype(np.float32)


TIPOS_DE_RUIDO = ("branco", "rosa", "castanho")


def janela_com_palavra(rng, palavra, *, alinhar_ao_fim: bool = True):
    """Uma janela de 2 s com `palavra` (float) colocada, ruido de fundo e ganho aleatorios."""
    import numpy as np

    janela = np.zeros(AMOSTRAS_DA_JANELA, dtype=np.float32)
    palavra = palavra[-AMOSTRAS_DA_JANELA:]
    if alinhar_ao_fim:
        folga = int(rng.uniform(0.0, FOLGA_DO_FIM_S) * TAXA)
        fim = AMOSTRAS_DA_JANELA - folga
    else:
        fim = int(rng.uniform(len(palavra), AMOSTRAS_DA_JANELA + 1)) if len(palavra) < AMOSTRAS_DA_JANELA else AMOSTRAS_DA_JANELA
    inicio = max(0, fim - len(palavra))
    janela[inicio:fim] = palavra[len(palavra) - (fim - inicio):]
    pico = np.abs(janela).max() or 1.0
    janela *= rng.uniform(0.1, 0.9) / pico
    if rng.uniform() < FRACAO_SEM_RUIDO:
        return janela
    snr_db = rng.uniform(5.0, 30.0)
    ruido = ruido_gerado(rng, AMOSTRAS_DA_JANELA, TIPOS_DE_RUIDO[rng.integers(len(TIPOS_DE_RUIDO))])
    potencia = float(np.mean(janela**2)) or 1e-8
    ruido *= np.sqrt(potencia / (10 ** (snr_db / 10)) / (float(np.mean(ruido**2)) or 1e-8))
    return janela + ruido


def recorte(rng, audio):
    """2 s tirados de um audio qualquer (completado com zeros se for curto)."""
    import numpy as np

    if len(audio) <= AMOSTRAS_DA_JANELA:
        janela = np.zeros(AMOSTRAS_DA_JANELA, dtype=np.float32)
        inicio = int(rng.integers(0, AMOSTRAS_DA_JANELA - len(audio) + 1))
        janela[inicio : inicio + len(audio)] = audio
        return janela
    inicio = int(rng.integers(0, len(audio) - AMOSTRAS_DA_JANELA + 1))
    return audio[inicio : inicio + AMOSTRAS_DA_JANELA].copy()


# --- Fontes de audio -------------------------------------------------------------------


@dataclass
class Amostra:
    #: Grupo de origem: todas as janelas de uma mesma gravacao ou sintese
    #: ficam do mesmo lado da divisao treino/validacao.
    origem: str
    rotulo: int
    janela: object


class Sintetizador:
    """A voz Piper pt_PT de models/piper/, dentro do processo, com parametros variaveis."""

    def __init__(self, modelo: Path = VOZ_PIPER) -> None:
        if not Path(modelo).is_file():
            raise FileNotFoundError(f"voz Piper em falta: '{avaliar.mostrar(modelo)}' (ver docs/MODELOS.md)")
        from piper import PiperVoice, SynthesisConfig

        self._voz = PiperVoice.load(str(modelo), f"{modelo}.json")
        self._config = SynthesisConfig

    def sintetizar(self, texto: str, rng) -> object:
        config = self._config(
            length_scale=float(rng.uniform(0.8, 1.3)),
            noise_scale=float(rng.uniform(0.4, 0.9)),
            noise_w_scale=float(rng.uniform(0.5, 1.0)),
        )
        blocos = list(self._voz.synthesize(texto, syn_config=config))
        pcm = b"".join(b.audio_int16_bytes for b in blocos)
        taxa = blocos[0].sample_rate if blocos else TAXA
        return para_float(reamostrar_pcm16(pcm, taxa, TAXA))


def frases_negativas_do_guiao() -> list[str]:
    """As frases do guiao de voz real, sem a palavra de ativacao e com nomes ficticios."""
    frases = gravar_voz.ler_guiao(gravar_voz.GUIOES[LINGUA], LINGUA)
    textos = []
    for frase in frases:
        texto = frase.frase.replace("<projeto-1>", "exemplo um").replace("<projeto-2>", "exemplo dois")
        textos.append(retirar_palavra_de_ativacao(texto, [PALAVRA], aceitar_cauda=False)[0])
    return textos


def textos_negativos(rng, quantos: int) -> list[str]:
    """As frases parecidas (varias vezes), a conversa de casa e `quantos` pedacos de 1 a 4 palavras do guiao.

    Muitos negativos falados pela MESMA voz sintetica dos positivos obrigam o
    modelo a aprender a palavra e nao a voz.
    """
    pedacos: set[str] = set()
    for frase in frases_negativas_do_guiao():
        palavras = frase.replace(",", " ").replace(":", " ").split()
        for tamanho in range(1, 5):
            for inicio in range(len(palavras) - tamanho + 1):
                pedaco = " ".join(palavras[inicio : inicio + tamanho])
                if retirar_palavra_de_ativacao(pedaco + " x", [PALAVRA], aceitar_cauda=False)[1] is None:
                    pedacos.add(pedaco)
    ordenados = sorted(pedacos)
    escolhidos = [ordenados[i] for i in rng.permutation(len(ordenados))[:quantos]] if ordenados else []
    return list(TEXTOS_PARECIDOS) * REPETICOES_DOS_PARECIDOS + list(TEXTOS_DO_DIA_A_DIA) + escolhidos


def gravacoes_do_sponsor(pasta_base_gravacoes: Path) -> tuple[list[tuple[str, object]], list[tuple[str, object]]]:
    """(positivos do conjunto de treino, negativos do guiao de voz real sem a palavra).

    So entram gravacoes que o manifesto diz virem do microfone.
    """
    pasta_treino = pasta_base_gravacoes / avaliar.NOME_PASTA_ATIVACAO / avaliar.nome_da_pasta_da_palavra(
        LINGUA, avaliar.CONJUNTO_TREINO
    )
    ids_treino = [avaliar.id_da_repeticao(LINGUA, n, avaliar.CONJUNTO_TREINO) for n, _ in avaliar.ler_guiao()]
    positivos, _ = avaliar.gravacoes_validas(pasta_treino, ids_treino)
    frases = gravar_voz.ler_guiao(gravar_voz.GUIOES[LINGUA], LINGUA)
    sem_palavra = [f.id for f in frases if not f.ativacao]
    negativos, _ = avaliar.gravacoes_validas(pasta_base_gravacoes / LINGUA, sem_palavra)
    carregar = avaliar.pcm_mono_16k
    return (
        [(i, para_float(carregar(c))) for i, c in positivos],
        [(i, para_float(carregar(c))) for i, c in negativos],
    )


def montar_amostras(
    rng,
    positivos_reais: Sequence[tuple[str, object]],
    negativos_reais: Sequence[tuple[str, object]],
    sintetizar: Callable[[str, object], object] | None,
    *,
    sinteticas_positivas: int = SINTETICAS_POSITIVAS,
    textos_negativos: Sequence[str] = (),
    janelas_de_ruido: int = JANELAS_DE_RUIDO,
    escrever: Callable[[str], object] = print,
) -> tuple[list[Amostra], dict]:
    contagem = {
        "positivos_reais": len(positivos_reais),
        "positivos_sinteticos": 0,
        "negativos_reais": len(negativos_reais),
        "negativos_sinteticos": 0,
        "janelas_de_ruido": janelas_de_ruido,
    }
    amostras: list[Amostra] = []
    for id_, audio in positivos_reais:
        inicio, fim = zona_de_fala(audio)
        palavra = audio[max(0, inicio - TAXA // 20) : fim + TAXA // 20]
        amostras += [Amostra(id_, 1, janela_com_palavra(rng, palavra)) for _ in range(AUMENTOS_POR_GRAVACAO)]
    for id_, audio in negativos_reais:
        amostras += [Amostra(id_, 0, recorte(rng, audio)) for _ in range(RECORTES_POR_GRAVACAO_NEGATIVA)]
    if sintetizar is not None:
        for indice in range(sinteticas_positivas):
            audio = sintetizar(TEXTOS_POSITIVOS[indice % len(TEXTOS_POSITIVOS)], rng)
            inicio, fim = zona_de_fala(audio)
            palavra = audio[inicio:fim]
            amostras += [Amostra(f"sint-pos-{indice}", 1, janela_com_palavra(rng, palavra))
                         for _ in range(AUMENTOS_POR_SINTETICA)]
            contagem["positivos_sinteticos"] += 1
            if (indice + 1) % 100 == 0:
                escrever(f"  sintetizados {indice + 1} de {sinteticas_positivas} positivos")
        for indice, texto in enumerate(textos_negativos):
            audio = sintetizar(texto, rng)
            inicio, fim = zona_de_fala(audio)
            fala = audio[inicio:fim]
            amostras.append(Amostra(f"sint-neg-{indice}", 0, janela_com_palavra(rng, fala)))
            amostras.append(Amostra(f"sint-neg-{indice}", 0, janela_com_palavra(rng, fala, alinhar_ao_fim=False)))
            contagem["negativos_sinteticos"] += 1
    import numpy as np

    for indice in range(janelas_de_ruido):
        ruido = ruido_gerado(rng, AMOSTRAS_DA_JANELA, TIPOS_DE_RUIDO[indice % len(TIPOS_DE_RUIDO)])
        amostras.append(Amostra(f"ruido-{indice}", 0, ruido * np.float32(rng.uniform(0.001, 0.3))))
    return amostras, contagem


# --- Caracteristicas e treino -----------------------------------------------------------


def caracteristicas_openwakeword(janelas) -> object:
    """(N, 32000) float -> (N, 16, 96) pelos modelos de caracteristicas do openWakeWord."""
    import numpy as np
    from openwakeword.utils import AudioFeatures

    extrator = AudioFeatures(
        melspec_model_path=str(MODELO_MELSPEC), embedding_model_path=str(MODELO_EMBEDDING), inference_framework="onnx"
    )
    x = np.stack([para_int16(j) for j in janelas])
    return extrator.embed_clips(x, batch_size=64)[:, -JANELAS:, :]


def dividir_por_origem(rng, amostras: Sequence[Amostra], fracao: float = FRACAO_DE_VALIDACAO) -> tuple[list[int], list[int]]:
    origens = sorted({a.origem for a in amostras})
    validacao = {o for o in origens if rng.uniform() < fracao}
    treino = [i for i, a in enumerate(amostras) if a.origem not in validacao]
    valida = [i for i, a in enumerate(amostras) if a.origem in validacao]
    return treino, valida


@dataclass
class Treinado:
    pesos: list
    vieses: list
    metricas: dict


def treinar(x, y, indices_treino, indices_validacao, semente: int = SEMENTE) -> Treinado:
    """MLP com as classes equilibradas por repeticao dos positivos; devolve pesos ja normalizados."""
    import numpy as np
    from sklearn.neural_network import MLPClassifier

    plano = x.reshape(len(x), -1).astype(np.float64)
    treino = np.asarray(indices_treino)
    media = plano[treino].mean(axis=0)
    desvio = plano[treino].std(axis=0) + 1e-6
    positivos = treino[y[treino] == 1]
    negativos = treino[y[treino] == 0]
    if len(positivos) == 0 or len(negativos) == 0:
        raise ValueError("o treino precisa de positivos e de negativos")
    repeticoes = max(1, round(len(negativos) / len(positivos)))
    equilibrado = np.concatenate([negativos] + [positivos] * repeticoes)
    classificador = MLPClassifier(
        hidden_layer_sizes=CAMADAS_ESCONDIDAS, alpha=1e-3, max_iter=300, random_state=semente
    )
    classificador.fit((plano[equilibrado] - media) / desvio, y[equilibrado])
    pesos, vieses = pesos_do_mlp(classificador, media, desvio)
    metricas: dict = {"iteracoes": int(classificador.n_iter_)}
    if len(indices_validacao):
        valida = np.asarray(indices_validacao)
        prob = classificador.predict_proba((plano[valida] - media) / desvio)[:, 1]
        pos, neg = y[valida] == 1, y[valida] == 0
        metricas.update({
            "validacao_positivos": int(pos.sum()),
            "validacao_negativos": int(neg.sum()),
            "detecao_a_0_5": float((prob[pos] >= 0.5).mean()) if pos.any() else None,
            "falsos_a_0_5": float((prob[neg] >= 0.5).mean()) if neg.any() else None,
        })
    return Treinado(pesos, vieses, metricas)


def forward_numpy(pesos, vieses, x):
    import numpy as np

    h = np.asarray(x, dtype=np.float64).reshape(len(x), -1)
    for indice, (w, b) in enumerate(zip(pesos, vieses)):
        h = h @ w + b
        if indice < len(pesos) - 1:
            h = np.maximum(h, 0)
    return 1 / (1 + np.exp(-h[:, 0]))


def escrever_modelo(treinado: Treinado, saida: Path, x_verificacao) -> tuple[str, float]:
    """Escreve o ONNX, confirma com onnxruntime que da o mesmo que o MLP e devolve (sha256, erro maximo)."""
    import numpy as np

    dados = onnx_do_mlp(treinado.pesos, treinado.vieses)
    erro = float(np.max(np.abs(prever_onnx(dados, x_verificacao) - forward_numpy(treinado.pesos, treinado.vieses, x_verificacao))))
    if erro > 1e-3:
        raise RuntimeError(f"o ONNX escrito difere do MLP treinado (erro maximo {erro:.2e}); nada foi guardado")
    saida.parent.mkdir(parents=True, exist_ok=True)
    temporario = saida.with_suffix(".onnx.tmp")
    temporario.write_bytes(dados)
    temporario.replace(saida)
    return hashlib.sha256(dados).hexdigest(), erro


def caminho_de_saida(valor: str | Path | None, ensaio: bool) -> Path:
    """Dentro do repositorio so em models/ (ignorada); o ensaio nunca vai para o modelo real."""
    caminho = Path(valor).expanduser() if valor else MODELO_DE_SAIDA
    if not caminho.is_absolute():
        caminho = RAIZ / caminho
    caminho = caminho.resolve()
    if caminho.suffix.lower() != ".onnx":
        raise ValueError("--saida tem de acabar em .onnx")
    if caminho.is_relative_to(RAIZ.resolve()) and not caminho.is_relative_to(PASTA_MODELOS.resolve()):
        raise ValueError("--saida dentro do repositorio so em models/ (a unica pasta ignorada para modelos)")
    if ensaio and caminho == MODELO_DE_SAIDA.resolve():
        raise ValueError("um ensaio so com voz sintetica nunca se grava como o modelo real; usar --saida")
    return caminho


# --- CLI ---------------------------------------------------------------------------------


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"Treina o modelo openWakeWord de '{PALAVRA}' localmente.")
    parser.add_argument("--saida", default=None, help=f"ficheiro .onnx (por omissao {avaliar.mostrar(MODELO_DE_SAIDA)})")
    parser.add_argument("--ensaio-sintetico", action="store_true",
                        help="treina sem a voz do Sponsor so para provar a cadeia (nunca o modelo real)")
    parser.add_argument("--sinteticas", type=int, default=SINTETICAS_POSITIVAS, help="positivos sintetizados")
    parser.add_argument("--semente", type=int, default=SEMENTE)
    parser.add_argument("--autoteste", action="store_true", help="dados falsos; sem Piper, sem microfone, sem som")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)
    if args.autoteste:
        return _autoteste()
    import numpy as np

    try:
        saida = caminho_de_saida(args.saida, args.ensaio_sintetico)
    except ValueError as erro:
        print(f"erro: {erro}", file=sys.stderr)
        return 2
    positivos, negativos = gravacoes_do_sponsor(RAIZ / gravar_voz.NOME_PASTA_GRAVACOES)
    if len(positivos) < avaliar.REPETICOES and not args.ensaio_sintetico:
        print(
            f"PENDENTE — passo do Sponsor: {len(positivos)} de {avaliar.REPETICOES} repeticoes de treino validas. "
            f"Gravar com: {avaliar.comando('--gravar-palavra --lingua pt --conjunto treino')}",
            file=sys.stderr,
        )
        return 1
    if args.ensaio_sintetico:
        positivos, negativos = [], []
    rng = np.random.default_rng(args.semente)
    inicio = time.perf_counter()
    print(f"voz do Sponsor: {len(positivos)} repeticoes de treino, {len(negativos)} frases sem a palavra")
    sintetizador = Sintetizador()
    amostras, contagem = montar_amostras(
        rng, positivos, negativos, sintetizador.sintetizar, sinteticas_positivas=args.sinteticas,
        textos_negativos=textos_negativos(rng, NEGATIVOS_POR_POSITIVO * args.sinteticas),
    )
    print(f"{len(amostras)} janelas; a extrair caracteristicas do openWakeWord...")
    x = caracteristicas_openwakeword([a.janela for a in amostras])
    y = np.array([a.rotulo for a in amostras])
    indices_treino, indices_validacao = dividir_por_origem(rng, amostras)
    treinado = treinar(x, y, indices_treino, indices_validacao, args.semente)
    sha256, erro = escrever_modelo(treinado, saida, x[: min(64, len(x))])

    from jarvis.ouvido import DetetorOpenWakeWord

    detetor = DetetorOpenWakeWord(saida)
    silencio = avaliar.serie_de_scores(detetor, b"\x00\x00" * TAXA * 3)
    cartao = {
        "palavra": PALAVRA,
        "treinado_em": datetime.now().isoformat(timespec="seconds"),
        "ensaio_sem_voz_do_sponsor": bool(args.ensaio_sintetico),
        "sha256": sha256,
        "contagem": contagem,
        "janelas": {"total": len(amostras), "positivas": int(y.sum()), "treino": len(indices_treino),
                    "validacao": len(indices_validacao)},
        "metricas": treinado.metricas,
        "onnx_contra_mlp_erro_maximo": erro,
        "score_maximo_em_silencio": max(silencio, default=0.0),
        "parametros": {"semente": args.semente, "camadas": list(CAMADAS_ESCONDIDAS),
                       "aumentos_por_gravacao": AUMENTOS_POR_GRAVACAO, "folga_do_fim_s": FOLGA_DO_FIM_S},
        "versoes": {"python": platform.python_version(), **_versoes()},
        "segundos": round(time.perf_counter() - inicio, 1),
    }
    cartao_caminho = saida.with_name(saida.name + ".json")
    cartao_caminho.write_text(json.dumps(cartao, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(treinado.metricas, ensure_ascii=False))
    print(f"modelo: {avaliar.mostrar(saida)}  sha256 {sha256}")
    print(f"cartao: {avaliar.mostrar(cartao_caminho)}")
    if not args.ensaio_sintetico:
        print(f"Medir na voz do Sponsor: {avaliar.comando('--lingua pt')}")
    return 0


def _versoes() -> dict:
    from importlib.metadata import PackageNotFoundError, version

    versoes = {}
    for pacote in ("openwakeword", "scikit-learn", "onnxruntime", "numpy", "piper-tts"):
        try:
            versoes[pacote] = version(pacote)
        except PackageNotFoundError:
            versoes[pacote] = None
    return versoes


# --- Autoteste: dados falsos, sem Piper, sem microfone, sem som --------------------------


def _autoteste() -> int:
    import numpy as np

    falhas: list[str] = []

    def verificar(nome: str, condicao: bool, detalhe: object = "") -> None:
        if condicao:
            print(f"ok   {nome}")
        else:
            falhas.append(f"{nome} {detalhe}".strip())

    rng = np.random.default_rng(0)

    # 1. o ONNX minimo da o mesmo que o forward em numpy.
    pesos = [rng.standard_normal((JANELAS * DIMENSAO, 8)), rng.standard_normal((8, 1))]
    vieses = [rng.standard_normal(8), rng.standard_normal(1)]
    x = rng.standard_normal((5, JANELAS, DIMENSAO)).astype(np.float32)
    erro = float(np.max(np.abs(prever_onnx(onnx_do_mlp(pesos, vieses), x) - forward_numpy(pesos, vieses, x))))
    verificar("onnx: igual ao forward em numpy", erro < 1e-4, erro)

    # 2. treino sobre caracteristicas separaveis: detecao e falsos na validacao.
    y = np.array([1] * 60 + [0] * 240)
    x = rng.standard_normal((300, JANELAS, DIMENSAO)).astype(np.float32)
    x[y == 1, :, :4] += 3.0
    origens = [Amostra(f"o{i // 3}", int(y[i]), None) for i in range(300)]
    treino_i, valida_i = dividir_por_origem(np.random.default_rng(1), origens)
    grupos = {origens[i].origem for i in treino_i} & {origens[i].origem for i in valida_i}
    verificar("divisao: uma origem nunca fica dos dois lados", not grupos, grupos)
    treinado = treinar(x, y, treino_i, valida_i)
    detecao, falsos = treinado.metricas["detecao_a_0_5"], treinado.metricas["falsos_a_0_5"]
    verificar("treino: detecao na validacao", detecao is not None and detecao >= 0.95, treinado.metricas)
    verificar("treino: falsos na validacao", falsos is not None and falsos <= 0.05, treinado.metricas)

    # 3. modelo escrito carrega no detetor do ouvido (openWakeWord) e da scores.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="treinar-ativacao-") as temporaria:
        saida = Path(temporaria) / "falso.onnx"
        sha256, erro = escrever_modelo(treinado, saida, x[:16])
        verificar("modelo: sha256 do ficheiro", sha256 == hashlib.sha256(saida.read_bytes()).hexdigest())
        if MODELO_MELSPEC.is_file() and MODELO_EMBEDDING.is_file():
            from jarvis.ouvido import DetetorOpenWakeWord

            scores = avaliar.serie_de_scores(DetetorOpenWakeWord(saida), b"\x00\x00" * TAXA)
            verificar("modelo: carrega no detetor do ouvido", len(scores) == 13 and all(0 <= s <= 1 for s in scores))
        else:
            print("--   modelos de caracteristicas do openWakeWord em falta: carga no detetor nao verificada")

    # 4. janelas: a palavra acaba perto do fim; recortes com 2 s.
    palavra = np.ones(8000, dtype=np.float32) * 0.5
    janela = janela_com_palavra(np.random.default_rng(2), palavra)
    inicio, fim = zona_de_fala(janela)
    verificar("janela: 2 s", len(janela) == AMOSTRAS_DA_JANELA)
    verificar("janela: palavra perto do fim", AMOSTRAS_DA_JANELA - fim <= FOLGA_DO_FIM_S * TAXA + TAXA // 100, fim)
    verificar("recorte: 2 s de audio curto", len(recorte(rng, palavra)) == AMOSTRAS_DA_JANELA)

    # 5. montagem sem voz sintetica: positivos aumentados, negativos e ruido.
    amostras, contagem = montar_amostras(
        rng, [("treino-pt-01", palavra)], [("pt-02", np.ones(40000, dtype=np.float32) * 0.1)], None,
        janelas_de_ruido=6, escrever=lambda _t: None,
    )
    positivas = sum(a.rotulo for a in amostras)
    verificar("montagem: aumentos por gravacao", positivas == AUMENTOS_POR_GRAVACAO, positivas)
    verificar("montagem: negativos e ruido", len(amostras) - positivas == RECORTES_POR_GRAVACAO_NEGATIVA + 6)
    verificar("montagem: contagem", contagem["positivos_reais"] == 1 and contagem["negativos_reais"] == 1)

    # 6. negativos do guiao: sem a palavra de ativacao e sem marcadores.
    textos = frases_negativas_do_guiao()
    verificar("negativos: nenhum comeca pela palavra", not any(t.lower().startswith(PALAVRA) for t in textos))
    verificar("negativos: sem marcadores de projeto", not any("<projeto" in t for t in textos))
    pedacos = textos_negativos(np.random.default_rng(3), 200)
    verificar("negativos: pedacos pedidos mais os parecidos", len(pedacos) == len(TEXTOS_PARECIDOS) * REPETICOES_DOS_PARECIDOS + len(TEXTOS_DO_DIA_A_DIA) + 200, len(pedacos))
    verificar("negativos: nenhum pedaco e a palavra", not any(p.lower().startswith(PALAVRA) for p in pedacos))

    # 7. saida: so em models/ dentro do repositorio; o ensaio nunca e o modelo real.
    for valor, ensaio, aceite in (
        (None, False, True),
        ("docs/modelo.onnx", False, False),
        ("models/openwakeword/modelo.txt", False, False),
        (None, True, False),
        ("models/openwakeword/ensaio.onnx", True, True),
    ):
        try:
            caminho_de_saida(valor, ensaio)
            aceite_obtido = True
        except ValueError:
            aceite_obtido = False
        verificar(f"saida {valor!r} ensaio={ensaio}: {'aceite' if aceite else 'recusada'}", aceite_obtido == aceite)

    # 8. o protobuf: varints de valores grandes e negativos.
    verificar("varint", _varint(300) == b"\xac\x02" and len(_varint(-1)) == 10)
    verificar("tensor float com dims", _tensor("W", np.zeros((2, 3)))[:4] == struct.pack("BBBB", 8, 2, 8, 3))

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste de treinar_ativacao completo (dados falsos, sem Piper, sem microfone nem som).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
