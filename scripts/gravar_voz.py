r"""Grava a voz real do Sponsor a ler o guiao, frase a frase, para recordings/.

Le `tests/voz/guiao-real-<lingua>.md`, mostra cada frase com os marcadores
<projeto-1>/<projeto-2> trocados pelos nomes do `config.toml` local e grava do
microfone configurado (`[microfone] nome`): Enter para comecar, Enter para
parar. Cada frase fica em `recordings/<lingua>/<id>.wav` (16 kHz, mono, PCM16)
e o manifesto local `recordings/<lingua>/manifesto.json` regista o que ja foi
gravado. `recordings/` inteira esta no .gitignore: nem o audio, nem o
manifesto (que leva os nomes reais dos projetos) entram no Git.

RETOMA onde parou: uma frase que ja esta no manifesto, cujo WAV existe e passa
a validacao e saltada. `--refazer pt-03,pt-17` grava de novo so essas.

MICROFONE: escolhido e provado por `scripts/verificar_microfone.py`
(`abrir_microfone`: MME, depois WASAPI reamostrado para 16 kHz; nunca
DirectSound nem WDM-KS; a leitura tem de acompanhar o relogio). Sem nenhum
dispositivo que passe, o gravador para antes de gravar seja o que for.
`--verificar` abre o microfone 2 s, sem gravar ficheiro, e diz o dispositivo,
a API, o tempo real contra o audio lido e o nivel.

VALIDACAO: uma captura com mais audio do que o tempo real decorrido, ou com um
troco de zeros exatos de 0,5 s ou mais, e recusada e a frase repete-se com
aviso. As gravacoes ja existentes passam pela mesma validacao (e a duracao do
WAV tem de bater com a do manifesto): as que falham contam como nao gravadas e
voltam a ser pedidas. O gravador nunca apaga ficheiros: o WAV invalido antigo
fica ao lado, renomeado `<id>.invalida-<data>.wav`, quando a frase e regravada
(e o de um `--refazer`, `<id>.anterior-<data>.wav`).

A pasta de destino (`--pasta`) tem de ficar dentro de `recordings/` deste
repositorio; qualquer caminho fora e recusado antes de gravar seja o que for.

SILENCIOSO POR OMISSAO: o gravador so abre o microfone. Um bip curto antes de
cada gravacao so toca com `--com-som`; sem a flag nunca se abre um dispositivo
de saida de audio.

Uso (cerca de 10 minutos por lingua):
    .venv\Scripts\python scripts/gravar_voz.py --lingua en
    .venv\Scripts\python scripts/gravar_voz.py --lingua pt
    .venv\Scripts\python scripts/gravar_voz.py --lingua pt --refazer pt-03
    .venv\Scripts\python scripts/gravar_voz.py --verificar
    .venv\Scripts\python scripts/gravar_voz.py --autoteste
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import shutil
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.audio_util import (  # noqa: E402
    TAXA_AMOSTRAGEM_PADRAO,
    caminho_wav_de_saida,
    escrever_wav_pcm16,
    ler_wav_pcm16,
    pico_pcm16,
)
from jarvis.config import Config  # noqa: E402
from jarvis.consola import forcar_consola_utf8  # noqa: E402

PASTA_SCRIPTS = Path(__file__).resolve().parent


def _carregar_modulo_irmao(nome: str):
    """Importa `scripts/<nome>.py` por caminho, sem mexer no sys.path.

    Mesma chave de cache de `scripts/medir_voz.py`, para o modulo so ser
    executado uma vez por processo, venha de onde vier o pedido.
    """
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


microfone = _carregar_modulo_irmao("verificar_microfone")


# --- O guiao -------------------------------------------------------------------

GUIOES = {
    "pt": RAIZ / "tests" / "voz" / "guiao-real-pt.md",
    "en": RAIZ / "tests" / "voz" / "guiao-real-en.md",
}
LINGUAS = tuple(GUIOES)

#: A palavra de ativacao de cada lingua, tal como se le no guiao.
PALAVRA_DE_ATIVACAO = {"pt": "boas jarvis", "en": "hey jarvis"}

MARCADORES_DE_PROJETO = ("<projeto-1>", "<projeto-2>")
SEM_PROJETO = "—"

#: Casos do guiao e o minimo de frases que cada um tem de ter.
MINIMO_POR_CASO = {"a": 10, "b": 8, "c": 8, "local": 10, "confirmacao": 4}
MINIMO_DE_FRASES = 40

#: Intencoes que o guiao pode esperar: a lista fechada do interprete mais as
#: respostas ao recap de confirmacao.
INTENCOES = (
    "ditar_prompt",
    "estado",
    "ler_relatorio",
    "lancar_run",
    "retomar_run",
    "parar_run",
    "conversa",
    "horas",
    "abrir_editor",
    "abrir_pasta",
    "calar",
    "dormir",
    "acordar",
    "desconhecido",
    "confirmar",
    "corrigir",
    "acrescentar",
    "cancelar",
)

COLUNAS = ("id", "caso", "frase", "intenção", "projeto", "ativação")
PADRAO_ID = re.compile(r"^(pt|en)-\d{2}$")
PADRAO_MARCADOR = re.compile(r"<[^>]*>")


class GuiaoError(Exception):
    """O guiao nao cumpre o formato; a mensagem diz a linha e o problema."""


@dataclass(frozen=True)
class FraseDoGuiao:
    id: str
    caso: str
    #: A frase com marcadores, tal como esta no ficheiro versionado.
    frase: str
    intencao: str
    #: O marcador do projeto esperado, ou None quando a frase nao nomeia projeto.
    projeto: str | None
    ativacao: bool


def normalizar(texto: str) -> str:
    """minusculas, sem acentos, so letras/digitos/espacos, espacos colapsados."""
    import unicodedata

    sem_acentos = unicodedata.normalize("NFKD", texto)
    sem_acentos = "".join(c for c in sem_acentos if not unicodedata.combining(c)).lower()
    sem_acentos = re.sub(r"[^a-z0-9\s]", " ", sem_acentos)
    return re.sub(r"\s+", " ", sem_acentos).strip()


def comeca_pela_ativacao(texto: str, lingua: str) -> bool:
    """A frase normalizada comeca pelas palavras exatas da palavra de ativacao."""
    palavras = normalizar(texto).split()
    alvo = PALAVRA_DE_ATIVACAO[lingua].split()
    return palavras[: len(alvo)] == alvo


def _celulas(linha: str) -> list[str]:
    return [celula.strip() for celula in linha.strip().strip("|").split("|")]


def ler_guiao(caminho: Path, lingua: str) -> list[FraseDoGuiao]:
    """Le e valida a tabela do guiao. Levanta GuiaoError com a linha em falta."""
    if lingua not in LINGUAS:
        raise GuiaoError(f"lingua '{lingua}': so {LINGUAS}")
    texto = Path(caminho).read_text(encoding="utf-8")
    frases: list[FraseDoGuiao] = []
    vistos: set[str] = set()
    cabecalho_visto = False
    for numero, linha in enumerate(texto.splitlines(), start=1):
        if not linha.lstrip().startswith("|"):
            continue
        celulas = _celulas(linha)
        if not cabecalho_visto:
            if tuple(celulas) != COLUNAS:
                raise GuiaoError(f"{caminho.name}:{numero}: cabecalho esperado {COLUNAS}, obtido {celulas}")
            cabecalho_visto = True
            continue
        if all(set(c) <= set(":-") for c in celulas):
            continue  # linha separadora da tabela
        if len(celulas) != len(COLUNAS):
            raise GuiaoError(f"{caminho.name}:{numero}: {len(celulas)} colunas, esperava {len(COLUNAS)}")
        id_, caso, frase, intencao, projeto, ativacao = celulas
        onde = f"{caminho.name}:{numero} ({id_})"
        if not PADRAO_ID.match(id_) or not id_.startswith(f"{lingua}-"):
            raise GuiaoError(f"{onde}: id tem de ser '{lingua}-NN'")
        if id_ in vistos:
            raise GuiaoError(f"{onde}: id repetido")
        vistos.add(id_)
        if caso not in MINIMO_POR_CASO:
            raise GuiaoError(f"{onde}: caso '{caso}' fora de {tuple(MINIMO_POR_CASO)}")
        if intencao not in INTENCOES:
            raise GuiaoError(f"{onde}: intencao '{intencao}' fora da lista fechada")
        for marcador in PADRAO_MARCADOR.findall(frase):
            if marcador not in MARCADORES_DE_PROJETO:
                raise GuiaoError(f"{onde}: so se aceitam os marcadores {MARCADORES_DE_PROJETO}, nao '{marcador}'")
        if projeto == SEM_PROJETO:
            projeto_esperado = None
        elif projeto in MARCADORES_DE_PROJETO:
            if projeto not in frase:
                raise GuiaoError(f"{onde}: projeto esperado {projeto} nao aparece na frase")
            projeto_esperado = projeto
        else:
            raise GuiaoError(f"{onde}: projeto tem de ser um marcador ou '{SEM_PROJETO}'")
        if ativacao not in ("sim", "não"):
            raise GuiaoError(f"{onde}: ativacao tem de ser 'sim' ou 'não'")
        com_ativacao = ativacao == "sim"
        if com_ativacao != comeca_pela_ativacao(frase, lingua):
            raise GuiaoError(
                f"{onde}: coluna ativacao='{ativacao}' nao bate com a frase "
                f"(a palavra de ativacao e '{PALAVRA_DE_ATIVACAO[lingua]}')"
            )
        frases.append(FraseDoGuiao(id_, caso, frase, intencao, projeto_esperado, com_ativacao))
    if not cabecalho_visto:
        raise GuiaoError(f"{caminho.name}: tabela do guiao nao encontrada")
    return frases


def verificar_cobertura(frases: Sequence[FraseDoGuiao]) -> list[str]:
    """Os minimos do guiao. Devolve a lista de problemas (vazia = cumpre)."""
    problemas: list[str] = []
    if len(frases) < MINIMO_DE_FRASES:
        problemas.append(f"{len(frases)} frases, minimo {MINIMO_DE_FRASES}")
    for caso, minimo in MINIMO_POR_CASO.items():
        n = sum(1 for f in frases if f.caso == caso)
        if n < minimo:
            problemas.append(f"caso '{caso}': {n} frases, minimo {minimo}")
    com_ativacao = sum(1 for f in frases if f.ativacao)
    if com_ativacao * 2 != len(frases):
        problemas.append(f"{com_ativacao} de {len(frases)} frases com palavra de ativacao; tem de ser metade")
    return problemas


def nomes_dos_marcadores(config: Config) -> dict[str, str]:
    """<projeto-N> -> nome do projeto N da configuracao (o 1.o repete-se se so houver um)."""
    nomes = [p.nome for p in config.projetos]
    if not nomes:
        raise GuiaoError("a configuracao nao tem projetos; o guiao precisa de pelo menos um")
    return {
        MARCADORES_DE_PROJETO[0]: nomes[0],
        MARCADORES_DE_PROJETO[1]: nomes[1] if len(nomes) > 1 else nomes[0],
    }


def texto_a_ler(frase: FraseDoGuiao, projetos: dict[str, str]) -> str:
    texto = frase.frase
    for marcador, nome in projetos.items():
        texto = texto.replace(marcador, nome)
    return texto


# --- Pastas e manifesto ----------------------------------------------------------

NOME_PASTA_GRAVACOES = "recordings"
NOME_MANIFESTO = "manifesto.json"
ORIGEM_MICROFONE = "microfone"
ORIGEM_SINTETICA = "sintetico"


def pasta_de_gravacoes(valor: str | Path | None = None, raiz: Path | None = None) -> Path:
    """Valida `--pasta` (entrada externa): tem de ficar dentro de recordings/.

    recordings/ e a pasta que o .gitignore apanha para audio e para o manifesto
    (que leva os nomes reais dos projetos). Um caminho fora do repositorio, ou
    dentro dele mas fora de recordings/, e recusado com ValueError.
    """
    raiz = (RAIZ if raiz is None else Path(raiz)).resolve()
    base = raiz / NOME_PASTA_GRAVACOES
    caminho = Path(valor).expanduser() if valor else base
    if not caminho.is_absolute():
        caminho = raiz / caminho
    caminho = caminho.resolve()
    if not caminho.is_relative_to(raiz):
        raise ValueError(f"--pasta '{valor}' fica fora do repositorio; as gravacoes vivem em {NOME_PASTA_GRAVACOES}/")
    if not caminho.is_relative_to(base):
        raise ValueError(
            f"--pasta '{valor}' fica fora de {NOME_PASTA_GRAVACOES}/, a unica pasta que o .gitignore "
            "cobre para as gravacoes e o manifesto"
        )
    return caminho


def ler_manifesto(pasta_lingua: Path) -> dict:
    caminho = pasta_lingua / NOME_MANIFESTO
    if not caminho.is_file():
        return {}
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError) as erro:
        raise ValueError(f"manifesto ilegivel em {NOME_PASTA_GRAVACOES}/{pasta_lingua.name}/{NOME_MANIFESTO}: {erro}")
    return dados if isinstance(dados, dict) else {}


def escrever_manifesto(pasta_lingua: Path, manifesto: dict) -> None:
    """Escreve por ficheiro temporario + replace: um Ctrl+C nunca o deixa a meio."""
    pasta_lingua.mkdir(parents=True, exist_ok=True)
    destino = pasta_lingua / NOME_MANIFESTO
    temporario = destino.with_suffix(".json.tmp")
    temporario.write_text(json.dumps(manifesto, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporario, destino)


def ja_gravadas(pasta_lingua: Path, manifesto: dict) -> set[str]:
    """Ids do manifesto cujo WAV ainda existe no disco."""
    gravacoes = manifesto.get("gravacoes", {})
    return {
        id_
        for id_, dados in gravacoes.items()
        if isinstance(dados, dict) and (pasta_lingua / str(dados.get("ficheiro", ""))).is_file()
    }


#: Diferenca maxima entre a duracao do WAV e a registada no manifesto.
FOLGA_DA_DURACAO_S = 0.05


def _numero(valor: object) -> float | None:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not math.isfinite(valor):
        return None
    return float(valor)


def motivo_de_gravacao_invalida(pcm: bytes, taxa: int, canais: int, registo: dict) -> str | None:
    """Porque uma gravacao ja feita nao serve, ou None se serve.

    A mesma validacao das capturas novas (troco de zeros exatos, mais audio do
    que tempo real) mais a coerencia com o manifesto: a duracao do WAV tem de
    ser a registada.
    """
    if canais != 1:
        return f"{canais} canais; o gravador so escreve mono"
    duracao = (len(pcm) // 2) / taxa if taxa else 0.0
    registada = _numero(registo.get("duracao_s"))
    if registada is None:
        return "o manifesto nao regista a duracao desta gravacao"
    if abs(duracao - registada) > FOLGA_DA_DURACAO_S:
        return f"o WAV tem {duracao:.2f} s e o manifesto diz {registada:.2f} s"
    real = _numero(registo.get("tempo_real_s"))
    if real is not None:
        motivo = microfone.desacerto_com_o_relogio(duracao, real, so_excesso=True)
        if motivo:
            return motivo
    zeros = microfone.maior_troco_de_zeros_s(pcm, taxa)
    if zeros >= microfone.TROCO_DE_ZEROS_MAXIMO_S:
        return f"silencio digital: {zeros:.2f} s seguidos de zeros exatos"
    return None


def gravacoes_invalidas(pasta_lingua: Path, manifesto: dict) -> dict[str, str]:
    """id -> motivo, para as gravacoes do manifesto cujo WAV existe mas nao serve."""
    registos = manifesto.get("gravacoes", {})
    invalidas: dict[str, str] = {}
    for id_ in sorted(ja_gravadas(pasta_lingua, manifesto)):
        registo = registos[id_]
        ficheiro = str(registo.get("ficheiro", ""))
        if Path(ficheiro).name != ficheiro or not ficheiro.lower().endswith(".wav"):
            invalidas[id_] = "o ficheiro do manifesto nao e um .wav desta pasta"
            continue
        try:
            pcm, taxa, canais = ler_wav_pcm16(pasta_lingua / ficheiro)
        except (OSError, ValueError, EOFError) as erro:
            invalidas[id_] = f"WAV ilegivel ({type(erro).__name__})"
            continue
        motivo = motivo_de_gravacao_invalida(pcm, taxa, canais, registo)
        if motivo:
            invalidas[id_] = motivo
    return invalidas


def guardar_ao_lado(caminho: Path, etiqueta: str) -> Path:
    """Renomeia um WAV que vai ser substituido para `<id>.<etiqueta>-<data>.wav`; nunca apaga."""
    carimbo = datetime.now().strftime("%Y%m%d-%H%M%S")
    destino = caminho.with_name(f"{caminho.stem}.{etiqueta}-{carimbo}.wav")
    contador = 1
    while destino.exists():
        contador += 1
        destino = caminho.with_name(f"{caminho.stem}.{etiqueta}-{carimbo}-{contador}.wav")
    caminho.rename(destino)
    return destino


# --- Captura -----------------------------------------------------------------------

TAMANHO_DO_BLOCO = 1024
SEGUNDOS_MAXIMOS_POR_FRASE = 20.0
SEGUNDOS_MINIMOS_POR_FRASE = 0.3
SEGUNDOS_DA_VERIFICACAO = 2.0


@dataclass(frozen=True)
class Captura:
    #: PCM16 mono a 16 kHz.
    pcm: bytes
    #: Tempo de relogio que a captura levou, de abrir o stream ao ultimo bloco.
    segundos_reais: float


class CapturaPyAudio:
    """Microfone real, escolhido e provado por `verificar_microfone.abrir_microfone`.

    `abrir()` escolhe o dispositivo uma vez por sessao (com a prova de tempo
    real); cada `gravar()` reabre o stream com a mesma escolha e mede o tempo
    real da captura. `criar_pa`, `formato` e `relogio` existem para os testes
    usarem dispositivos falsos.
    """

    origem = ORIGEM_MICROFONE

    def __init__(
        self,
        nome_microfone: str,
        criar_pa: Callable[[], object] | None = None,
        formato: int | None = None,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        self.nome_microfone = nome_microfone
        self._criar_pa = criar_pa
        self._formato = formato
        self._relogio = relogio
        self._pa = None
        self.escolha = None
        self.prova = None
        self.tentativas: list[str] = []
        self.descricao = ""

    def abrir(self, segundos_de_prova: float = microfone.SEGUNDOS_DE_PROVA) -> None:
        if self._criar_pa is None:
            import pyaudio

            self._criar_pa, self._formato = pyaudio.PyAudio, pyaudio.paInt16
        self._pa = self._criar_pa()
        try:
            entrada = microfone.abrir_microfone(
                self._pa, self.nome_microfone, self._formato,
                segundos_de_prova=segundos_de_prova, relogio=self._relogio,
            )
        except BaseException:
            self.fechar()
            raise
        entrada.fechar()
        self.escolha, self.prova, self.tentativas = entrada.escolha, entrada.prova, entrada.tentativas
        self.descricao = entrada.escolha.descrever()

    def gravar(self, parar: threading.Event, maximo_s: float) -> Captura:
        limite = int(TAXA_AMOSTRAGEM_PADRAO * maximo_s) * 2
        blocos: list[bytes] = []
        lidos = 0
        # O relogio comeca antes de abrir: o audio que o driver junta enquanto
        # o stream abre tambem e tempo real.
        inicio = self._relogio()
        entrada = microfone.abrir_entrada(self._pa, self.escolha, self._formato, TAMANHO_DO_BLOCO)
        try:
            while not parar.is_set() and lidos < limite:
                bloco = entrada.ler()
                if not bloco:
                    break
                blocos.append(bloco)
                lidos += len(bloco)
        finally:
            fim = self._relogio()
            entrada.fechar()
        return Captura(b"".join(blocos)[:limite], fim - inicio)

    def fechar(self) -> None:
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None


def bip() -> None:
    """Bip curto no altifalante. So e chamado com --com-som."""
    import winsound

    winsound.Beep(880, 120)


@dataclass
class ResumoDaGravacao:
    gravadas: list[str]
    saltadas: list[str]
    ja_existiam: list[str]
    interrompido: bool
    #: id -> motivo das gravacoes antigas que nao passaram a validacao.
    invalidas: dict[str, str] = field(default_factory=dict)
    #: Capturas recusadas nesta sessao (a frase repetiu-se).
    recusadas: int = 0


def gravar_guiao(
    lingua: str,
    frases: Sequence[FraseDoGuiao],
    projetos: dict[str, str],
    pasta: Path,
    captura,
    entrada: Callable[[str], str] = input,
    com_som: bool = False,
    tocar_sinal: Callable[[], None] = bip,
    refazer: Sequence[str] = (),
    maximo_s: float = SEGUNDOS_MAXIMOS_POR_FRASE,
    raiz: Path | None = None,
    guiao: Path | None = None,
) -> ResumoDaGravacao:
    """Grava, frase a frase, as que faltam. O manifesto e escrito a cada frase."""
    raiz = (RAIZ if raiz is None else Path(raiz)).resolve()
    pasta_lingua = pasta / lingua
    manifesto = ler_manifesto(pasta_lingua)
    manifesto.setdefault("versao", 1)
    manifesto["lingua"] = lingua
    guiao = GUIOES[lingua] if guiao is None else guiao
    manifesto["guiao"] = Path(guiao).name
    manifesto["projetos"] = dict(projetos)
    manifesto.setdefault("gravacoes", {})
    ids_do_guiao = {f.id for f in frases}
    invalidas = {i: m for i, m in gravacoes_invalidas(pasta_lingua, manifesto).items() if i in ids_do_guiao}
    existentes = ja_gravadas(pasta_lingua, manifesto) - set(refazer) - set(invalidas)
    resumo = ResumoDaGravacao([], [], sorted(existentes), False, dict(invalidas))
    if invalidas:
        print(
            f"{len(invalidas)} gravacoes antigas nao passam a validacao e voltam a ser pedidas "
            "(nada e apagado: cada WAV antigo fica ao lado quando a frase for regravada):"
        )
        for id_, motivo in invalidas.items():
            print(f"    {id_}: {motivo}")
    pendentes = [f for f in frases if f.id not in existentes]
    if not pendentes:
        print(f"Nada a gravar: as {len(frases)} frases de '{lingua}' ja estao gravadas.")
        return resumo
    print(f"{len(existentes)} de {len(frases)} ja gravadas; faltam {len(pendentes)}.")
    captura.abrir()
    print(f"Microfone: {captura.descricao}")
    try:
        indice = 0
        while indice < len(pendentes):
            frase = pendentes[indice]
            posicao = frases.index(frase) + 1
            print()
            print(f"[{posicao}/{len(frases)}] {frase.id} (caso {frase.caso})")
            print(f"    LE:  {texto_a_ler(frase, projetos)}")
            resposta = entrada("    Enter para gravar, s para saltar, q para sair: ").strip().lower()
            if resposta == "q":
                resumo.interrompido = True
                break
            if resposta == "s":
                resumo.saltadas.append(frase.id)
                indice += 1
                continue
            if com_som:
                tocar_sinal()
            parar = threading.Event()
            resultado: dict[str, object] = {}

            def capturar() -> None:
                try:
                    resultado["captura"] = captura.gravar(parar, maximo_s)
                except BaseException as erro:  # noqa: BLE001 - relancado na thread principal
                    resultado["erro"] = erro

            fio = threading.Thread(target=capturar, daemon=True)
            fio.start()
            entrada("    a gravar... Enter para parar: ")
            parar.set()
            fio.join(timeout=maximo_s + 5)
            if "erro" in resultado:
                raise resultado["erro"]
            feita = resultado.get("captura")
            pcm = feita.pcm if feita is not None else b""
            duracao = (len(pcm) // 2) / TAXA_AMOSTRAGEM_PADRAO
            if pico_pcm16(pcm) == 0:
                resumo.recusadas += 1
                print("    SEM SINAL (microfone em mudo ou sem permissao); a frase repete-se.")
                continue
            if duracao < SEGUNDOS_MINIMOS_POR_FRASE:
                print(f"    gravacao de {duracao:.2f} s e curta demais; a frase repete-se.")
                continue
            defeito = microfone.defeito_da_captura(pcm, feita.segundos_reais, so_excesso=True)
            if defeito:
                resumo.recusadas += 1
                print(f"    AVISO: captura recusada ({defeito}); a frase repete-se.")
                print("    Se voltar a acontecer: q para sair e correr gravar_voz.py --verificar.")
                continue
            ficheiro = f"{frase.id}.wav"
            caminho = caminho_wav_de_saida(pasta_lingua / ficheiro, raiz)
            registo = {
                "ficheiro": ficheiro,
                "origem": captura.origem,
                # Os nomes que estavam no ecra: se o config.toml mudar entre
                # sessoes, cada frase continua avaliada contra o que foi lido.
                "projetos": dict(projetos),
                "duracao_s": round(duracao, 3),
                "tempo_real_s": round(feita.segundos_reais, 3),
                "gravado_em": datetime.now().isoformat(timespec="seconds"),
            }
            if caminho.is_file():
                # Regravar (frase invalida ou --refazer) nunca apaga o WAV antigo.
                etiqueta = "invalida" if frase.id in invalidas else "anterior"
                registo["substituiu"] = guardar_ao_lado(caminho, etiqueta).name
            escrever_wav_pcm16(caminho, pcm, TAXA_AMOSTRAGEM_PADRAO, canais=1)
            manifesto["gravacoes"][frase.id] = registo
            escrever_manifesto(pasta_lingua, manifesto)
            resumo.gravadas.append(frase.id)
            print(f"    gravada: {duracao:.1f} s")
            indice += 1
    finally:
        captura.fechar()
    return resumo


def verificar_captura(
    nome_microfone: str,
    criar_pa: Callable[[], object] | None = None,
    formato: int | None = None,
    relogio: Callable[[], float] = time.monotonic,
    segundos: float = SEGUNDOS_DA_VERIFICACAO,
) -> int:
    """Abre o microfone `segundos`, sem escrever ficheiro, e diz se pode gravar.

    Imprime o dispositivo, a API, o tempo real contra o audio lido e o nivel;
    devolve 0 se o microfone passou a prova, 1 se nao.
    """
    captura = CapturaPyAudio(nome_microfone, criar_pa, formato, relogio)
    try:
        captura.abrir(segundos_de_prova=segundos)
    except microfone.MicrofoneInutilizavel as erro:
        print(f"ERRO: {erro}")
        return 1
    except Exception as erro:  # noqa: BLE001 - pyaudio em falta ou dispositivo que rebenta
        print(f"ERRO: nao foi possivel abrir o microfone: {erro!r}")
        return 1
    finally:
        captura.fechar()
    escolha, prova = captura.escolha, captura.prova
    rms, pico, veredicto = microfone.descrever_sinal(prova.pcm)
    for tentativa in captura.tentativas:
        print(f"saltado       = {tentativa}")
    print(f"dispositivo   = [{escolha.candidato.indice}] {escolha.candidato.nome} ({escolha.origem})")
    print(f"API           = {escolha.candidato.api}, aberto a {escolha.taxa} Hz com {escolha.canais} canal(is); entregue a {TAXA_AMOSTRAGEM_PADRAO} Hz mono")
    print(f"tempo real    = {prova.real_s:.2f} s; audio lido = {prova.audio_s:.2f} s")
    print(f"zeros exatos  = maior troco {prova.zeros_s:.2f} s (maximo {microfone.TROCO_DE_ZEROS_MAXIMO_S} s)")
    print(f"nivel         = RMS {rms:.4f}, pico {pico} de 32768 ({veredicto})")
    print("OK: o microfone le em tempo real e sem silencio digital; nenhum ficheiro foi gravado.")
    return 0


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Grava a voz real do Sponsor a ler o guiao, para recordings/ (ignorada pelo Git).",
        epilog="Sem --com-som nada toca: o gravador so abre o microfone.",
    )
    parser.add_argument("--lingua", choices=LINGUAS, help="lingua do guiao a gravar")
    parser.add_argument("--pasta", default=None, help="pasta dentro de recordings/ (por omissao recordings/)")
    parser.add_argument("--refazer", default="", help="ids a gravar de novo, separados por virgula (ex.: pt-03,pt-17)")
    parser.add_argument("--com-som", action="store_true", help="toca um bip antes de cada gravacao")
    parser.add_argument(
        "--verificar",
        action="store_true",
        help=f"abre o microfone {SEGUNDOS_DA_VERIFICACAO:.0f} s sem gravar ficheiro e diz dispositivo, API, tempo real e nivel",
    )
    parser.add_argument("--autoteste", action="store_true", help="corre o autoteste com microfone falso, sem som")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)
    if args.autoteste:
        return _autoteste()
    if args.verificar:
        medir_voz = _carregar_modulo_irmao("medir_voz")
        config, origem = medir_voz.carregar_config_para_arnes()
        print(f"Configuracao: {origem}. Microfone pedido: '{config.microfone}'.")
        return verificar_captura(config.microfone)
    if not args.lingua:
        print("erro: falta --lingua pt|en", file=sys.stderr)
        return 2
    try:
        pasta = pasta_de_gravacoes(args.pasta)
        frases = ler_guiao(GUIOES[args.lingua], args.lingua)
    except (ValueError, GuiaoError) as erro:
        print(f"erro: {erro}", file=sys.stderr)
        return 2
    medir_voz = _carregar_modulo_irmao("medir_voz")
    config, origem = medir_voz.carregar_config_para_arnes()
    projetos = nomes_dos_marcadores(config)
    refazer = [item.strip() for item in args.refazer.split(",") if item.strip()]
    desconhecidos = sorted(set(refazer) - {f.id for f in frases})
    if desconhecidos:
        print(f"erro: --refazer com ids que nao estao no guiao: {', '.join(desconhecidos)}", file=sys.stderr)
        return 2
    print(f"Configuracao: {origem}. Microfone pedido: '{config.microfone}'.")
    captura = CapturaPyAudio(config.microfone)
    try:
        resumo = gravar_guiao(args.lingua, frases, projetos, pasta, captura, com_som=args.com_som, refazer=refazer)
    except KeyboardInterrupt:
        print("\nInterrompido. O que ja foi gravado fica; volta a correr o mesmo comando para continuar.")
        return 130
    except OSError as erro:
        print(f"erro do microfone: {erro}", file=sys.stderr)
        print(r"Nada foi gravado. Ver o que se passa: .venv\Scripts\python scripts/gravar_voz.py --verificar", file=sys.stderr)
        return 1
    print()
    print(
        f"Gravadas agora: {len(resumo.gravadas)}; saltadas: {len(resumo.saltadas)}; "
        f"ja existiam: {len(resumo.ja_existiam)}; capturas recusadas: {resumo.recusadas}."
    )
    if resumo.interrompido or resumo.saltadas:
        print("Para continuar mais tarde, corre o mesmo comando: retoma onde parou.")
    return 0


# --- Autoteste: microfone falso, WAV em tmp/, sem som ------------------------------


def tom_pcm16(segundos: float, frequencia: float = 220.0, amplitude: int = 8000) -> bytes:
    """Onda sinusoidal PCM16 16 kHz: audio sintetico para os autotestes."""
    n = int(TAXA_AMOSTRAGEM_PADRAO * segundos)
    return struct.pack(
        f"<{n}h",
        *(int(amplitude * math.sin(2 * math.pi * frequencia * i / TAXA_AMOSTRAGEM_PADRAO)) for i in range(n)),
    )


class CapturaFalsa:
    """Devolve um tom de 1 s por gravacao, sem tocar em hardware.

    `segundos_reais` e o tempo de relogio que a captura diz ter levado; por
    omissao a propria duracao do audio (um microfone que le em tempo real).
    Com uma lista, cada gravacao usa o valor seguinte (o ultimo repete-se).
    """

    origem = ORIGEM_SINTETICA

    def __init__(self, pcm: bytes | None = None, segundos_reais: float | Sequence[float] | None = None) -> None:
        self.pcm = tom_pcm16(1.0) if pcm is None else pcm
        if segundos_reais is None:
            segundos_reais = (len(self.pcm) // 2) / TAXA_AMOSTRAGEM_PADRAO
        self.segundos_reais = [segundos_reais] if isinstance(segundos_reais, (int, float)) else list(segundos_reais)
        self.abertas = 0
        self.fechadas = 0
        self.gravacoes = 0
        self.descricao = "captura falsa"

    def abrir(self) -> None:
        self.abertas += 1

    def gravar(self, parar: threading.Event, maximo_s: float) -> Captura:
        parar.wait(timeout=5)
        real = self.segundos_reais[min(self.gravacoes, len(self.segundos_reais) - 1)]
        self.gravacoes += 1
        return Captura(self.pcm, real)

    def fechar(self) -> None:
        self.fechadas += 1


def entrada_roteirizada(respostas: Sequence[str]) -> Callable[[str], str]:
    fila = list(respostas)

    def entrada(_pergunta: str) -> str:
        return fila.pop(0) if fila else "q"

    return entrada


def _autoteste() -> int:
    import contextlib
    import io

    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    for lingua in LINGUAS:
        frases = ler_guiao(GUIOES[lingua], lingua)
        verificar(f"guiao {lingua}: cobertura minima", verificar_cobertura(frases), [])

    raiz = (RAIZ / "tmp" / "autoteste-gravar-voz").resolve()
    shutil.rmtree(raiz, ignore_errors=True)
    raiz.mkdir(parents=True)
    try:
        for valor in ("../fora", str(RAIZ.parent / "fora"), "tests"):
            try:
                pasta_de_gravacoes(valor, raiz)
                falhas.append(f"pasta '{valor}' foi aceite")
            except ValueError:
                print(f"ok   pasta '{valor}' recusada")
        pasta = pasta_de_gravacoes(None, raiz)
        frases = ler_guiao(GUIOES["pt"], "pt")[:5]
        projetos = {"<projeto-1>": "exemplo-um", "<projeto-2>": "exemplo-dois"}
        sinais: list[int] = []
        captura = CapturaFalsa()
        # 1.a sessao: grava 2, salta 1, sai.
        with contextlib.redirect_stdout(io.StringIO()):
            resumo = gravar_guiao(
                "pt", frases, projetos, pasta, captura,
                entrada=entrada_roteirizada(["", "", "", "", "s", "q"]),
                tocar_sinal=lambda: sinais.append(1), raiz=raiz,
            )
        verificar("1.a sessao grava 2 frases", resumo.gravadas, ["pt-01", "pt-02"])
        verificar("1.a sessao salta 1", resumo.saltadas, ["pt-03"])
        verificar("sem --com-som nunca ha sinal sonoro", sinais, [])
        manifesto = ler_manifesto(pasta / "pt")
        verificar("manifesto marca a origem sintetica", manifesto["gravacoes"]["pt-01"]["origem"], ORIGEM_SINTETICA)
        verificar("WAV 16 kHz mono escrito", ler_wav_pcm16(pasta / "pt" / "pt-01.wav")[1:], (16_000, 1))
        verificar(
            "manifesto sem caminhos absolutos",
            str(raiz) in json.dumps(manifesto) or str(RAIZ) in json.dumps(manifesto),
            False,
        )
        # 2.a sessao retoma: as duas gravadas nao se repetem.
        with contextlib.redirect_stdout(io.StringIO()):
            resumo = gravar_guiao(
                "pt", frases, projetos, pasta, captura,
                entrada=entrada_roteirizada([""] * 6), com_som=True,
                tocar_sinal=lambda: sinais.append(1), raiz=raiz,
            )
        verificar("retoma onde parou", resumo.gravadas, ["pt-03", "pt-04", "pt-05"])
        verificar("--com-som da um sinal por gravacao", len(sinais), 3)
        # Microfone mudo: nada e gravado.
        with contextlib.redirect_stdout(io.StringIO()):
            resumo = gravar_guiao(
                "pt", frases, projetos, pasta, CapturaFalsa(b"\x00\x00" * 16_000),
                entrada=entrada_roteirizada(["", "", "q"]), refazer=["pt-01"], raiz=raiz,
            )
        verificar("silencio digital nao e gravado", resumo.gravadas, [])
        # Mais audio do que tempo real: recusada, a frase repete-se e a 2.a passa.
        antes = sorted(p.name for p in (pasta / "pt").glob("*.wav"))
        with contextlib.redirect_stdout(io.StringIO()):
            resumo = gravar_guiao(
                "pt", frases, projetos, pasta, CapturaFalsa(tom_pcm16(3.0), segundos_reais=[0.01, 3.0]),
                entrada=entrada_roteirizada([""] * 4), refazer=["pt-01"], raiz=raiz,
            )
        verificar("leitura mais rapida que o relogio recusada e repetida", (resumo.recusadas, resumo.gravadas), (1, ["pt-01"]))
        depois = sorted(p.name for p in (pasta / "pt").glob("*.wav"))
        verificar("--refazer guarda o WAV antigo ao lado", set(antes) <= set(depois) and len(depois) == len(antes) + 1, True)
        # Troco de zeros a meio de uma captura: recusada.
        com_buraco = tom_pcm16(1.0) + b"\x00\x00" * 8_000 + tom_pcm16(0.5)
        with contextlib.redirect_stdout(io.StringIO()):
            resumo = gravar_guiao(
                "pt", frases, projetos, pasta, CapturaFalsa(com_buraco),
                entrada=entrada_roteirizada(["", "", "q"]), refazer=["pt-01"], raiz=raiz,
            )
        verificar("troco de zeros de 0,5 s recusado", (resumo.recusadas, resumo.gravadas), (1, []))
        # Gravacao antiga invalida: volta a ser pedida sem apagar nada.
        escrever_wav_pcm16(pasta / "pt" / "pt-02.wav", com_buraco, TAXA_AMOSTRAGEM_PADRAO)
        manifesto = ler_manifesto(pasta / "pt")
        manifesto["gravacoes"]["pt-02"]["duracao_s"] = round(len(com_buraco) / 2 / TAXA_AMOSTRAGEM_PADRAO, 3)
        manifesto["gravacoes"]["pt-02"].pop("tempo_real_s", None)
        escrever_manifesto(pasta / "pt", manifesto)
        antes = sorted(p.name for p in (pasta / "pt").glob("*.wav"))
        with contextlib.redirect_stdout(io.StringIO()):
            resumo = gravar_guiao(
                "pt", frases, projetos, pasta, CapturaFalsa(),
                entrada=entrada_roteirizada([""] * 2), raiz=raiz,
            )
        depois = sorted(p.name for p in (pasta / "pt").glob("*.wav"))
        verificar("gravacao antiga invalida volta a ser pedida", (list(resumo.invalidas), resumo.gravadas), (["pt-02"], ["pt-02"]))
        verificar("nenhum ficheiro apagado", set(antes) <= set(depois), True)
        verificar("WAV invalido guardado ao lado", any(n.startswith("pt-02.invalida-") for n in depois), True)
        # --verificar com dispositivos falsos: o caso medido passa pelo MME.
        relogio = microfone.RelogioFalso()
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            codigo = verificar_captura(
                microfone.NOME_FALSO, lambda: microfone.PyAudioFalso(relogio=relogio), 8, relogio
            )
        verificar("--verificar escolhe MME e passa", (codigo, "API           = MME" in saida.getvalue()), (0, True))

        def pa_so_com_zeros():
            pa = microfone.PyAudioFalso(relogio=relogio)
            for dispositivo in pa.dispositivos:
                dispositivo["comportamento"] = "zeros"
            return pa

        with contextlib.redirect_stdout(io.StringIO()):
            codigo = verificar_captura(microfone.NOME_FALSO, pa_so_com_zeros, 8, relogio)
        verificar("--verificar falha sem microfone que leia em tempo real", codigo, 1)
    finally:
        shutil.rmtree(raiz, ignore_errors=True)

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste do gravador completo (microfone falso, sem som).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
