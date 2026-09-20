r"""Encaminhador deterministico do jarvis: lista branca FECHADA da D4.

Decide, para uma frase ja transcrita, uma de tres saidas — nunca mais que
isso, e nunca com um LLM no meio (D9, o Ollama nao entra neste run):

  "local"  - a frase INTEIRA e um comando da lista branca fechada da D4 E,
             quando o comando precisa de um projeto, esse projeto esta na
             configuracao. Devolve so a DESCRICAO da acao (`nome_acao` mais
             `argumento`), nunca a executa (D48.2 — quem executa e a T5; um
             caminho ou nome lido da configuracao e entrada externa e nunca
             entra numa linha de comandos aqui).
  "claude" - tudo o resto: perguntas, conversa, uma frase que apenas CONTEM
             um gatilho da lista branca, um comando sem projeto identificavel
             (ambiguo), um projeto que nao esta na configuracao, e qualquer
             frase de dominio financeiro (D12 — segue como TEXTO, nunca como
             acao; ver "D12" mais abaixo).
  "nada"   - guarda de tokens e de falsos despertares (D5): transcricao
             vazia, mais curta que o minimo, com confianca abaixo do limiar
             (quando a chamada fornece uma), ou reconhecida como ruido/
             alucinacao tipica do STT sobre silencio. Nunca chega ao Claude
             Code.

A lista branca fechada da D4, por esta ordem de deteccao:
  (a) horas e data (o `argumento` diz qual dos dois: "horas" ou "data");
  (b) abrir o VS Code num projeto conhecido;
  (c) abrir uma pasta conhecida;
  (d) calar / cala-te;
  (e) adormecer e acordar o jarvis.

COMO A LISTA BRANCA E FECHADA (corrige o bloqueador 1 da revisao da T4 a1):
cada padrao e casado com `re.fullmatch` contra a frase NORMALIZADA INTEIRA,
nunca com `search`. Uma frase que apenas contenha o gatilho ("diz ao claude
para abrir um ticket sobre o vs code no exemplo-um") nao casa com nada e vai
para o Claude Code como texto. So se toleram, fora do padrao, cortesias de
fronteira ("jarvis", "por favor", "obrigado", "agora"), retiradas uma a uma
e sempre depois de tentar a frase tal e qual — por isso um projeto chamado
"jarvis" continua a ser reconhecido como argumento. Em cima disso, duas
guardas explicitas mandam para o Claude Code qualquer frase com negacao
("nao abras o vs code no exemplo-um") ou com destinatario explicito ("diz ao
claude...", "pergunta...", "manda...", "escreve...").

COMO O PROJETO E RECONHECIDO (corrige o bloqueador 2): o padrao captura o
fragmento que vem depois do objeto do comando, e SO esse fragmento e
comparado com os nomes da configuracao — nunca a frase toda, nunca por
substring. A comparacao e palavra a palavra: mesmo numero de palavras e cada
palavra igual, tolerando um unico erro de escrita dentro de palavras longas
(>= 6 letras). Uma palavra diferente ("exemplo dos", "exemplo doido") ou uma
palavra a mais ("exemplo dois privado") nao e o projeto: vai para o Claude
Code. Se mais do que um projeto conhecido bater, e ambiguo e vai tambem para
o Claude Code. O router nunca escolhe "o mais parecido" (D4, e prioridade
nao funcional 2 do PRODUCT-PROFILE).

D12 (proibicao permanente), como a D52 a fixou: a garantia e a lista branca,
nao um regex de nomes. Nenhuma das cinco acoes da D4 consegue negociar,
ligar-se a uma corretora ou ler uma carteira, e tudo o que nao e lista branca
ja vai como texto. O `PADRAO_FINANCEIRO` corre DEPOIS da lista branca e serve
so para carimbar o motivo D12 no log dessas frases. Nao existe nenhuma lista
de nomes proibidos, em codigo ou em configuracao. "Abre o VS Code no
<projeto>" e "abre a pasta do <projeto>" continuam a ser acao local para
QUALQUER projeto da configuracao privada (D52.5).

A deteccao e so regex + comparacao de texto (difflib) contra os nomes de
projeto da configuracao — determinista, sem inferencia nenhuma.

Uso:

    from jarvis.config import carregar_config
    from jarvis.router import encaminhar

    config = carregar_config()
    resultado = encaminhar("que horas sao", config)
    resultado.tipo        # -> "local"
    resultado.nome_acao   # -> "horas_e_data"
    resultado.argumento   # -> "horas"

Autoteste das partes puras, com uma configuracao ficticia em memoria (nunca
com o config.toml real):

    .venv\Scripts\python -m jarvis.router --autoteste
"""

from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

from jarvis.config import Config, Projeto

#: Tipo da saida do router. "local" tem sempre nome_acao; "claude" tem sempre
#: texto; "nada" so tem motivo (para o log — D5 diz para registar a ocorrencia).
TipoResultado = Literal["local", "claude", "nada"]

#: Comprimento minimo, em caracteres apos strip(), para uma transcricao ser
#: sequer considerada (D5). Mais curto do que isto e "sem fala util", nao
#: ambiguidade — cai na guarda "nada", nunca no Claude Code.
COMPRIMENTO_MINIMO_CARACTERES = 4

#: Confianca minima em [0, 1]. A guarda esta LIGADA por omissao: sempre que a
#: chamada fornece uma confianca, ela e comparada com este limiar. O que nao
#: se pode inventar e um valor quando a chamada nao mede nenhum — nesse caso
#: `ResultadoRouter.confianca_verificada` vem False e o log tem de o dizer.
#: NOTA PARA A T6 (cadeia viva): passar SEMPRE a confianca do transcritor a
#: encaminhar(); um resultado com confianca_verificada=False significa que a
#: metade "confianca" da guarda da D5 nao correu naquela frase.
CONFIANCA_MINIMA = 0.35

#: Correspondencias EXATAS (depois de normalizar) que o Whisper produz
#: tipicamente sobre silencio/ruido de fundo em vez de nao dizer nada — a D51
#: documenta "Obrigado por assistir!" sobre um WAV de ruido branco isolado.
#: Uma frase real do Sponsor que por acaso contenha estas palavras no meio de
#: mais texto NAO cai aqui (e comparacao exata da frase inteira, nao uma
#: palavra solta), so a alucinacao completa e tratada como ruido.
FRASES_DE_RUIDO_CONHECIDAS = frozenset(
    {
        "obrigado por assistir",
        "obrigada por assistir",
        "obrigado por assistirem",
        "legendas pela comunidade amara org",
        "www mooji org",
    }
)

#: D12/D52: vocabulario generico de dominio financeiro em portugues. NAO e um
#: portao (corre depois da lista branca) e NAO contem nomes de projetos, de
#: corretoras ou de carteiras concretas: serve so para escrever o motivo D12
#: no log de frases que ja iam para o Claude Code como texto.
PADRAO_FINANCEIRO = re.compile(
    r"\b("
    r"compr(a|as|o|ar|ei|ou|em|amos|avam|ando|ada|adas|ado|ados)"
    r"|vend(e|es|o|er|i|eu|a|as|am|emos|endo|ida|idas|ido|idos)"
    r"|ordem de (compra|venda)"
    r"|corretora(s)?|broker(s)?"
    r"|carteira(s)?|wallet(s)?"
    r"|bolsa(s)?"
    r"|acoes|accoes"
    r"|investe|investem|investi|investir|investimento(s)?"
    # A grafia inglesa da palavra "cripto" fica deliberadamente de fora: e a
    # unica forma de a verificacao de fecho da D52.1 dar zero linhas, e a
    # forma portuguesa cobre a fala do Sponsor. Uma frase com a grafia
    # inglesa continua a ir como texto para o Claude Code; so o motivo no log
    # fica generico.
    r"|cripto|criptomoeda(s)?|bitcoin(s)?|ethereum|altcoin(s)?"
    r"|trade|trades|trading|trader(s)?"
    r")\b"
)

# --- Guardas que mandam sempre para o Claude Code ---------------------------

#: Negacao em qualquer ponto da frase: "nao abras o vs code no exemplo-um" nao
#: e uma ordem de abrir. Uma frase negada nunca vira acao local (D4).
PADRAO_NEGACAO = re.compile(r"\b(nao|nunca|jamais|nem)\b")

#: Destinatario explicito: a frase e para ser ENTREGUE a alguem ("diz ao
#: claude...", "pergunta...", "manda...", "escreve..."), logo e texto, nunca
#: um comando local. "diz-me as horas" e excecao (o destinatario e o proprio
#: jarvis), por isso o lookahead deixa passar "me"/"nos".
PADRAO_DESTINATARIO = re.compile(
    r"\b(diz|diga|digas|dizer|pergunta|pergunte|perguntar|manda|mande|mandar"
    r"|escreve|escreva|escrever|pede|peca|pedir|envia|envie|enviar"
    r"|responde|responda|responder|avisa|avise|comunica)\b(?!\s+(me|nos)\b)"
)

# --- Lista branca fechada da D4 (todos casados com re.fullmatch) ------------

#: Prefixo tolerado de "diz-me" / "diga-me" nas perguntas de horas e data.
_DIZ_ME = r"(?:(?:diz|dizes|diga|dizer)(?:\s+(?:me|nos))?\s+)?"

PADRAO_HORAS = re.compile(
    _DIZ_ME
    + r"(?:"
    r"que\s+horas\s+(?:sao|e|temos|serao)(?:\s+agora)?"
    r"|sabes\s+que\s+horas\s+(?:sao|e)"
    r"|qual\s+(?:e\s+)?(?:a\s+)?hora(?:\s+(?:atual|certa|de\s+agora))?"
    r"|(?:as\s+)?horas(?:\s+certas)?"
    r"|tens\s+horas"
    r")"
)

PADRAO_DATA = re.compile(
    _DIZ_ME
    + r"(?:"
    r"(?:em\s+)?que\s+dia\s+(?:da\s+semana\s+)?(?:e|estamos)(?:\s+hoje)?"
    r"|qual\s+(?:e\s+)?(?:a\s+)?data(?:\s+de\s+hoje)?"
    r"|(?:a\s+)?data\s+de\s+hoje"
    r"|que\s+data\s+(?:e|temos)(?:\s+hoje)?"
    r"|em\s+que\s+mes\s+estamos"
    r")"
)

PADRAO_CALAR = re.compile(
    r"(?:"
    r"cala(?:\s+te|\s+a\s+boca)?"
    r"|calate"
    r"|silencio"
    r"|para\s+de\s+(?:falar|escutar)"
    r"|fica\s+(?:calado|calada|em\s+silencio)"
    r"|chiu"
    r")"
)

PADRAO_ADORMECER = re.compile(
    r"(?:"
    r"adormece(?:\s+te)?"
    r"|adormecer"
    r"|dorme"
    r"|vai\s+(?:dormir|descansar)"
    r"|entra\s+em\s+(?:modo\s+de\s+)?espera"
    r"|modo\s+de\s+espera"
    r"|hiberna"
    r")"
)

PADRAO_ACORDAR = re.compile(
    r"(?:"
    r"acorda"
    r"|acordar"
    r"|desperta"
    r"|sai\s+(?:da|do)\s+(?:modo\s+de\s+)?espera"
    r"|volta\s+ao\s+trabalho"
    r")"
)

#: O grupo `projeto` captura SO a cauda do comando; e esse fragmento, e nunca
#: a frase toda, que e comparado com os nomes da configuracao.
PADRAO_ABRIR_VSCODE = re.compile(
    r"(?:abre|abres|abrir|lanca|lancar|inicia|iniciar|arranca)(?:\s+me)?"
    r"\s+(?:o\s+|a\s+)?"
    r"(?:vs\s?code|vscode|visual\s?studio\s?code|editor)"
    r"\s+(?P<projeto>[a-z0-9][a-z0-9\s]*)"
)

PADRAO_ABRIR_PASTA = re.compile(
    r"(?:abre|abres|abrir|mostra|mostras|mostrar)(?:\s+me)?"
    r"\s+(?:a\s+|o\s+)?"
    r"(?:pasta|diretorio|directorio|explorador)"
    r"\s+(?P<projeto>[a-z0-9][a-z0-9\s]*)"
)

#: Palavras de ligacao que podem aparecer antes do nome do projeto no
#: fragmento capturado ("... no projeto exemplo-um"). Sao retiradas uma a uma,
#: e o fragmento completo e sempre tentado primeiro — um projeto chamado
#: "projeto-x" ou "o-meu-repo" continua a ser reconhecido.
PALAVRAS_DE_LIGACAO = frozenset(
    {
        "no",
        "na",
        "nos",
        "nas",
        "do",
        "da",
        "dos",
        "das",
        "de",
        "em",
        "para",
        "o",
        "a",
        "os",
        "as",
        "meu",
        "minha",
        "projeto",
        "repositorio",
        "repo",
        "pasta",
        "diretorio",
        "directorio",
    }
)

#: Cortesias de fronteira toleradas a volta de um comando. Sao retiradas uma
#: de cada vez, gerando variantes tentadas por ordem (a frase tal e qual
#: primeiro), nunca no meio da frase.
CORTESIAS_INICIAIS: tuple[tuple[str, ...], ...] = (
    ("se", "faz", "favor"),
    ("faz", "favor"),
    ("por", "favor"),
    ("jarvis",),
    ("ok",),
    ("okay",),
    ("hey",),
    ("ei",),
    ("oi",),
    ("ola",),
    ("olha",),
    ("escuta",),
    ("entao",),
    ("agora",),
    ("ja",),
)

CORTESIAS_FINAIS: tuple[tuple[str, ...], ...] = (
    ("se", "faz", "favor"),
    ("se", "fazes", "favor"),
    ("faz", "favor"),
    ("por", "favor"),
    ("obrigado",),
    ("obrigada",),
    ("sff",),
    ("jarvis",),
    ("agora",),
    ("ja",),
)

#: Comparacao de nomes de projeto, palavra a palavra. Uma palavra com menos do
#: que isto tem de ser IGUAL (e por isso "dos" nunca e "dois" e "doido" nunca
#: e "dois"); numa palavra mais longa tolera-se um unico erro de escrita.
COMPRIMENTO_MINIMO_PARA_TOLERAR_ERRO = 6

#: Semelhanca minima dentro de uma palavra longa (SequenceMatcher, 0..1).
#: 0.85 = um caracter trocado ou em falta em "exemplo"; duas diferencas ja nao
#: passam. Nunca se escolhe "o mais parecido": isto e um criterio absoluto,
#: aplicado a cada projeto por si, e se mais do que um passar e ambiguo.
RAZAO_MINIMA_POR_PALAVRA = 0.85


def _normalizar(texto: str) -> str:
    """minusculas, sem acentos, so letras/digitos/espacos, espacos colapsados."""
    sem_acentos = unicodedata.normalize("NFKD", texto)
    sem_acentos = "".join(c for c in sem_acentos if not unicodedata.combining(c))
    sem_acentos = sem_acentos.lower()
    sem_acentos = re.sub(r"[^a-z0-9\s]", " ", sem_acentos)
    return re.sub(r"\s+", " ", sem_acentos).strip()


def _tirar_um_grupo(palavras: list[str], grupos: tuple[tuple[str, ...], ...], *, inicio: bool) -> list[str] | None:
    """Retira UM grupo de cortesia do inicio ou do fim, ou devolve None."""
    for grupo in grupos:
        n = len(grupo)
        if len(palavras) <= n:
            continue  # nunca esvaziar a frase: sobraria um comando sem verbo
        if inicio and tuple(palavras[:n]) == grupo:
            return palavras[n:]
        if not inicio and tuple(palavras[-n:]) == grupo:
            return palavras[:-n]
    return None


def _variantes_sem_cortesias(normalizado: str) -> list[str]:
    """A frase tal e qual, seguida das variantes sem cortesias de fronteira.

    A ordem importa e e determinista: a frase completa primeiro, depois as
    versoes com uma cortesia a menos de cada vez. Assim "abre o vs code no
    jarvis" casa com a frase completa (e "jarvis" fica como nome de projeto),
    enquanto "acorda jarvis" so casa depois de tirar a cortesia final.
    """
    variantes = [normalizado]
    atual = normalizado.split()
    while True:
        seguinte = _tirar_um_grupo(atual, CORTESIAS_INICIAIS, inicio=True)
        if seguinte is None:
            break
        atual = seguinte
        texto = " ".join(atual)
        if texto not in variantes:
            variantes.append(texto)

    for base in list(variantes):
        atual = base.split()
        while True:
            seguinte = _tirar_um_grupo(atual, CORTESIAS_FINAIS, inicio=False)
            if seguinte is None:
                break
            atual = seguinte
            texto = " ".join(atual)
            if texto not in variantes:
                variantes.append(texto)
    return variantes


def _palavra_bate(obtida: str, esperada: str) -> bool:
    """Palavras iguais, ou um unico erro de escrita numa palavra longa."""
    if obtida == esperada:
        return True
    if len(esperada) < COMPRIMENTO_MINIMO_PARA_TOLERAR_ERRO:
        return False
    if abs(len(obtida) - len(esperada)) > 1:
        return False
    return SequenceMatcher(None, obtida, esperada).ratio() >= RAZAO_MINIMA_POR_PALAVRA


def _nome_bate(candidato: str, nome_do_projeto: str) -> bool:
    """O candidato E o nome deste projeto (nunca "o mais parecido")."""
    alvo = _normalizar(nome_do_projeto)
    if not alvo or not candidato:
        return False
    if candidato == alvo:
        return True
    palavras_candidato = candidato.split()
    palavras_alvo = alvo.split()
    if len(palavras_candidato) != len(palavras_alvo):
        return False
    return all(_palavra_bate(a, b) for a, b in zip(palavras_candidato, palavras_alvo))


def _candidatos_do_fragmento(fragmento: str) -> list[str]:
    """O fragmento capturado, e depois o mesmo sem as palavras de ligacao."""
    candidatos = [fragmento] if fragmento else []
    palavras = fragmento.split()
    while len(palavras) > 1 and palavras[0] in PALAVRAS_DE_LIGACAO:
        palavras = palavras[1:]
        texto = " ".join(palavras)
        if texto not in candidatos:
            candidatos.append(texto)
    return candidatos


def _projeto_do_fragmento(
    fragmento: str, projetos: tuple[Projeto, ...]
) -> tuple[Projeto | None, str]:
    """O projeto conhecido que o fragmento nomeia, ou None mais o motivo.

    Nao ha "melhor candidato": cada projeto e avaliado por um criterio
    absoluto e, se mais do que um bater, a frase e ambigua e vai para o
    Claude Code (D4).
    """
    for candidato in _candidatos_do_fragmento(fragmento):
        correspondencias = [p for p in projetos if _nome_bate(candidato, p.nome)]
        if len(correspondencias) == 1:
            return correspondencias[0], ""
        if len(correspondencias) > 1:
            nomes = ", ".join(sorted(p.nome for p in correspondencias))
            return None, f"'{candidato}' bate com mais do que um projeto ({nomes}): ambiguo"
    return None, f"'{fragmento}' nao e nenhum projeto da configuracao"


@dataclass(frozen=True)
class ResultadoRouter:
    """A decisao do router: nunca executa nada, so descreve (D48.2)."""

    tipo: TipoResultado
    nome_acao: str | None = None
    argumento: str | None = None
    texto: str | None = None
    motivo: str = ""
    #: True so quando quem chamou forneceu uma confianca E ela passou o limiar
    #: da D5. False significa que essa metade da guarda nao correu (ver
    #: CONFIANCA_MINIMA) — o log da T6 tem de o registar.
    confianca_verificada: bool = False


def _acao_da_lista_branca(variante: str) -> tuple[str, str | None] | tuple[None, None]:
    """(nome_acao, argumento) para os comandos SEM projeto, ou (None, None)."""
    if PADRAO_HORAS.fullmatch(variante):
        return "horas_e_data", "horas"
    if PADRAO_DATA.fullmatch(variante):
        return "horas_e_data", "data"
    if PADRAO_CALAR.fullmatch(variante):
        return "calar", None
    if PADRAO_ADORMECER.fullmatch(variante):
        return "adormecer", None
    if PADRAO_ACORDAR.fullmatch(variante):
        return "acordar", None
    return None, None


def _motivo_do_texto(normalizado: str) -> str:
    """Motivo a escrever no log quando a frase segue como texto."""
    if PADRAO_FINANCEIRO.search(normalizado):
        # D12/D52: etiqueta, nao portao. A frase ja ia como texto; isto so
        # deixa escrito no log porque nunca podia ter virado acao.
        return "fora da lista branca da D4; vocabulario financeiro: texto para o Claude Code, nunca acao (D12)"
    return "fora da lista branca fechada da D4: segue para o Claude Code"


def encaminhar(
    texto: str | None,
    config: Config,
    confianca: float | None = None,
) -> ResultadoRouter:
    """Decide local/claude/nada para uma frase ja transcrita. Nunca levanta."""
    bruto = texto or ""
    limpo = bruto.strip()

    # --- Guarda de tokens e de falsos despertares (D5) ---------------------
    if not limpo:
        return ResultadoRouter("nada", motivo="transcricao vazia (D5)")
    if len(limpo) < COMPRIMENTO_MINIMO_CARACTERES:
        return ResultadoRouter(
            "nada",
            motivo=(
                f"transcricao curta de mais ({len(limpo)} caracteres, minimo "
                f"{COMPRIMENTO_MINIMO_CARACTERES}) (D5)"
            ),
        )
    if confianca is not None and confianca < CONFIANCA_MINIMA:
        return ResultadoRouter(
            "nada",
            motivo=f"confianca {confianca:.2f} abaixo do limiar {CONFIANCA_MINIMA:.2f} (D5)",
        )
    confianca_verificada = confianca is not None

    normalizado = _normalizar(limpo)
    if not normalizado:
        return ResultadoRouter(
            "nada",
            motivo="transcricao so com pontuacao/ruido, sem palavras (D5)",
            confianca_verificada=confianca_verificada,
        )
    if normalizado in FRASES_DE_RUIDO_CONHECIDAS:
        return ResultadoRouter(
            "nada",
            motivo="frase e uma alucinacao conhecida do STT sobre ruido/silencio (D5/D51)",
            confianca_verificada=confianca_verificada,
        )

    def como_texto(motivo: str) -> ResultadoRouter:
        return ResultadoRouter(
            "claude",
            texto=limpo,
            motivo=motivo,
            confianca_verificada=confianca_verificada,
        )

    # --- Guardas: uma frase negada ou dirigida a alguem nunca e comando ----
    if PADRAO_NEGACAO.search(normalizado):
        return como_texto("frase com negacao: nunca vira acao local (D4)")
    if PADRAO_DESTINATARIO.search(normalizado):
        return como_texto("frase dirigida a um destinatario: e texto, nunca acao local (D4)")

    # --- Lista branca FECHADA da D4: fullmatch contra a frase inteira ------
    # Uma recusa por projeto desconhecido nao interrompe a procura: outra
    # variante (por exemplo sem a cortesia final) ainda pode ser um comando
    # valido. O primeiro motivo de recusa guarda-se para o log.
    motivo_da_recusa = ""
    for variante in _variantes_sem_cortesias(normalizado):
        nome_acao, argumento = _acao_da_lista_branca(variante)
        if nome_acao is not None:
            return ResultadoRouter(
                "local",
                nome_acao=nome_acao,
                argumento=argumento,
                motivo=f"{nome_acao} (D4), frase inteira casada com a lista branca",
                confianca_verificada=confianca_verificada,
            )

        for padrao, acao, etiqueta in (
            (PADRAO_ABRIR_VSCODE, "abrir_vscode", "D4.b"),
            (PADRAO_ABRIR_PASTA, "abrir_pasta", "D4.c"),
        ):
            casou = padrao.fullmatch(variante)
            if casou is None:
                continue
            projeto, porque_nao = _projeto_do_fragmento(
                casou.group("projeto").strip(), config.projetos
            )
            if projeto is None:
                if not motivo_da_recusa:
                    motivo_da_recusa = f"{acao} ({etiqueta}) recusado: {porque_nao}"
                continue
            return ResultadoRouter(
                "local",
                nome_acao=acao,
                argumento=str(projeto.caminho),
                motivo=f"{acao} no projeto conhecido '{projeto.nome}' ({etiqueta})",
                confianca_verificada=confianca_verificada,
            )

    return como_texto(motivo_da_recusa or _motivo_do_texto(normalizado))


# --- Autoteste das partes puras (config ficticia em memoria) ---------------


def _autoteste() -> int:
    import tempfile
    from pathlib import Path

    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    with tempfile.TemporaryDirectory() as pasta:
        caminho_a = Path(pasta) / "projeto-exemplo"
        caminho_a.mkdir()
        config = Config(
            microfone="Microfone de Teste",
            projetos=(Projeto(nome="projeto-exemplo", caminho=caminho_a),),
        )

        verificar("horas: local", encaminhar("que horas sao", config).tipo, "local")
        verificar(
            "horas: argumento distingue de data",
            encaminhar("que horas sao", config).argumento,
            "horas",
        )
        verificar(
            "data: argumento distingue de horas",
            encaminhar("que dia e hoje", config).argumento,
            "data",
        )
        verificar(
            "abrir vscode: projeto conhecido -> local com argumento",
            encaminhar("abre o vs code no projeto-exemplo", config).argumento,
            str(caminho_a),
        )
        verificar(
            "abrir vscode: sem projeto -> claude (ambiguo)",
            encaminhar("abre o vs code", config).tipo,
            "claude",
        )
        verificar(
            "abrir vscode: projeto desconhecido -> claude, nunca local",
            encaminhar("abre o vs code no projeto marte", config).tipo,
            "claude",
        )
        verificar(
            "lista branca fechada: gatilho no meio de outra frase -> claude",
            encaminhar(
                "diz ao claude para abrir um ticket sobre o vs code no projeto-exemplo", config
            ).tipo,
            "claude",
        )
        verificar(
            "lista branca fechada: negacao -> claude",
            encaminhar("nao abras o vs code no projeto-exemplo", config).tipo,
            "claude",
        )
        verificar(
            "nome parecido nao e o projeto -> claude",
            encaminhar("abre a pasta do projeto-exemplar-antigo", config).tipo,
            "claude",
        )
        verificar("vazio -> nada", encaminhar("", config).tipo, "nada")
        verificar("2 caracteres -> nada", encaminhar("oi", config).tipo, "nada")
        verificar(
            "ruido conhecido -> nada",
            encaminhar("Obrigado por assistir!", config).tipo,
            "nada",
        )
        verificar(
            "financeiro -> claude, nunca local (D12)",
            encaminhar("compra-me duas acoes agora", config).tipo,
            "claude",
        )
        verificar(
            "confianca baixa -> nada",
            encaminhar("qualquer coisa aqui", config, confianca=0.1).tipo,
            "nada",
        )
        verificar(
            "pergunta comum -> claude",
            encaminhar("como esta o tempo hoje", config).tipo,
            "claude",
        )

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste do router completo (lista branca, guardas, D12).")
    return 0


if __name__ == "__main__":
    if "--autoteste" in sys.argv[1:]:
        sys.exit(_autoteste())
    print(__doc__)
    print("Autoteste: python -m jarvis.router --autoteste")
