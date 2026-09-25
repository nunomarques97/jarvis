r"""Encaminhador deterministico do jarvis: lista branca FECHADA da D4.

Decide, para uma frase ja transcrita, uma de tres saidas — nunca mais que
isso, e nunca com um LLM no meio (o Ollama nao entra neste run):

  "local"  - a frase INTEIRA e um comando da lista branca fechada da D4 E,
             quando o comando precisa de um projeto, esse projeto esta na
             configuracao. Devolve so a DESCRICAO da acao (`nome_acao` mais
             `argumento`), nunca a executa (quem executa e
             `jarvis.acoes_locais`; um
             caminho ou nome lido da configuracao e entrada externa e nunca
             entra numa linha de comandos aqui).
  "claude" - tudo o resto: perguntas, conversa, uma frase que apenas CONTEM
             um gatilho da lista branca, um comando sem projeto identificavel
             (ambiguo), um projeto que nao esta na configuracao, e qualquer
             frase de dominio financeiro (segue como TEXTO, nunca como
             acao; ver "D12" mais abaixo).
  "nada"   - guarda de tokens e de falsos despertares: transcricao
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

LIMPEZA DO RESIDUO DA PALAVRA DE ATIVACAO (causa-raiz do defeito 1):
o microfone continua aberto depois da deteccao da wake word, por isso o audio
dela entra muitas vezes na janela transcrita e cola-se ao INICIO da frase
("jarvis, que horas sao" em vez de "que horas sao"). Antes de qualquer outra
coisa, `encaminhar()` tira do inicio da frase normalizada um residuo dessa
palavra, a partir da lista FECHADA e pequena `RESIDUOS_PALAVRA_DE_ATIVACAO`
(nunca aproximacao/fuzzy). So o
INICIO: nunca toca no meio nem no fim da frase, e nunca esvazia a frase (um
residuo que seria a frase toda fica tal e qual, porque nesse caso a guarda da
D5 ja trata o caso como "nada", nunca como comando). O que foi removido fica
em `ResultadoRouter.residuo_removido`, para o log da etapa 3 (jarvis/app.py)
poder explicar ao utilizador, numa frase, porque e que "jarvis, que horas sao"
virou a mesma accao que "que horas sao". PROIBIDO o dicionario de enganos: uma
transcricao que nao e um residuo conhecido da wake word colado a um comando
da lista branca NUNCA vira essa accao por adivinhacao — "jorvis, que oracao"
fica fora da lista branca e segue como texto para o Claude Code, exactamente
como antes desta limpeza (uma accao local por adivinhacao e inaceitavel).

COMO A LISTA BRANCA E FECHADA:
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

COMO O PROJETO E RECONHECIDO: o padrao captura o
fragmento que vem depois do objeto do comando, e SO esse fragmento e
comparado com os nomes da configuracao — nunca a frase toda, nunca por
substring. A comparacao e palavra a palavra: mesmo numero de palavras e cada
palavra igual, tolerando um unico erro de escrita dentro de palavras longas
(>= 6 letras). Uma palavra diferente ("exemplo dos", "exemplo doido") ou uma
palavra a mais ("exemplo dois privado") nao e o projeto: vai para o Claude
Code. Se mais do que um projeto conhecido bater, e ambiguo e vai tambem para
o Claude Code. O router nunca escolhe "o mais parecido".

D12 (proibicao permanente), como a D52 a fixou: a garantia e a lista branca,
nao um regex de nomes. Nenhuma das cinco acoes da D4 consegue negociar,
ligar-se a uma corretora ou ler uma carteira, e tudo o que nao e lista branca
ja vai como texto. O `PADRAO_FINANCEIRO` corre DEPOIS da lista branca e serve
so para carimbar o motivo D12 no log dessas frases. Nao existe nenhuma lista
de nomes proibidos, em codigo ou em configuracao. "Abre o VS Code no
<projeto>" e "abre a pasta do <projeto>" continuam a ser acao local para
QUALQUER projeto da configuracao privada.

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
#: sequer considerada. Mais curto do que isto e "sem fala util", nao
#: ambiguidade — cai na guarda "nada", nunca no Claude Code.
COMPRIMENTO_MINIMO_CARACTERES = 4

#: Confianca minima em [0, 1]. A guarda esta LIGADA por omissao: sempre que a
#: chamada fornece uma confianca, ela e comparada com este limiar. O que nao
#: se pode inventar e um valor quando a chamada nao mede nenhum — nesse caso
#: `ResultadoRouter.confianca_verificada` vem False e o log tem de o dizer.
#: NOTA PARA A CADEIA VIVA (jarvis/app.py): passar SEMPRE a confianca do transcritor a
#: encaminhar(); um resultado com confianca_verificada=False significa que a
#: metade "confianca" da guarda da D5 nao correu naquela frase.
CONFIANCA_MINIMA = 0.35

#: Lista FECHADA e pequena das variantes escritas do residuo da palavra de
#: ativacao, depois de `_normalizar()` (minusculas, sem acentos, sem
#: pontuacao). So o que esta aqui e o que a medicao das 20 frases mostrou que
#: aparece de facto conta como residuo — nada de aproximacao/fuzzy.
#: Ordem: variantes de duas palavras primeiro, para que "hey jarvis" nao pare
#: a meio em "hey" sozinho; "jorvis" e a grafia que o log real mostrou (linha
#: 647 de logs/jarvis-2026-09-20.log, "Jorvis, que oração!"). "boas jarvis"
#: e a palavra de ativacao portuguesa; o ouvido residente ja a tira antes (so
#: as palavras exatas), e aqui fica para qualquer outro caminho ate ao router.
RESIDUOS_PALAVRA_DE_ATIVACAO: tuple[str, ...] = (
    "hey jarvis",
    "boas jarvis",
    "hei jarvis",
    "ei jarvis",
    "jorvis",
    "jarvis",
)

#: Correspondencias EXATAS (depois de normalizar) que o Whisper produz
#: tipicamente sobre silencio/ruido de fundo em vez de nao dizer nada — a D51
#: documenta "Obrigado por assistir!" sobre um WAV de ruido branco isolado.
#: Uma frase real do utilizador que por acaso contenha estas palavras no meio de
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
    # forma portuguesa cobre a fala do utilizador. Uma frase com a grafia
    # inglesa continua a ir como texto para o Claude Code; so o motivo no log
    # fica generico.
    r"|cripto|criptomoeda(s)?|bitcoin(s)?|ethereum|altcoin(s)?"
    r"|trade|trades|trading|trader(s)?"
    r")\b"
)

# --- Guardas que mandam sempre para o Claude Code ---------------------------

#: Negacao em qualquer ponto da frase: "nao abras o vs code no exemplo-um" nao
#: e uma ordem de abrir. Uma frase negada nunca vira acao local. D58a:
#: as MESMAS regras valem em ingles — "do not"/"never"/"not" (a forma
#: contraida "don't" chega aqui ja normalizada para "don t" por `_normalizar`,
#: por isso as formas contraidas entram com o espaco literal).
PADRAO_NEGACAO = re.compile(
    r"\b(nao|nunca|jamais|nem"
    r"|not|never"
    r"|don\s+t|do\s+not|doesn\s+t|didn\s+t|won\s+t|can\s+t|isn\s+t"
    r")\b"
)

#: Destinatario explicito: a frase e para ser ENTREGUE a alguem ("diz ao
#: claude...", "pergunta...", "manda...", "escreve...", "tell claude...",
#: "ask claude..."), logo e texto, nunca um comando local. "diz-me as horas" /
#: "tell me the time" sao excecao (o destinatario e o proprio jarvis), por
#: isso o lookahead deixa passar "me"/"nos"/"us".
PADRAO_DESTINATARIO = re.compile(
    r"\b(diz|diga|digas|dizer|pergunta|pergunte|perguntar|manda|mande|mandar"
    r"|escreve|escreva|escrever|pede|peca|pedir|envia|envie|enviar"
    r"|responde|responda|responder|avisa|avise|comunica"
    r"|tell|ask|send|write|message)\b(?!\s+(me|nos|us)\b)"
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

# --- Lista branca fechada da D4, formulacoes inglesas ----------------
#
# MESMAS CINCO ACOES, traduzidas — zero acoes novas. Nenhum verbo de
# compra/venda entra aqui (nem "buy", nem "sell", nem "order",
# nem "trade", nem "broker", nem "wallet") e nenhum nome real de projeto:
# o grupo `projeto` continua a capturar so a cauda do comando, tal
# e qual a versao portuguesa, comparada com os mesmos nomes da configuracao.
#
# Prefixo tolerado de "tell me" nas perguntas de horas e data — o equivalente
# ingles de `_DIZ_ME`. "tell me the time" NAO e dirigido ao Claude Code porque
# o destinatario e o proprio jarvis (a guarda `PADRAO_DESTINATARIO` ja deixa
# passar "tell" seguido de "me"/"us", ver acima).
_TELL_ME_EN = r"(?:tell\s+me\s+)?"

PADRAO_HORAS_EN = re.compile(
    _TELL_ME_EN
    + r"(?:"
    r"what\s+time\s+is\s+it(?:\s+now)?"
    r"|what\s+s\s+the\s+time"
    r"|do\s+you\s+know\s+what\s+time\s+it\s+is"
    r"|the\s+time"
    r")"
)

PADRAO_DATA_EN = re.compile(
    _TELL_ME_EN
    + r"(?:"
    r"what\s+day\s+is\s+it(?:\s+today)?"
    r"|what\s+s\s+today\s+s\s+date"
    r"|what\s+s\s+the\s+date(?:\s+today)?"
    r"|the\s+date"
    r")"
)

PADRAO_CALAR_EN = re.compile(
    r"(?:"
    r"be\s+quiet"
    r"|stay\s+quiet"
    r"|quiet"
    r"|silence"
    r"|stop\s+talking"
    r"|stop\s+listening"
    r"|shush"
    r")"
)

PADRAO_ADORMECER_EN = re.compile(
    r"(?:"
    r"go\s+to\s+sleep"
    r"|go\s+to\s+rest"
    r"|sleep"
    r"|enter\s+standby(?:\s+mode)?"
    r"|standby\s+mode"
    r"|hibernate"
    r")"
)

PADRAO_ACORDAR_EN = re.compile(
    r"(?:"
    r"wake\s+up"
    r"|wake"
    r"|exit\s+standby(?:\s+mode)?"
    r"|back\s+to\s+work"
    r")"
)

#: Mesma logica do `PADRAO_ABRIR_VSCODE` portugues: o grupo `projeto` captura
#: so a cauda do comando. "run"/"launch"/"start" sao o equivalente ingles das
#: sinonimas portuguesas "arranca"/"lanca"/"inicia" — mesma accao, mais
#: nenhuma.
PADRAO_ABRIR_VSCODE_EN = re.compile(
    r"(?:open|launch|start|run)"
    r"\s+(?:up\s+)?"
    r"(?:vs\s?code|vscode|visual\s?studio\s?code|the\s+editor)"
    r"\s+(?:in\s+|for\s+)?(?:the\s+|project\s+)?"
    r"(?P<projeto>[a-z0-9][a-z0-9\s]*)"
)

#: Ao contrario do portugues ("abre a pasta DO <projeto>"), a ordem natural em
#: ingles poe o projeto ANTES da palavra "folder" ("open the <projeto>
#: folder"): o grupo `projeto` e o mesmo fragmento de sempre, so a posicao no
#: padrao muda. O quantificador preguicoso (`*?`) e o que permite ao grupo
#: parar mesmo antes de " folder"/" directory" em vez de os engolir.
PADRAO_ABRIR_PASTA_EN = re.compile(
    r"(?:open|show)"
    r"\s+(?:up\s+)?"
    r"(?:the\s+)?"
    r"(?P<projeto>[a-z0-9][a-z0-9\s]*?)"
    r"\s+(?:folder|directory)"
)

#: Tabelas das accoes SEM projeto (horas/data/calar/adormecer/acordar), uma
#: por lingua — usadas por `_acoes_bare_que_batem` para implementar a regra
#: das duas listas da D58(b) sem depender de nenhuma deteccao de lingua: a
#: MESMA frase normalizada e casada contra as duas tabelas, nunca so uma.
_ACOES_BARE_PT: tuple[tuple[re.Pattern[str], str, str | None], ...] = (
    (PADRAO_HORAS, "horas_e_data", "horas"),
    (PADRAO_DATA, "horas_e_data", "data"),
    (PADRAO_CALAR, "calar", None),
    (PADRAO_ADORMECER, "adormecer", None),
    (PADRAO_ACORDAR, "acordar", None),
)

_ACOES_BARE_EN: tuple[tuple[re.Pattern[str], str, str | None], ...] = (
    (PADRAO_HORAS_EN, "horas_e_data", "horas"),
    (PADRAO_DATA_EN, "horas_e_data", "data"),
    (PADRAO_CALAR_EN, "calar", None),
    (PADRAO_ADORMECER_EN, "adormecer", None),
    (PADRAO_ACORDAR_EN, "acordar", None),
)

#: As accoes COM projeto (abrir vscode / abrir pasta), PT e EN juntas: cada
#: par (padrao, nome_acao) e tentado por ordem contra cada variante; como as
#: duas linguas exigem verbos/estrutura diferentes no INICIO e/ou no FIM da
#: frase inteira (fullmatch), a mesma variante nunca pode casar por acidente
#: com o padrao das duas linguas ao mesmo tempo — nao ha colisao possivel
#: para verificar aqui (ao contrario das accoes sem projeto, ver acima).
_ACOES_COM_PROJETO: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (PADRAO_ABRIR_VSCODE, "abrir_vscode", "D4.b"),
    (PADRAO_ABRIR_VSCODE_EN, "abrir_vscode", "D58a"),
    (PADRAO_ABRIR_PASTA, "abrir_pasta", "D4.c"),
    (PADRAO_ABRIR_PASTA_EN, "abrir_pasta", "D58a"),
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
#: primeiro), nunca no meio da frase. D58a: as mesmas variantes inglesas
#: ("please", "hi", "hello", "thanks"/"thank you") tambem sao toleradas.
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
    ("please",),
    ("hi",),
    ("hello",),
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
    ("please",),
    ("thanks",),
    ("thank", "you"),
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


def _remover_residuo_wake_word(normalizado: str) -> tuple[str, str | None]:
    """Tira do INICIO da frase normalizada um residuo conhecido da wake word.

    So a lista FECHADA `RESIDUOS_PALAVRA_DE_ATIVACAO` conta: nada de
    aproximacao/fuzzy. Remove no maximo um residuo,
    uma unica vez — a wake word so acontece uma vez por frase. Nunca esvazia
    a frase: se o residuo fosse a frase toda, nao se remove nada (a guarda de
    tokens da D5, mais acima em `encaminhar()`, ja trata isso como "nada").
    Devolve (frase_sem_residuo, o_que_foi_removido_ou_None).
    """
    palavras = normalizado.split()
    for residuo in RESIDUOS_PALAVRA_DE_ATIVACAO:
        palavras_do_residuo = residuo.split()
        n = len(palavras_do_residuo)
        if len(palavras) <= n:
            continue  # sobraria frase vazia: nunca se remove
        if palavras[:n] == palavras_do_residuo:
            return " ".join(palavras[n:]), residuo
    return normalizado, None


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
    Claude Code.
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
    """A decisao do router: nunca executa nada, so descreve."""

    tipo: TipoResultado
    nome_acao: str | None = None
    argumento: str | None = None
    texto: str | None = None
    motivo: str = ""
    #: True so quando quem chamou forneceu uma confianca E ela passou o limiar
    #: da D5. False significa que essa metade da guarda nao correu (ver
    #: CONFIANCA_MINIMA) — o log da cadeia viva tem de o registar.
    confianca_verificada: bool = False
    #: O residuo da palavra de ativacao removido do INICIO da frase, tal
    #: e qual esta em `RESIDUOS_PALAVRA_DE_ATIVACAO` (ex.: "hey jarvis",
    #: "jorvis"), ou None quando nenhum residuo conhecido foi encontrado. Fica
    #: aqui para o log da etapa 3 (jarvis/app.py) poder explicar ao utilizador,
    #: numa frase, o que foi tirado antes do encaminhamento.
    residuo_removido: str | None = None


def _acoes_que_batem(
    variante: str, tabela: tuple[tuple[re.Pattern[str], str, str | None], ...]
) -> list[tuple[str, str | None]]:
    """(nome_acao, argumento) de cada padrao da tabela que faz fullmatch.

    Funcao pura, sem estado: usada tanto pela tabela portuguesa como pela
    inglesa (e por testes que a chamam com tabelas propositadamente
    construidas para verificar o mecanismo — nao ha frase real que colida
    entre as duas listas de producao, ver `_acoes_bare_que_batem`).
    """
    return [
        (nome_acao, argumento)
        for padrao, nome_acao, argumento in tabela
        if padrao.fullmatch(variante)
    ]


def _acoes_bare_que_batem(variante: str) -> list[tuple[str, str | None]]:
    """(nome_acao, argumento) distintos que a variante bate nas DUAS listas.

    D58(b), regra das duas listas, sem depender de nenhuma deteccao de
    lingua: a frase e casada contra `_ACOES_BARE_PT` e `_ACOES_BARE_EN`. O
    chamador (`encaminhar()`) decide: zero resultados -> tenta outra coisa;
    um resultado distinto -> essa accao executa-se (batesse so numa lista ou
    nas duas, para a MESMA accao, tanto faz); mais do que um resultado
    distinto -> ambiguo, vai para o Claude Code como texto (nunca se escolhe
    a mais provavel).
    """
    encontradas: list[tuple[str, str | None]] = []
    for tabela in (_ACOES_BARE_PT, _ACOES_BARE_EN):
        for par in _acoes_que_batem(variante, tabela):
            if par not in encontradas:
                encontradas.append(par)
    return encontradas


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

    # --- Guarda de tokens e de falsos despertares ---------------------
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

    # --- Limpeza do residuo da palavra de ativacao, ANTES do
    # encaminhamento: so a lista FECHADA RESIDUOS_PALAVRA_DE_ATIVACAO, so no
    # INICIO, nunca no meio nem no fim, nunca esvazia a frase (ver a guarda
    # dentro de `_remover_residuo_wake_word`). O que foi removido viaja em
    # `residuo_removido` ate ao resultado, para o log da etapa 3 o explicar.
    normalizado, residuo_removido = _remover_residuo_wake_word(normalizado)

    def como_texto(motivo: str) -> ResultadoRouter:
        return ResultadoRouter(
            "claude",
            texto=limpo,
            motivo=motivo,
            confianca_verificada=confianca_verificada,
            residuo_removido=residuo_removido,
        )

    # --- Guardas: uma frase negada ou dirigida a alguem nunca e comando ----
    if PADRAO_NEGACAO.search(normalizado):
        return como_texto("frase com negacao: nunca vira acao local (D4)")
    if PADRAO_DESTINATARIO.search(normalizado):
        return como_texto("frase dirigida a um destinatario: e texto, nunca acao local (D4)")

    # --- Lista branca FECHADA da D4/D58a: fullmatch contra a frase inteira,
    # casada contra AS DUAS listas (PT e EN), sem nenhuma deteccao de lingua.
    # Uma recusa por projeto desconhecido nao interrompe a procura:
    # outra variante (por exemplo sem a cortesia final) ainda pode ser um
    # comando valido. O primeiro motivo de recusa guarda-se para o log.
    motivo_da_recusa = ""
    for variante in _variantes_sem_cortesias(normalizado):
        candidatos = _acoes_bare_que_batem(variante)
        if len(candidatos) == 1:
            nome_acao, argumento = candidatos[0]
            return ResultadoRouter(
                "local",
                nome_acao=nome_acao,
                argumento=argumento,
                motivo=f"{nome_acao} (D4/D58b), frase inteira casada com a lista branca",
                confianca_verificada=confianca_verificada,
                residuo_removido=residuo_removido,
            )
        if len(candidatos) > 1:
            # D58(b), ultimo caso: a variante bate em ACOES DIFERENTES das
            # duas listas — nunca se escolhe a mais provavel, vai como texto.
            resumo = ", ".join(
                f"{nome_acao}" + (f" ({argumento})" if argumento else "")
                for nome_acao, argumento in candidatos
            )
            return como_texto(
                f"frase bate em acoes diferentes das duas listas brancas ({resumo}): "
                "ambiguo, nunca se escolhe a mais provavel (D58b)"
            )

        for padrao, acao, etiqueta in _ACOES_COM_PROJETO:
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
                residuo_removido=residuo_removido,
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
