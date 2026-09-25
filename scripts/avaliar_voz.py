r"""Avalia os motores STT sobre as gravacoes da voz real do Sponsor.

Le as gravacoes de `recordings/<lingua>/` (feitas por `scripts/gravar_voz.py`
a partir do guiao `tests/voz/guiao-real-<lingua>.md`), transcreve cada uma
com cada motor de `jarvis.stt` (carregado uma vez por motor) e escreve a
evidencia em `docs/forja/evidence/` (ignorada pelo Git), por lingua x motor x
faixa de duracao (<1 s, 1-2 s, >2 s):

  - acerto de intencao contra a intencao esperada no guiao, com o
    interprete ligado (por omissao o encaminhador atual, `jarvis.router`, que
    so conhece comandos locais e texto livre; o interprete novo liga-se pelo
    parametro `interpretar` de `avaliar()`);
  - intencao preservada: o interprete da a mesma intencao sobre a
    transcricao e sobre a frase lida. E a medida que isola o efeito do motor
    STT, seja qual for o interprete;
  - acerto de projeto: o ultimo nome de projeto dito na transcricao, depois
    de tirar a palavra de ativacao do inicio, e o esperado (nenhum, quando a
    frase nao nomeia projeto);
  - WER contra a frase lida;
  - latencia de transcricao p50/p95 (so a inferencia, modelo ja carregado);
  - palavra de ativacao no texto: a transcricao comeca por "hey jarvis" /
    "boas jarvis" quando a frase a tem, e nao comeca quando nao a tem. E uma
    medida de texto; a detecao acustica mede-se a parte.

Um motor que nao pode correr (pacote ou modelo em falta, falha de GPU) fica
no relatorio como SALTADO, com o motivo: nunca ha numeros inventados. Se
alguma gravacao nao veio do microfone (por exemplo as do autoteste), a
evidencia abre com "SEM VOZ DO SPONSOR — não decide nada". Sem gravacoes de
uma lingua, a evidencia diz PENDENTE e o comando exato que o Sponsor corre.

Gravacoes que falham a validacao do gravador (troco de zeros exatos de 0,5 s
ou mais, duracao que nao bate com o manifesto, mais audio do que tempo real)
ficam marcadas como INVALIDAS na evidencia e nunca sao avaliadas: o gravador
volta a pedi-las.

Nunca toca som: so le WAV e escreve texto.

Uso:
    .venv\Scripts\python scripts/avaliar_voz.py
    .venv\Scripts\python scripts/avaliar_voz.py --lingua en --motores parakeet-tdt-0.6b-v3
    .venv\Scripts\python scripts/avaliar_voz.py --device cpu
    .venv\Scripts\python scripts/avaliar_voz.py --motores whisper-medium,parakeet-tdt-0.6b-v3:cpu
    .venv\Scripts\python scripts/avaliar_voz.py --autoteste

Cada motor corre no `--device` pedido, salvo quando a lista lhe fixa outro com
`nome:cuda` ou `nome:cpu`; o device onde cada motor correu fica na evidencia.
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.audio_util import (  # noqa: E402
    TAXA_AMOSTRAGEM_PADRAO,
    caminho_para_mostrar,
    escrever_wav_pcm16,
    ler_wav_pcm16,
    reamostrar_pcm16,
)
from jarvis.config import Config  # noqa: E402
from jarvis.consola import forcar_consola_utf8  # noqa: E402
from jarvis.router import encaminhar  # noqa: E402
from jarvis.stt import MOTORES, MotorBase, MotorIndisponivel, criar_motor  # noqa: E402

PASTA_SCRIPTS = Path(__file__).resolve().parent


def _carregar_modulo_irmao(nome: str):
    """Importa `scripts/<nome>.py` por caminho (mesma chave de cache dos irmaos)."""
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


gravar = _carregar_modulo_irmao("gravar_voz")
medir = _carregar_modulo_irmao("medir_voz")

AVISO_SEM_VOZ = "SEM VOZ DO SPONSOR — não decide nada"

#: Faixas de duracao do audio: (etiqueta, minimo inclusivo, maximo exclusivo).
FAIXAS = (("<1 s", 0.0, 1.0), ("1-2 s", 1.0, 2.0), (">2 s", 2.0, math.inf))
TODAS = "todas"

#: Nome de acao do encaminhador atual -> intencao da lista fechada do guiao.
INTENCAO_DA_ACAO_LOCAL = {
    "horas_e_data": "horas",
    "calar": "calar",
    "adormecer": "dormir",
    "acordar": "acordar",
    "abrir_vscode": "abrir_editor",
    "abrir_pasta": "abrir_pasta",
}

COMANDO_GRAVAR = r".venv\Scripts\python scripts/gravar_voz.py --lingua {lingua}"
COMANDO_AVALIAR = r".venv\Scripts\python scripts/avaliar_voz.py"

#: Todos os motores; o Parakeet fixo em cpu porque o onnxruntime do .venv so
#: tem CPU (com um onnxruntime com CUDA, pedir `parakeet-tdt-0.6b-v3:cuda`).
MOTORES_POR_OMISSAO = ",".join(f"{nome}:cpu" if nome == "parakeet-tdt-0.6b-v3" else nome for nome in MOTORES)


def motores_pedidos(texto: str, device: str) -> list[tuple[str, str]]:
    """'a,b:cpu' -> [(a, device), (b, 'cpu')]. Nome ou device fora da lista -> ValueError."""
    pedidos: list[tuple[str, str]] = []
    for parte in texto.split(","):
        parte = parte.strip()
        if not parte:
            continue
        nome, _, proprio = parte.partition(":")
        nome, proprio = nome.strip(), proprio.strip()
        if nome not in MOTORES:
            raise ValueError(f"motor '{nome}' desconhecido; motores: {', '.join(MOTORES)}")
        if proprio and proprio not in ("cuda", "cpu"):
            raise ValueError(f"device '{proprio}' do motor {nome}: so 'cuda' ou 'cpu'")
        pedidos.append((nome, proprio or device))
    if not pedidos:
        raise ValueError("nenhum motor pedido")
    return pedidos


def faixa_de(duracao_s: float) -> str:
    for etiqueta, minimo, maximo in FAIXAS:
        if minimo <= duracao_s < maximo:
            return etiqueta
    return FAIXAS[-1][0]


# --- Interprete e projeto ---------------------------------------------------------

#: interpretar(texto, config) -> intencao da lista fechada do guiao.
Interpretador = Callable[[str, Config], str]


def intencao_pelo_router(texto: str, config: Config) -> str:
    """O encaminhador atual traduzido para a lista fechada do guiao.

    Acao local -> a intencao correspondente; texto livre para o Claude Code
    -> ditar_prompt; nada (vazio, ruido) -> desconhecido.
    """
    resultado = encaminhar(texto, config)
    if resultado.tipo == "local":
        return INTENCAO_DA_ACAO_LOCAL.get(resultado.nome_acao or "", "desconhecido")
    if resultado.tipo == "claude":
        return "ditar_prompt"
    return "desconhecido"


def sem_palavra_de_ativacao(texto: str, lingua: str) -> str:
    """O texto normalizado sem a palavra de ativacao do inicio, se a tiver.

    E o que o interprete recebe: a palavra de ativacao nunca conta como nome
    de projeto, mesmo quando um projeto se chama como ela.
    """
    palavras = gravar.normalizar(texto).split()
    alvo = gravar.PALAVRA_DE_ATIVACAO[lingua].split()
    if palavras[: len(alvo)] == alvo:
        palavras = palavras[len(alvo) :]
    return " ".join(palavras)


def projeto_mencionado(texto: str, nomes: Iterable[str], lingua: str | None = None) -> str | None:
    """O ULTIMO nome de projeto dito no texto (palavras inteiras, sem acentos).

    O ultimo porque numa correcao ("muda o A para o B") o projeto que conta e
    o de destino. Sem nenhum nome, None: nunca se adivinha um projeto. Com a
    `lingua`, a palavra de ativacao do inicio sai antes da procura.
    """
    base = texto if lingua is None else sem_palavra_de_ativacao(texto, lingua)
    normalizado = f" {gravar.normalizar(base)} "
    melhor: tuple[int, str] | None = None
    for nome in nomes:
        alvo = gravar.normalizar(nome)
        if not alvo:
            continue
        for encontrado in re.finditer(rf"(?<= ){re.escape(alvo)}(?= )", normalizado):
            if melhor is None or encontrado.start() > melhor[0]:
                melhor = (encontrado.start(), nome)
    return None if melhor is None else melhor[1]


# --- Gravacoes ----------------------------------------------------------------------


@dataclass(frozen=True)
class Gravacao:
    frase: object  # gravar_voz.FraseDoGuiao
    lingua: str
    pcm: bytes
    duracao_s: float
    origem: str
    #: A frase tal como foi lida, com os nomes reais dos projetos.
    referencia: str
    #: Nome do projeto esperado (ou None), ja trocado do marcador.
    projeto_esperado: str | None
    #: Nomes dos projetos que estavam no ecra quando esta frase foi gravada.
    projetos_lidos: tuple[str, ...] = ()


@dataclass
class GravacoesDaLingua:
    lingua: str
    gravacoes: list[Gravacao] = field(default_factory=list)
    em_falta: list[str] = field(default_factory=list)
    #: id -> motivo das gravacoes que falham a validacao do gravador (excluidas).
    invalidas: dict[str, str] = field(default_factory=dict)
    projetos: dict[str, str] = field(default_factory=dict)
    problemas: list[str] = field(default_factory=list)


def _projetos_validos(projetos: object) -> dict[str, str]:
    """marcador -> nome, so com os marcadores do guiao; outra coisa -> {}."""
    if not isinstance(projetos, dict):
        return {}
    return {str(k): str(v) for k, v in projetos.items() if k in gravar.MARCADORES_DE_PROJETO}


def carregar_gravacoes(pasta: Path, lingua: str, guiao: Path | None = None) -> GravacoesDaLingua:
    """As gravacoes de uma lingua que o manifesto conhece, cujo WAV existe e e valido.

    As que falham a validacao do gravador ficam em `invalidas`, com o motivo,
    e nao entram em `gravacoes`.
    """
    frases = gravar.ler_guiao(gravar.GUIOES[lingua] if guiao is None else guiao, lingua)
    pasta_lingua = pasta / lingua
    manifesto = gravar.ler_manifesto(pasta_lingua)
    resultado = GravacoesDaLingua(lingua)
    projetos = manifesto.get("projetos")
    resultado.projetos = _projetos_validos(projetos)
    existentes = gravar.ja_gravadas(pasta_lingua, manifesto)
    registos = manifesto.get("gravacoes", {})
    for frase in frases:
        if frase.id not in existentes:
            resultado.em_falta.append(frase.id)
            continue
        registo = registos[frase.id]
        # Cada gravacao guarda os nomes que estavam no ecra; os manifestos
        # antigos so os tinham ao nivel do manifesto.
        projetos_da_frase = _projetos_validos(registo.get("projetos")) or resultado.projetos
        if any(m in frase.frase for m in gravar.MARCADORES_DE_PROJETO) and not projetos_da_frase:
            resultado.problemas.append(f"{frase.id}: o manifesto nao diz que projetos foram lidos; saltada")
            continue
        ficheiro = str(registo["ficheiro"])
        if Path(ficheiro).name != ficheiro or not ficheiro.lower().endswith(".wav"):
            # O manifesto e so um ficheiro local: nunca serve para ler fora da pasta da lingua.
            resultado.problemas.append(f"{frase.id}: ficheiro do manifesto nao e um .wav desta pasta; saltada")
            continue
        try:
            pcm, taxa, canais = ler_wav_pcm16(pasta_lingua / ficheiro)
        except (OSError, ValueError, EOFError) as erro:
            resultado.problemas.append(f"{frase.id}: WAV ilegivel ({type(erro).__name__}); saltada")
            continue
        motivo = gravar.motivo_de_gravacao_invalida(pcm, taxa, canais, registo)
        if motivo:
            resultado.invalidas[frase.id] = motivo
            continue
        if taxa != TAXA_AMOSTRAGEM_PADRAO:
            pcm = reamostrar_pcm16(pcm, taxa, TAXA_AMOSTRAGEM_PADRAO)
        resultado.gravacoes.append(
            Gravacao(
                frase=frase,
                lingua=lingua,
                pcm=pcm,
                duracao_s=(len(pcm) // 2) / TAXA_AMOSTRAGEM_PADRAO,
                origem=str(registo.get("origem", "desconhecida")),
                referencia=gravar.texto_a_ler(frase, projetos_da_frase),
                projeto_esperado=projetos_da_frase.get(frase.projeto) if frase.projeto else None,
                projetos_lidos=tuple(projetos_da_frase.values()),
            )
        )
    return resultado


# --- Avaliacao ----------------------------------------------------------------------


@dataclass(frozen=True)
class LinhaAvaliada:
    id: str
    lingua: str
    motor: str
    caso: str
    duracao_s: float
    faixa: str
    origem: str
    referencia: str
    transcricao: str
    latencia_ms: float
    distancia: int
    palavras_referencia: int
    wer: float
    intencao_esperada: str
    intencao_obtida: str
    intencao_da_referencia: str
    projeto_esperado: str | None
    projeto_obtido: str | None
    ativacao_esperada: bool
    ativacao_detetada: bool

    @property
    def intencao_certa(self) -> bool:
        return self.intencao_obtida == self.intencao_esperada

    @property
    def intencao_preservada(self) -> bool:
        return self.intencao_obtida == self.intencao_da_referencia

    @property
    def projeto_certo(self) -> bool:
        return self.projeto_obtido == self.projeto_esperado

    @property
    def tudo_certo(self) -> bool:
        return self.intencao_certa and self.projeto_certo


@dataclass
class ResultadoDaAvaliacao:
    linhas: list[LinhaAvaliada] = field(default_factory=list)
    #: motor -> motivo por que nao correu (ou foi descartado a meio).
    saltados: dict[str, str] = field(default_factory=dict)
    #: motor -> latencia de carregamento em ms.
    carregamento_ms: dict[str, float] = field(default_factory=dict)
    #: motor -> device onde correu.
    devices: dict[str, str] = field(default_factory=dict)
    #: motor -> latencia da inferencia de aquecimento (descartada), em ms.
    aquecimento_ms: dict[str, float] = field(default_factory=dict)
    #: As gravacoes carregadas por lingua (com as invalidas), preenchido por `correr`.
    por_lingua: list[GravacoesDaLingua] = field(default_factory=list)


def avaliar(
    motores: Sequence[MotorBase],
    gravacoes: Sequence[Gravacao],
    config: Config,
    interpretar: Interpretador = intencao_pelo_router,
) -> ResultadoDaAvaliacao:
    """Cada motor transcreve todas as gravacoes; um motor de cada vez na VRAM.

    Um motor que falha ao carregar, ou a meio, e SALTADO por inteiro com o
    motivo: as linhas que ja tinha sao descartadas, nunca se mistura um
    resultado parcial com os dos outros motores.
    """
    resultado = ResultadoDaAvaliacao()
    # Os nomes lidos contam mesmo que o config.toml tenha mudado depois.
    nomes = list(dict.fromkeys([p.nome for p in config.projetos] + [n for g in gravacoes for n in g.projetos_lidos]))
    referencias: dict[tuple[str, str], str] = {}
    for gravacao in gravacoes:
        chave = (gravacao.lingua, gravacao.frase.id)
        referencias[chave] = interpretar(gravacao.referencia, config)
    for motor in motores:
        try:
            motor.carregar()
        except MotorIndisponivel as erro:
            resultado.saltados[motor.nome] = str(erro)
            continue
        resultado.carregamento_ms[motor.nome] = motor.latencia_carregamento_ms or 0.0
        resultado.devices[motor.nome] = motor.device_real or motor.device
        linhas: list[LinhaAvaliada] = []
        try:
            if gravacoes:
                # A primeira inferencia paga o aquecimento (CUDA, caches): fica
                # registada a parte e fora dos p50/p95.
                aquecimento = motor.transcrever(gravacoes[0].pcm, lingua=gravacoes[0].lingua)
                resultado.aquecimento_ms[motor.nome] = aquecimento.latencia_ms
            for gravacao in gravacoes:
                frase = gravacao.frase
                transcricao = motor.transcrever(gravacao.pcm, lingua=gravacao.lingua)
                wer = medir.calcular_wer(gravacao.referencia, transcricao.texto)
                linhas.append(
                    LinhaAvaliada(
                        id=frase.id,
                        lingua=gravacao.lingua,
                        motor=motor.nome,
                        caso=frase.caso,
                        duracao_s=gravacao.duracao_s,
                        faixa=faixa_de(gravacao.duracao_s),
                        origem=gravacao.origem,
                        referencia=gravacao.referencia,
                        transcricao=transcricao.texto,
                        latencia_ms=transcricao.latencia_ms,
                        distancia=wer.distancia_edicao,
                        palavras_referencia=wer.n_palavras_referencia,
                        wer=wer.wer,
                        intencao_esperada=frase.intencao,
                        intencao_obtida=interpretar(transcricao.texto, config),
                        intencao_da_referencia=referencias[(gravacao.lingua, frase.id)],
                        projeto_esperado=gravacao.projeto_esperado,
                        projeto_obtido=projeto_mencionado(transcricao.texto, nomes, gravacao.lingua),
                        ativacao_esperada=frase.ativacao,
                        ativacao_detetada=gravar.comeca_pela_ativacao(transcricao.texto, gravacao.lingua),
                    )
                )
        except Exception as erro:  # GPU sem memoria, DLL, erro do modelo
            resultado.saltados[motor.nome] = (
                f"{motor.nome}: falhou a transcrever ({type(erro).__name__}: {erro}); "
                "resultados parciais descartados"
            )
            linhas = []
        finally:
            motor.libertar()
        resultado.linhas.extend(linhas)
    return resultado


@dataclass(frozen=True)
class Agregado:
    lingua: str
    motor: str
    faixa: str
    n: int
    intencao_certa: int
    intencao_preservada: int
    projeto_certo: int
    tudo_certo: int
    wer: float
    p50_ms: float
    p95_ms: float
    ativacao_esperadas: int
    ativacao_detetadas: int
    sem_ativacao: int
    ativacao_falsas: int


def agregar(linhas: Sequence[LinhaAvaliada]) -> list[Agregado]:
    """Por lingua x motor, primeiro 'todas' e depois cada faixa com linhas."""
    grupos: dict[tuple[str, str], list[LinhaAvaliada]] = {}
    for linha in linhas:
        grupos.setdefault((linha.lingua, linha.motor), []).append(linha)
    agregados: list[Agregado] = []
    for (lingua, motor), do_grupo in grupos.items():
        faixas = [(TODAS, do_grupo)] + [
            (etiqueta, [l for l in do_grupo if l.faixa == etiqueta]) for etiqueta, _, _ in FAIXAS
        ]
        for faixa, subset in faixas:
            if not subset:
                continue
            palavras = sum(l.palavras_referencia for l in subset)
            latencias = [l.latencia_ms for l in subset]
            com = [l for l in subset if l.ativacao_esperada]
            sem = [l for l in subset if not l.ativacao_esperada]
            agregados.append(
                Agregado(
                    lingua=lingua,
                    motor=motor,
                    faixa=faixa,
                    n=len(subset),
                    intencao_certa=sum(l.intencao_certa for l in subset),
                    intencao_preservada=sum(l.intencao_preservada for l in subset),
                    projeto_certo=sum(l.projeto_certo for l in subset),
                    tudo_certo=sum(l.tudo_certo for l in subset),
                    wer=(sum(l.distancia for l in subset) / palavras) if palavras else 0.0,
                    p50_ms=medir.percentil(latencias, 0.50),
                    p95_ms=medir.percentil(latencias, 0.95),
                    ativacao_esperadas=len(com),
                    ativacao_detetadas=sum(l.ativacao_detetada for l in com),
                    sem_ativacao=len(sem),
                    ativacao_falsas=sum(l.ativacao_detetada for l in sem),
                )
            )
    return agregados


# --- Evidencia ----------------------------------------------------------------------


def vram_livre() -> str | None:
    """Memoria livre/total da GPU segundo o nvidia-smi, ou None se nao houver."""
    executavel = shutil.which("nvidia-smi")
    if executavel is None:
        return None
    try:
        saida = subprocess.run(
            [executavel, "--query-gpu=memory.free,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip().splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    if not saida:
        return None
    partes = [p.strip() for p in saida[0].split(",")]
    if len(partes) != 2 or not all(p.isdigit() for p in partes):
        return None
    return f"{partes[0]} MiB livres de {partes[1]} MiB"


def _pct(parte: int, total: int) -> str:
    return f"{parte}/{total} ({100 * parte / total:.0f}%)" if total else "—"


def _celula(texto: object) -> str:
    return medir.celula_markdown(texto)


def evidencia_markdown(
    por_lingua: Sequence[GravacoesDaLingua],
    resultado: ResultadoDaAvaliacao,
    motores_pedidos: Sequence[str],
    origem_config: str,
    device: str,
    vram_antes: str | None,
    vram_depois: str | None,
) -> str:
    origens = {g.origem for dados in por_lingua for g in dados.gravacoes}
    sem_voz = bool(origens - {gravar.ORIGEM_MICROFONE})
    linhas: list[str] = [f"# Avaliação STT com a voz real — {datetime.now().isoformat(timespec='seconds')}", ""]
    if sem_voz:
        linhas += [
            f"**{AVISO_SEM_VOZ}.** Há gravações que não vieram do microfone "
            f"(origens: {', '.join(sorted(origens))}). Estes números testam a cadeia, "
            "não a pronúncia do Sponsor; nenhuma escolha de motor ou de língua sai daqui.",
            "",
        ]
    pendentes = [d for d in por_lingua if not d.gravacoes]
    for dados in pendentes:
        nenhuma = "Não há gravações válidas desta língua" if dados.invalidas else "Não há gravações desta língua"
        linhas += [
            f"**PENDENTE — passo do Sponsor ({dados.lingua}).** {nenhuma}. "
            f"Gravar (~10 min): `{COMANDO_GRAVAR.format(lingua=dados.lingua)}`; depois correr "
            f"`{COMANDO_AVALIAR}`.",
            "",
        ]
    for dados in por_lingua:
        if dados.invalidas:
            linhas += [
                f"**GRAVAÇÕES INVÁLIDAS ({dados.lingua}): {len(dados.invalidas)}, excluídas da avaliação.** "
                f"`{COMANDO_GRAVAR.format(lingua=dados.lingua)}` volta a pedi-las (nada é apagado). "
                "Motivos na secção 'Gravações inválidas'.",
                "",
            ]
    linhas += [
        "## Condições",
        "",
        f"- Configuração: {origem_config}",
        f"- Device pedido: {device}",
        f"- VRAM antes: {vram_antes or 'não medida (sem nvidia-smi)'}; depois: {vram_depois or 'não medida'}",
        f"- Motores pedidos: {', '.join(motores_pedidos)}",
        "- Intenção: interprete = encaminhador atual (`jarvis.router`); 'preservada' = mesma intenção "
        "na transcrição e na frase lida (isola o efeito do STT).",
        "- Projeto: último nome de projeto (da configuração ou lido na gravação) dito na transcrição, "
        "depois de tirar a palavra de ativação do início.",
        "- Latência: só a inferência com o modelo carregado; a primeira inferência de cada motor é "
        "aquecimento e fica fora do p50/p95.",
        "- Ativação: medida de texto (a transcrição começa pela palavra de ativação), não acústica.",
        "",
        "## Gravações",
        "",
        "| língua | avaliadas | inválidas (excluídas) | em falta | problemas |",
        "|---|---|---|---|---|",
    ]
    for dados in por_lingua:
        linhas.append(
            f"| {dados.lingua} | {len(dados.gravacoes)} | {len(dados.invalidas)} | {len(dados.em_falta)} | "
            f"{_celula('; '.join(dados.problemas) or '—')} |"
        )
    invalidas = [(dados.lingua, i, m) for dados in por_lingua for i, m in dados.invalidas.items()]
    if invalidas:
        linhas += ["", "## Gravações inválidas", "", "| língua | id | motivo |", "|---|---|---|"]
        linhas += [f"| {lingua} | {id_} | {_celula(motivo)} |" for lingua, id_, motivo in invalidas]
    linhas += [
        "",
        "## Motores",
        "",
        "| motor | estado | device | carregamento | aquecimento (fora do p50/p95) |",
        "|---|---|---|---|---|",
    ]
    for nome in motores_pedidos:
        if nome in resultado.saltados:
            linhas.append(f"| {nome} | SALTADO: {_celula(resultado.saltados[nome])} | — | — | — |")
        elif nome not in resultado.devices:
            linhas.append(f"| {nome} | não corrido (sem gravações) | — | — | — |")
        else:
            aquecimento = resultado.aquecimento_ms.get(nome)
            texto_aquecimento = "—" if aquecimento is None else f"{aquecimento:.0f} ms"
            linhas.append(
                f"| {nome} | avaliado | {resultado.devices.get(nome, '—')} | "
                f"{resultado.carregamento_ms.get(nome, 0.0):.0f} ms | {texto_aquecimento} |"
            )
    linhas += [
        "",
        "## Resultados por língua x motor x faixa",
        "",
        "| língua | motor | faixa | n | intenção certa | intenção preservada | projeto certo | "
        "intenção+projeto | WER | p50 | p95 | ativação detetada | ativação falsa |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for a in agregar(resultado.linhas):
        linhas.append(
            f"| {a.lingua} | {a.motor} | {a.faixa} | {a.n} | {_pct(a.intencao_certa, a.n)} | "
            f"{_pct(a.intencao_preservada, a.n)} | {_pct(a.projeto_certo, a.n)} | {_pct(a.tudo_certo, a.n)} | "
            f"{100 * a.wer:.1f}% | {a.p50_ms:.0f} ms | {a.p95_ms:.0f} ms | "
            f"{_pct(a.ativacao_detetadas, a.ativacao_esperadas)} | {_pct(a.ativacao_falsas, a.sem_ativacao)} |"
        )
    if not resultado.linhas:
        linhas.append("| — | — | — | 0 | — | — | — | — | — | — | — | — | — |")
    linhas += [
        "",
        "## Frase a frase",
        "",
        "| id | motor | dur. | lat. | WER | intenção (esperada → obtida) | projeto | ativação | transcrição |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for l in resultado.linhas:
        marca_i = "ok" if l.intencao_certa else "X"
        marca_p = "ok" if l.projeto_certo else f"X ({l.projeto_obtido or 'nenhum'})"
        marca_a = "ok" if l.ativacao_detetada == l.ativacao_esperada else "X"
        linhas.append(
            f"| {l.id} | {l.motor} | {l.duracao_s:.2f} s | {l.latencia_ms:.0f} ms | {100 * l.wer:.0f}% | "
            f"{marca_i} {l.intencao_esperada} → {l.intencao_obtida} | {_celula(marca_p)} | {marca_a} | "
            f"{_celula(l.transcricao)} |"
        )
    linhas.append("")
    return "\n".join(linhas)


# --- Linha de comandos --------------------------------------------------------------


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Avalia os motores STT sobre as gravacoes da voz real (recordings/).",
        epilog="Nunca toca som. A evidencia fica em docs/forja/evidence/ (ignorada pelo Git).",
    )
    parser.add_argument("--lingua", choices=gravar.LINGUAS, action="append", help="por omissao as duas")
    parser.add_argument(
        "--motores",
        default=MOTORES_POR_OMISSAO,
        help=f"lista separada por virgulas de {', '.join(MOTORES)}; 'nome:cpu' fixa o device desse motor",
    )
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--pasta", default=None, help="pasta das gravacoes, dentro de recordings/")
    parser.add_argument("--saida", default=None, help="ficheiro .md dentro de docs/forja/evidence/")
    parser.add_argument("--autoteste", action="store_true", help="motores falsos e WAV sinteticos em tmp/")
    return parser


def correr(
    linguas: Sequence[str],
    motores: Sequence[MotorBase],
    config: Config,
    origem_config: str,
    pasta: Path,
    saida: Path,
    device: str,
    interpretar: Interpretador = intencao_pelo_router,
    guioes: dict[str, Path] | None = None,
) -> tuple[ResultadoDaAvaliacao, str]:
    por_lingua = [carregar_gravacoes(pasta, l, None if guioes is None else guioes[l]) for l in linguas]
    gravacoes = [g for dados in por_lingua for g in dados.gravacoes]
    vram_antes = vram_livre() if device == "cuda" else None
    resultado = avaliar(motores if gravacoes else [], gravacoes, config, interpretar)
    resultado.por_lingua = por_lingua
    vram_depois = vram_livre() if device == "cuda" else None
    texto = evidencia_markdown(
        por_lingua, resultado, [m.nome for m in motores], origem_config, device, vram_antes, vram_depois
    )
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(texto, encoding="utf-8")
    return resultado, texto


def main(argv: Sequence[str] | None = None) -> int:
    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)
    if args.autoteste:
        return _autoteste()
    linguas = args.lingua or list(gravar.LINGUAS)
    try:
        motores = [criar_motor(nome, device) for nome, device in motores_pedidos(args.motores, args.device)]
        pasta = gravar.pasta_de_gravacoes(args.pasta)
        carimbo = datetime.now().strftime("%Y%m%d-%H%M%S")
        saida = medir.caminho_evidencia_de_saida(args.saida or f"docs/forja/evidence/avaliar-voz-{carimbo}.md")
    except ValueError as erro:
        print(f"erro: {erro}", file=sys.stderr)
        return 2
    config, origem = medir.carregar_config_para_arnes()
    resultado, texto = correr(linguas, motores, config, origem, pasta, saida, args.device)
    for nome, motivo in resultado.saltados.items():
        print(f"SALTADO {nome}: {motivo}")
    for a in agregar(resultado.linhas):
        if a.faixa == TODAS:
            print(
                f"{a.lingua} {a.motor}: intencao+projeto {_pct(a.tudo_certo, a.n)}, "
                f"preservada {_pct(a.intencao_preservada, a.n)}, WER {100 * a.wer:.1f}%, "
                f"p50 {a.p50_ms:.0f} ms, p95 {a.p95_ms:.0f} ms"
            )
    if AVISO_SEM_VOZ in texto:
        print(AVISO_SEM_VOZ)
    invalidas = sum(len(d.invalidas) for d in resultado.por_lingua)
    if invalidas:
        print(f"INVALIDAS: {invalidas} gravacoes excluidas (zeros exatos ou duracao incoerente); gravar_voz.py volta a pedi-las.")
    if "PENDENTE" in texto:
        print("PENDENTE: faltam gravacoes do Sponsor (ver a evidencia).")
    print(f"Evidencia: {caminho_para_mostrar(saida)}")
    return 0


# --- Autoteste: motores falsos, WAV sinteticos em tmp/, sem som ----------------------


class _MotorQueRepete(MotorBase):
    """Devolve a frase lida (por duracao do audio): o motor perfeito."""

    def __init__(self, nome: str, textos_por_amostras: dict[int, str]) -> None:
        super().__init__("cpu")
        self.nome = nome
        self.textos = textos_por_amostras
        self.carregamentos = 0

    def _carregar_modelo(self):
        self.carregamentos += 1
        return object()

    def _inferir(self, modelo, pcm16, lingua):
        return self.textos.get(len(pcm16) // 2, ""), lingua, False


class _MotorSurdo(MotorBase):
    """Devolve sempre o mesmo ruido: tudo errado, nada inventado."""

    nome = "surdo"

    def __init__(self) -> None:
        super().__init__("cpu")

    def _carregar_modelo(self):
        return object()

    def _inferir(self, modelo, pcm16, lingua):
        return "obrigado", lingua, False


class _MotorAusente(MotorBase):
    nome = "ausente"

    def __init__(self) -> None:
        super().__init__("cpu")

    def _carregar_modelo(self):
        raise MotorIndisponivel("ausente: pacote por instalar (autoteste)")


class _MotorQueRebenta(_MotorSurdo):
    nome = "rebenta"

    def __init__(self) -> None:
        super().__init__()
        self.chamadas = 0

    def _inferir(self, modelo, pcm16, lingua):
        self.chamadas += 1
        if self.chamadas > 2:
            raise RuntimeError("CUDA out of memory (simulado)")
        return "obrigado", lingua, False


#: Duracoes dos WAV do autoteste, a cobrir as tres faixas.
DURACOES_AUTOTESTE = (0.5, 0.8, 1.2, 1.6, 2.4, 3.0)


def preparar_gravacoes_sinteticas(pasta: Path, lingua: str, projetos: dict[str, str], n: int) -> dict[int, str]:
    """Escreve n WAV (tons) + manifesto de origem sintetica; devolve amostras -> frase lida."""
    frases = gravar.ler_guiao(gravar.GUIOES[lingua], lingua)[:n]
    pasta_lingua = pasta / lingua
    manifesto = {"versao": 1, "lingua": lingua, "projetos": dict(projetos), "gravacoes": {}}
    textos: dict[int, str] = {}
    for indice, frase in enumerate(frases):
        # Duracoes todas diferentes: o motor falso reconhece a frase pelo tamanho.
        segundos = DURACOES_AUTOTESTE[indice % len(DURACOES_AUTOTESTE)] + 0.01 * indice
        pcm = gravar.tom_pcm16(segundos)
        caminho = pasta_lingua / f"{frase.id}.wav"
        escrever_wav_pcm16(caminho, pcm, TAXA_AMOSTRAGEM_PADRAO, canais=1)
        manifesto["gravacoes"][frase.id] = {
            "ficheiro": caminho.name,
            "origem": gravar.ORIGEM_SINTETICA,
            "duracao_s": round(len(pcm) / 2 / TAXA_AMOSTRAGEM_PADRAO, 3),
        }
        textos[len(pcm) // 2] = gravar.texto_a_ler(frase, projetos)
    gravar.escrever_manifesto(pasta_lingua, manifesto)
    return textos


def _config_ficticia() -> Config:
    from jarvis.config import Projeto

    return Config(
        microfone="",
        projetos=(Projeto("exemplo-um", RAIZ / "tmp" / "exemplo-um"), Projeto("exemplo-dois", RAIZ / "tmp" / "exemplo-dois")),
    )


def _autoteste() -> int:
    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    config = _config_ficticia()
    projetos = {"<projeto-1>": "exemplo-um", "<projeto-2>": "exemplo-dois"}
    verificar("faixas de duracao", [faixa_de(0.5), faixa_de(1.0), faixa_de(1.99), faixa_de(2.0)], ["<1 s", "1-2 s", "1-2 s", ">2 s"])
    verificar("projeto: o ultimo dito", projeto_mencionado("muda o exemplo um para o exemplo-dois", ["exemplo-um", "exemplo-dois"]), "exemplo-dois")
    verificar("projeto: nunca adivinhado", projeto_mencionado("abre o exemplo", ["exemplo-um"]), None)
    # Um projeto pode chamar-se como a ultima palavra da palavra de ativacao.
    verificar("projeto: a palavra de ativacao nao conta (en)", projeto_mencionado("Hey Jarvis, what time is it?", ["exemplo-um", "jarvis"], "en"), None)
    verificar("projeto: a palavra de ativacao nao conta (pt)", projeto_mencionado("boas jarvis, dorme", ["exemplo-um", "jarvis"], "pt"), None)
    verificar("projeto: com o mesmo nome dito depois", projeto_mencionado("hey jarvis, open the jarvis folder", ["exemplo-um", "jarvis"], "en"), "jarvis")
    verificar("router: horas", intencao_pelo_router("que horas são", config), "horas")
    verificar("router: ditado", intencao_pelo_router("pede ao exemplo-um para corrigir os testes", config), "ditar_prompt")

    raiz = (RAIZ / "tmp" / "autoteste-avaliar-voz").resolve()
    shutil.rmtree(raiz, ignore_errors=True)
    try:
        pasta = gravar.pasta_de_gravacoes(None, raiz)
        textos = preparar_gravacoes_sinteticas(pasta, "pt", projetos, 12)
        evidencia = raiz / "docs" / "forja" / "evidence"
        saida = medir.caminho_evidencia_de_saida(evidencia / "avaliar.md", evidencia, raiz)
        perfeito = _MotorQueRepete("perfeito", textos)
        motores = [perfeito, _MotorSurdo(), _MotorAusente(), _MotorQueRebenta()]
        resultado, texto = correr(["pt", "en"], motores, config, "ficticia", pasta, saida, "cpu")
        verificar("evidencia escrita", saida.is_file(), True)
        verificar("aviso de voz sintetica", AVISO_SEM_VOZ in texto, True)
        verificar("lingua sem gravacoes fica PENDENTE com o comando", "PENDENTE" in texto and "--lingua en" in texto, True)
        verificar("motor indisponivel saltado", "ausente" in resultado.saltados, True)
        verificar("motor que falha a meio saltado sem linhas", ("rebenta" in resultado.saltados, any(l.motor == "rebenta" for l in resultado.linhas)), (True, False))
        verificar("carrega uma vez", perfeito.carregamentos, 1)
        por_motor = {(a.motor, a.faixa): a for a in agregar(resultado.linhas)}
        a = por_motor[("perfeito", TODAS)]
        verificar("perfeito: 12 frases", a.n, 12)
        verificar("perfeito: WER 0", a.wer, 0.0)
        verificar("perfeito: intencao preservada 100%", a.intencao_preservada, 12)
        verificar("perfeito: projeto 100%", a.projeto_certo, 12)
        verificar("perfeito: ativacao detetada em todas", (a.ativacao_detetadas, a.ativacao_falsas), (a.ativacao_esperadas, 0))
        verificar("tres faixas presentes", {f for (m, f) in por_motor if m == "perfeito"}, {TODAS, "<1 s", "1-2 s", ">2 s"})
        s = por_motor[("surdo", TODAS)]
        verificar("surdo: nenhuma ativacao", s.ativacao_detetadas, 0)
        verificar("surdo: WER alto", s.wer > 0.9, True)
        verificar("evidencia sem caminhos absolutos", str(RAIZ) in texto, False)
        try:
            medir.caminho_evidencia_de_saida(raiz / "fora.md", evidencia, raiz)
            falhas.append("evidencia fora da pasta aceite")
        except ValueError:
            print("ok   evidencia fora de docs/forja/evidence recusada")
        # Com todas as gravacoes vindas do microfone, o aviso desaparece.
        manifesto = gravar.ler_manifesto(pasta / "pt")
        for registo in manifesto["gravacoes"].values():
            registo["origem"] = gravar.ORIGEM_MICROFONE
        gravar.escrever_manifesto(pasta / "pt", manifesto)
        _, texto = correr(["pt"], [_MotorQueRepete("perfeito", textos)], config, "ficticia", pasta, saida, "cpu")
        verificar("so microfone: sem aviso", AVISO_SEM_VOZ in texto, False)
        # Gravacoes como as do DirectSound (zeros a seguir a fala) e com duracao
        # que nao bate com o manifesto: marcadas e excluidas, nunca avaliadas.
        com_zeros = gravar.tom_pcm16(0.7) + b"\x00\x00" * 16_000
        escrever_wav_pcm16(pasta / "pt" / "pt-01.wav", com_zeros, TAXA_AMOSTRAGEM_PADRAO)
        manifesto = gravar.ler_manifesto(pasta / "pt")
        manifesto["gravacoes"]["pt-01"]["duracao_s"] = 1.7
        manifesto["gravacoes"]["pt-02"]["duracao_s"] = 9.0
        gravar.escrever_manifesto(pasta / "pt", manifesto)
        motor = _MotorQueRepete("perfeito", textos)
        resultado, texto = correr(["pt"], [motor], config, "ficticia", pasta, saida, "cpu")
        dados = resultado.por_lingua[0]
        verificar("invalidas marcadas", sorted(dados.invalidas), ["pt-01", "pt-02"])
        verificar("invalidas excluidas da avaliacao", {l.id for l in resultado.linhas} & {"pt-01", "pt-02"}, set())
        verificar("evidencia marca as invalidas", "GRAVAÇÕES INVÁLIDAS (pt): 2" in texto and "silencio digital" in texto, True)
    finally:
        shutil.rmtree(raiz, ignore_errors=True)

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste do avaliador completo (motores falsos, sem som).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
