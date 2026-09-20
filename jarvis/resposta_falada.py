r"""Contrato da resposta falada (D59): o que chega a voz e o que fica so na consola.

Defeito que isto fecha (D59, T3): a linha 650 de logs/jarvis-2026-09-20.log trouxe o texto
interno de uma chamada de ferramenta do Claude Code (`<invoke name="Bash">...`) e a linha 652
mostra isso LIDO EM VOZ ALTA, porque `resumo_falado` so cortava aos 240 caracteres — um corte por
tamanho nao protege nada, so decide ONDE o texto proibido parte ao meio.

FILTRO DETERMINISTICO POR EXCLUSAO, nunca um modelo a julgar (a mesma logica da D9 para o
encaminhador): "linguagem natural" e definida pelo que ela NAO e. Uma linha (ou, a partir dela,
todo um bloco contiguo ate a proxima linha em branco) e retirada INTEIRA da voz se contiver
qualquer uma das categorias da D59.1.

A LISTA DA D59.1 E DE TOKENS, NAO DE CASOS BEM FORMADOS (foi aqui que as tentativas 1 e 2 da T3
falharam, as duas pela mesma forma): o que chega do Claude Code vem truncado, partido em linhas,
sem fecho e sem sintaxe valida — `<invok`, `{ok: 1`, `@@-1,2+3,4@@`, `>dir`, `$env:PATH`,
`|a|b`, `publico.pt`, `Traceback (most recent call last)` sem dois pontos. Por isso a exclusao
nao procura construcoes validas: procura os SINAIS que nenhuma frase falada tem.

O sinal mais forte e o mais barato e o CARACTER. Estes nunca aparecem em portugues (ou ingles)
dito em voz alta, e aparecem sempre em marcacao, codigo, caminhos, tabelas, consolas e saidas de
comandos — basta um para a linha sair:

    <  >   tags XML/HTML de qualquer forma, `</`, `/>`, redireccionamentos `2>`, prompts `>>>`
    {  }   JSON, dicionarios, f-strings, templates
    [  ]   listas JSON, indices, codigos ANSI, links markdown
    |      tabelas markdown e pipes de consola
    \      caminhos do Windows, UNC `\\servidor\partilha`, escapes
    =      atributos (`name="Bash"`), atribuicoes, `--flag=valor`
    _      identificadores em snake_case (`resumo_falado`) e enfase markdown
    &      `&&`, `2>&1`, entidades HTML (`&lt;invoke&gt;`)
    @      emails, decoradores, `@@` de diffs
    ~      `~/bin`, cercas `~~~`
    ^      acentos circunflexos de consola e regex
    tabulacao e caracteres de controlo (saida em colunas, escape `\x1b` das cores de terminal)

Tudo o resto da lista precisa de padroes, porque usa so caracteres comuns: cercas de tres crases,
pares "chave": valor, cabecalhos e corpos de diff, saidas de git, linhas de comando, prompts
`$`/`>`, variaveis de shell, caminhos com barra normal e ficheiros com extensao, URLs com e sem
esquema, tracebacks (Python e JavaScript), linhas de log e linhas so de simbolos.

Uma PALAVRA MAIOR DO QUE O LIMITE FALADO tambem sai: nunca e linguagem natural (nem caberia numa
frase falada) e era por ela que uma linha unica muito grande punha os padroes em retrocesso
quadratico — 22,5 s para 30 000 caracteres, medidos na revisao da tentativa 2, o "ficar a
espera" do inaceitavel n.4. Com o teto por palavra, nenhum padrao ve nunca um token grande e o
custo passa a linear no tamanho do texto.

Uma LINHA MAIOR DO QUE ~2000 CARACTERES sai tambem, e sai ANTES de qualquer padrao correr: uma
linha assim nunca e uma frase falada, e sem este teto uma linha feita de muitas palavras curtas
(nenhuma acima do teto por palavra) ainda levava os 28 padroes a varrer o texto todo. O teto e
por LINHA do texto de entrada, NAO pela resposta junta: a juncao das linhas sobreviventes e a
resposta inteira, e prosa natural limpa de 2500 caracteres em varios paragrafos continua a ser
falada (e so depois cortada aos 200). Confundir os dois manda prosa limpa para a frase de
recurso, que e ao mesmo tempo inventar conteudo ("codigo ou dados tecnicos" sobre prosa) e quebrar
a regra de que o recurso so entra quando nao sobra nada falavel.

NUNCA TRUNCAR PARA DENTRO DO PROIBIDO (D59.2): um pedaco proibido e removido inteiro (a linha, ou
o bloco tecnico contiguo a que pertence, ou o bloco de tres crases inteiro); a resposta so cai
para a frase de recurso quando isso esvazia tudo o que havia para dizer. Cortar aos N caracteres
nunca e protecao, so decide onde a fatia proibida parte ao meio — foi assim que o defeito
aconteceu.

O FILTRO E O ULTIMO A FALAR, NAO O PRIMEIRO (tentativa 1 da T3 falhou aqui): filtrar linha a
linha e so depois JUNTAR as linhas sobreviventes com um espaco remonta conteudo proibido a
jusante do filtro — `Esta tudo bem <invoke` + `name="Bash">` volta a dar a tag inteira da linha
652 do log. Por isso a exclusao e reaplicada ao texto JA JUNTO numa linha (antes do prefixo e do
corte) e outra vez ao texto JA CORTADO: se alguma dessas verificacoes falhar, nada daquilo vai a
voz, cai a frase de recurso. Nada chega as colunas sem ter passado o filtro na forma exata em que
vai ser lido.

LIMITES (D59.3): 200 caracteres de conteudo falado, 280 no total ja com o prefixo de origem. O
corte, quando o conteudo ainda excede o limite depois do filtro, e sempre no fim de uma frase
(`.`, `!` ou `?` seguido de espaco ou do fim do texto — o ponto de `3.14` ou de `jarvis.app` NAO
e fim de frase e nunca corta ali) ou, nao havendo, no fim de uma palavra — nunca a meio de uma
palavra.

FRASE DE RECURSO (D59.4): quando nao sobra nada falavel, o jarvis nunca fica calado. Ha tres
frases fixas, conforme o caso: a resposta chegou mesmo vazia; a resposta so tinha conteudo
tecnico (o filtro comeu tudo); ou — ja na rede de emergencia de `jarvis.app.responder`, para
respostas de qualquer origem — nao havia um unico sitio seguro onde cortar. As duas primeiras
nomeiam a origem (so elas sabem qual e); as tres dizem onde esta a resposta completa. A consola
e o log (D59.6) levam sempre o texto inteiro, em bruto.

PREFIXO DE ORIGEM (D59.5, cumpre a D48.4): diz de quem e a frase e que ela nao esta verificada —
a sessao filha do Claude Code corre sem ferramentas (D48.3) e pode alucinar que as usou.

O LADO SEGURO E CALAR O PEDACO, NAO ARRISCAR: quando uma frase natural tem um sinal destes
(`Alterei o app.py`, `Ve em publico.pt`), ela sai e fica a frase de recurso, que diz onde esta a
resposta completa. Ler marcacao em voz alta e o inaceitavel n.7 do PRODUCT-PROFILE ("parece
avariado, e parecer avariado e pior do que estar calado"); a frase de recurso garante que
calado, de facto, ele nunca fica (inaceitavel n.4).
"""

from __future__ import annotations

import re

#: D59.3 (era 240): no maximo de CONTEUDO falado por resposta do Claude Code.
MAXIMO_CARACTERES_FALADOS = 200
#: D59.3 (era 400): corte duro de qualquer resposta falada, ja com o prefixo, seja de onde for.
MAXIMO_ABSOLUTO_FALADO = 280
#: D48.4/D59.5: nomeia a origem e diz que a frase nao esta verificada. Pode encurtar-se (D59.5);
#: fica como estava porque ja cabe largamente dentro do limite (40 caracteres).
PREFIXO_DA_RESPOSTA_DO_CLAUDE = "Resposta do Claude Code, não verificada:"

#: D59.4: a resposta do Claude Code veio mesmo vazia (sem texto nenhum).
FRASE_RECURSO_SEM_TEXTO = (
    f"{PREFIXO_DA_RESPOSTA_DO_CLAUDE} respondeu sem texto para dizer; "
    "a resposta completa está na consola."
)
#: D59.4: havia texto, mas o filtro por exclusao nao deixou nada falavel (era so codigo, tags,
#: JSON, um caminho, etc.).
FRASE_RECURSO_SO_TECNICO = (
    f"{PREFIXO_DA_RESPOSTA_DO_CLAUDE} respondeu com código ou dados técnicos; "
    "a resposta completa está na consola."
)
#: D59.4, rede de emergencia de `jarvis.app.responder`: uma resposta de QUALQUER origem (nao so
#: do Claude Code) grande demais e sem um unico espaco onde cortar — uma "palavra" de centenas de
#: caracteres nunca e linguagem natural. Nao nomeia o Claude Code porque tambem serve as
#: respostas locais; diz de onde vem ("a resposta") e onde esta inteira, e nunca deixa o jarvis
#: calado (inaceitavel n.4).
FRASE_RECURSO_SEM_CORTE_SEGURO = (
    "Não tenho nada que se possa ler em voz alta; a resposta completa está na consola."
)

# --- padroes de exclusao (D59.1), categoria a categoria da lista do contrato -------------------
#
# Regra de escrita destes padroes: cada um tem de apanhar tambem a forma MALFORMADA ou PARCIAL da
# sua categoria, porque e assim que o texto chega de um modelo truncado. Nenhum padrao pode ter
# quantificadores encaixados sem guarda a esquerda: com o teto por palavra, cada tentativa custa
# no maximo o tamanho de um token, e o varrimento e linear.

#: CATEGORIA 1 (tags XML/HTML), 3 (JSON/dicionarios), 5 (caminhos do Windows e UNC), 7 (tabelas
#: markdown) e parte da 4 (`@@`, `&&`) e da 10: caracteres que nunca ocorrem em linguagem falada.
#: Um so basta. E a rede que apanha `</`, `/>`, `<invok`, `&lt;invoke&gt;`, `{ok: 1`, `[1, 2, 3`,
#: `|a|b`, `C:\Users`, `name=Bash>`, `resumo_falado`, `2>&1` e as cores de terminal.
_PADRAO_CARACTER_DE_CODIGO = re.compile(r"[<>{}\[\]|\\=_&@~^\t\x00-\x08\x0b-\x1f\x7f]")

#: CATEGORIA 1 (o que resta): inicio de tag com ou sem fecho na mesma linha. Ja implicado pelo
#: caracter `<`, fica escrito porque e a categoria que deu origem a D59.
_PADRAO_TAG = re.compile(r"<\s*/?\s*[a-zA-Z!?/]|/\s*>")
#: CATEGORIA 1: entidade HTML (`&lt;`, `&#60;`) — a tag escapada e na mesma uma tag.
_PADRAO_ENTIDADE_HTML = re.compile(r"&#?\w{1,8};")

#: CATEGORIA 2 (tres crases): duas ou mais crases seguidas sao sempre uma cerca de codigo, mesmo
#: quando o modelo escreve duas em vez de tres ou nao a fecha. A crase SOZINHA continua a ser so
#: marcacao markdown e e retirada do texto (`linha X` e falada como "linha X", D48).
_PADRAO_CRASE_DUPLA = re.compile(r"``")
#: CATEGORIA 2: a mesma cerca escrita com aspas triplas (o delimitador de um bloco de codigo em
#: Python) — `\"\"\"` ou `'''`, aberta ou fechada.
_PADRAO_ASPAS_TRIPLAS = re.compile(r"\"{3}|'{3}")

#: CATEGORIA 3: par "chave": valor de JSON ou de dicionario, com o valor ou sem ele (uma linha
#: truncada acaba em `"description":`).
_PADRAO_JSON_CHAVE = re.compile(r"[\"'][^\"']{0,120}[\"']\s*:")
#: CATEGORIA 3: linha que abre ou fecha um objeto/lista, mesmo por fechar.
_PADRAO_JSON_LINHA = re.compile(r"^\s*[\{\[]|[\}\]]\s*$")
#: CATEGORIA 3: par chave-valor SEM aspas, no estilo YAML ou de um dicionario escrito a mao, com
#: a virgula de continuacao que uma frase falada nao tem (`key: value,`). Exige-se a linha
#: inteira para nao comer "Encontrei dois problemas: o primeiro e facil", que e fala normal.
_PADRAO_PAR_CHAVE_VALOR = re.compile(r"^\s*[\w.-]+\s*:\s*\S+\s*,\s*$")
#: CATEGORIA 3: item entre aspas logo a seguir a um parentese — tuplo ou lista de codigo
#: (`( 'a', 1 )`). Uma citacao normal («disse "ola", e saiu») nao tem a aspa colada ao parentese.
_PADRAO_TUPLO = re.compile(r"[(\[]\s*[\"'][^\"']{0,60}[\"']\s*[,)\]]")

#: CATEGORIA 4: cabecalhos de diff/git, incluindo as formas sem espaco (`@@-1,2+3,4@@`) e as
#: reguas `---`/`+++` sozinhas.
_PADRAO_DIFF_CABECALHO = re.compile(
    r"^\s*(?:diff\s|index [0-9a-fA-F]{4}|-{3}|\+{3}|@@|commit [0-9a-fA-F]{7,40}\b"
    r"|Author:|Date:|Merge:|HEAD\b)"
)
#: CATEGORIA 4: corpo de um diff unificado. `+` no inicio da linha nunca e linguagem natural;
#: `-` so quando lhe segue algo colado (`-linha`), para nao comer uma lista markdown `- item`,
#: que e linguagem natural — o corpo de um diff a serio vem sempre debaixo de um cabecalho e sai
#: com ele no varrimento por bloco.
_PADRAO_DIFF_LINHA = re.compile(r"^\s*(?:\+|-\S)")
#: CATEGORIA 4: saida de `git status`, `git log`, `git commit` e afins, na forma curta e na longa.
_PADRAO_SAIDA_DE_GIT = re.compile(
    r"^\s*(?:On branch |Your branch |HEAD detached|nothing to commit|Untracked files"
    r"|Changes (?:not staged|to be committed)|no changes added"
    r"|(?:modified|new file|deleted|renamed|copied|both modified):"
    r"|\?\?\s|[AMDRUC]{1,2}\s+\S*[/\\]|[0-9a-f]{7,40}\s+\S|Fast-forward|Already up to date)"
    r"|\b\d+ files? changed\b|\b(?:insertion|deletion)s?\(|\bworking tree clean\b"
    r"|\bup to date with\b|\bbranch is (?:ahead|behind)\b|\b(?:origin|upstream)/\w"
)
#: CATEGORIA 4: uma linha que e so um comando de consola (sem pontuacao de frase no fim). A lista
#: e de comandos escritos em minusculas no inicio da linha: uma frase portuguesa nao comeca assim.
_PADRAO_LINHA_DE_COMANDO = re.compile(
    r"^(?:git|gh|npm|npx|pnpm|yarn|pip|pytest|python|py|node|cargo|go|docker|make|curl|wget"
    r"|ssh|scp|choco|winget|ls|dir|cd|cat|type|rm|del|mv|move|cp|copy|mkdir|echo|export|set"
    r"|source|sudo|chmod|grep|find|tar|zip|unzip)\s+\S[^.!?]*$"
)

#: CATEGORIA 6: a linha comeca por `$` ou `>`, com ou sem espaco a seguir — `>dir`, `>>> print(1)`
#: e `$env:PATH` sao exatamente a sintaxe de consola que o inaceitavel n.7 proibe ler em voz alta.
#: (A tentativa 2 exigia um espaco a seguir ao simbolo; a D59.1 nao o exige.)
_PADRAO_PROMPT = re.compile(r"^\s*[$>]")
#: CATEGORIA 6: variavel de shell em qualquer sitio da linha (`$env:PATH`, `$PWD`, `${HOME}`,
#: `$(comando)`, `%USERPROFILE%`). `$50` e dinheiro e nao casa: exige-se letra, `_`, `{` ou `(`.
_PADRAO_VARIAVEL_SHELL = re.compile(r"\$[A-Za-z_{(]|%\w+%")

#: CATEGORIA 5: caminho do Windows com letra de unidade, com barra invertida OU normal
#: (`C:\Users`, `c:/Users`), e a letra de unidade sozinha no fim da linha (o que sobra quando o
#: caminho se parte em linhas).
_PADRAO_CAMINHO_WINDOWS = re.compile(r"(?<![\w:])[A-Za-z]:(?=[\\/])|(?<![\w:])[A-Za-z]:\s*$")
#: CATEGORIA 5: caminho UNC de rede. Ja implicado pela barra invertida; fica escrito por clareza.
_PADRAO_CAMINHO_UNC = re.compile(r"\\\\[\w.$-]")
#: CATEGORIA 5: caminho com barra normal em todas as formas que nao sao "e/ou" nem uma data:
#: absoluto (`/usr/local`), relativo (`./scripts`, `../x`), de home (`~/bin`), com duas barras
#: (`docs/forja/RUN`, exige uma letra para nao apanhar `20/09/2026`), com extensao depois da barra
#: (`jarvis/app.py`) ou terminado em barra (`tests/fixtures/`).
_PADRAO_CAMINHO_POSIX = re.compile(
    r"(?:(?<=\s)|^)(?:\.{1,2}|~)?/[\w.-]"
    r"|(?<![\w.-])(?=[\w.-]*[A-Za-z])[\w.-]+/[\w.-]*/"
    r"|(?<![\w.-])[\w.-]+/[\w.-]*\.[A-Za-z0-9]{1,10}(?![\w])"
    r"|(?<![\w.-])[\w.-]+/(?=\s|$)"
)
#: CATEGORIA 5: nome de ficheiro de codigo/dados mesmo sem pasta nenhuma (`app.py`, `notas.md`):
#: ler uma extensao em voz alta e exatamente o "parece avariado" do inaceitavel n.7.
_PADRAO_FICHEIRO_SEM_PASTA = re.compile(
    r"(?<![\w.-])[\w-]+\.(?:py|pyc|pyd|pyi|ipynb|js|mjs|cjs|ts|tsx|jsx|json|jsonl|md|txt|log"
    r"|ya?ml|toml|cfg|ini|conf|csv|tsv|xml|html?|css|scss|sh|bash|zsh|bat|cmd|ps1|psm1|exe|dll"
    r"|so|dylib|wav|mp3|mp4|png|jpe?g|gif|svg|ico|pdf|zip|gz|tar|whl|sql|db|sqlite|lock|rs|go"
    r"|java|kt|c|cc|cpp|h|hpp|cs|rb|php|swift|env)(?![\w])",
    re.IGNORECASE,
)
#: CATEGORIA 5: ficheiro oculto ou de configuracao sem extensao nenhuma (`.gitignore`, `.env`,
#: `Dockerfile`). `node_modules` e `package-lock` ja caem pelo `_` e pelo `-`.
_PADRAO_FICHEIRO_SEM_EXTENSAO = re.compile(
    r"(?<![\w.])\.(?:git\w*|env\w*|venv|vscode|claude|idea|ssh|npmrc|editorconfig|dockerignore)"
    r"(?![\w])"
    r"|\b(?:Dockerfile|Makefile|CMakeLists|Procfile|Gemfile|Jenkinsfile)\b"
)

#: CATEGORIA 8: URL com esquema, incluindo a forma partida em linhas (`https` + `://...`), a
#: forma com uma barra so (`https:/exemplo`) e a forma so com `www.`.
#: A palavra `https` sozinha tambem sai: e o que resta quando o URL se parte em duas linhas
#: (`Ve em https` + `://exemplo.pt/guia`), e ninguem diz "agachetetepes" em voz alta.
_PADRAO_URL = re.compile(
    r"://|\bwww\.|\bhttps?\b|\b(?:ftps?|file|ssh|git|mailto)\s*:", re.IGNORECASE
)
#: CATEGORIA 8: dominio seguido de caminho (`exemplo.pt/guia`, `sub.exemplo.io/x`): inequivoco,
#: seja qual for o dominio de topo.
_PADRAO_URL_COM_CAMINHO = re.compile(r"(?<![\w-])[\w-]+\.[A-Za-z]{2,10}/\S")
#: CATEGORIA 8: URL sem esquema, sem `www` e sem caminho (`publico.pt`, `exemplo.co.uk`). A lista
#: de dominios de topo poe o mundo do Sponsor (`.pt`) a frente e junta-lhe os genericos.
#: SEM `re.IGNORECASE` de proposito, com a variante em maiusculas escrita a parte: assim
#: `PUBLICO.PT` cai na mesma, e duas frases coladas sem espaco ("acabei.Depois") nao caem — uma
#: maiuscula seguida de minuscula nunca e um dominio de topo. Era exatamente aqui que a
#: tentativa 2 deixava passar `publico.pt`: a lista nao tinha `.pt`.
_DOMINIOS_DE_TOPO = (
    "pt com org net io dev ai app eu co uk es fr br us ca nl au "
    "info biz tv xyz site online cloud tech store blog news live link page gov edu mil "
    "int gg sh ly"
).split()
_PADRAO_URL_SEM_ESQUEMA = re.compile(
    r"(?<![\w-])[\w-]+\.(?:"
    + "|".join(_DOMINIOS_DE_TOPO + [dominio.upper() for dominio in _DOMINIOS_DE_TOPO])
    + r")(?![\w])"
)

#: CATEGORIA 9: inicio de traceback, com dois pontos ou sem eles, e as linhas de ligacao entre
#: tracebacks encadeados.
_PADRAO_TRACEBACK_INICIO = re.compile(
    r"^\s*Traceback\b|^\s*During handling of the above exception"
    r"|^\s*The above exception was the direct cause|^\s*Stack trace\b|^\s*Caused by:"
)
#: CATEGORIA 9: linha de moldura de um traceback, em Python (`File "...", line N`) e em
#: JavaScript/Java (`at Object.foo (...)`).
_PADRAO_TRACEBACK_FICHEIRO = re.compile(r'^\s*File "|^\s*at [\w.$<]+\s*\(|^\s*\.{3}\s*\d+ more')
#: CATEGORIA 9: o nome do erro, onde quer que esteja na linha (`ValueError: x`,
#: `E   AssertionError`, `raise KeyboardInterrupt`). `Erro`/`erro` em portugues NAO casa:
#: isto e o identificador ingles.
_PADRAO_TRACEBACK_ERRO = re.compile(
    r"(?:^|[\s.])[\w.]*(?:Error|Exception|Interrupt|Traceback|SystemExit)(?![a-z])"
)

#: CATEGORIA 10 (o resto): linha de log com timestamp.
_PADRAO_LINHA_DE_LOG = re.compile(
    r"^\s*\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}|^\s*(?:ERROR|WARNING|DEBUG|INFO|TRACE|FATAL)\b[: ]"
)
#: CATEGORIA 10: saida de arnes de testes.
_PADRAO_SAIDA_DE_TESTES = re.compile(r"^\s*Ran \d+ tests?\b|^\s*FAILED\b|^\s*OK \(|^\s*\.{3,}\s*$")
#: CATEGORIA 10: marcador de titulo markdown ou comentario de consola no inicio da linha — o
#: cardinal seria lido em voz alta. O paragrafo por baixo do titulo continua a falar-se, porque
#: um titulo markdown e sempre seguido de uma linha em branco, que fecha o bloco proibido.
_PADRAO_TITULO_MARKDOWN = re.compile(r"^\s*#{1,6}\s")
#: CATEGORIA 10: linha feita so de simbolos (reguas `====`, `***`, cercas `~~~`).
_PADRAO_LINHA_DE_SIMBOLOS = re.compile(r"^\s*[-=*_~#+/\\.]{3,}\s*$")

_PADROES_DE_LINHA_PROIBIDA = (
    _PADRAO_CARACTER_DE_CODIGO,
    _PADRAO_TAG,
    _PADRAO_ENTIDADE_HTML,
    _PADRAO_CRASE_DUPLA,
    _PADRAO_ASPAS_TRIPLAS,
    _PADRAO_JSON_CHAVE,
    _PADRAO_JSON_LINHA,
    _PADRAO_PAR_CHAVE_VALOR,
    _PADRAO_TUPLO,
    _PADRAO_DIFF_CABECALHO,
    _PADRAO_DIFF_LINHA,
    _PADRAO_SAIDA_DE_GIT,
    _PADRAO_LINHA_DE_COMANDO,
    _PADRAO_PROMPT,
    _PADRAO_VARIAVEL_SHELL,
    _PADRAO_CAMINHO_WINDOWS,
    _PADRAO_CAMINHO_UNC,
    _PADRAO_CAMINHO_POSIX,
    _PADRAO_FICHEIRO_SEM_PASTA,
    _PADRAO_FICHEIRO_SEM_EXTENSAO,
    _PADRAO_URL,
    _PADRAO_URL_COM_CAMINHO,
    _PADRAO_URL_SEM_ESQUEMA,
    _PADRAO_TRACEBACK_INICIO,
    _PADRAO_TRACEBACK_FICHEIRO,
    _PADRAO_TRACEBACK_ERRO,
    _PADRAO_LINHA_DE_LOG,
    _PADRAO_SAIDA_DE_TESTES,
    _PADRAO_TITULO_MARKDOWN,
    _PADRAO_LINHA_DE_SIMBOLOS,
)
#: Uma crase tripla nunca chega a voz nem depois de junta: ver `_sem_blocos_de_tres_crases`.
_CRASE_TRIPLA = "```"
#: Uma "palavra" (sequencia sem espacos) maior do que o limite falado nunca e linguagem natural:
#: nao caberia numa frase falada e `cortar_no_limite` nem a conseguiria cortar. Verificar isto
#: ANTES dos padroes e tambem o que mantem o filtro linear (ver o cabecalho do modulo).
MAXIMO_CARACTERES_POR_PALAVRA = MAXIMO_CARACTERES_FALADOS
#: Teto por LINHA do texto de entrada (nao por palavra e NAO pela resposta junta), pedido
#: explicitamente pelo veredicto da tentativa 2 alem do teto por palavra: nenhuma frase falada
#: tem ~2000 caracteres numa unica linha, e verificar isto PRIMEIRO, antes de qualquer padrao
#: correr, evita que os 28 padroes cheguem sequer a ver uma linha gigante (o teto por palavra ja
#: cobre o caso do token unico sem espacos; este cobre a linha inteira, tambem quando tem muitas
#: palavras curtas separadas por espacos). Aplica-se SO em `_linhas_falaveis`: uma resposta de
#: prosa natural com varios paragrafos pode passar largamente dos 2000 caracteres somados e
#: continua falada (corta-se depois aos 200), porque a soma das linhas nao e uma linha.
MAXIMO_CARACTERES_POR_LINHA = 2000


def _tem_palavra_impossivel_de_falar(linha: str) -> bool:
    """Ha na linha um token sem espacos maior do que tudo o que cabe numa resposta falada."""
    return any(len(palavra) > MAXIMO_CARACTERES_POR_PALAVRA for palavra in linha.split())


def _linha_longa_demais(linha: str) -> bool:
    """Uma LINHA do texto de entrada maior do que `MAXIMO_CARACTERES_POR_LINHA`.

    So vale no caminho por linha (`_linhas_falaveis`), nunca sobre o texto ja junto: a juncao
    das linhas sobreviventes e a RESPOSTA inteira, nao uma linha, e prosa natural limpa de 2500
    caracteres em varios paragrafos tem de continuar a ser falada (D59.2 e D59.4 — a frase de
    recurso so entra quando nao sobra nada falavel, e dizer "codigo ou dados tecnicos" sobre
    prosa seria inventar conteudo). Foi exatamente essa a regressao da tentativa 1 desta retoma.
    """
    return len(linha) > MAXIMO_CARACTERES_POR_LINHA


def _linha_proibida(linha: str) -> bool:
    """Uma linha e proibida se casar com QUALQUER categoria de exclusao (D59.1).

    O teste barato do tamanho da maior palavra corre sempre primeiro, antes de qualquer padrao.
    O teto por LINHA nao esta aqui de proposito: esta funcao e reutilizada por `texto_proibido`
    sobre o texto JA JUNTO (ver `_linha_longa_demais`).
    """
    if _tem_palavra_impossivel_de_falar(linha):
        return True
    return any(padrao.search(linha) for padrao in _PADROES_DE_LINHA_PROIBIDA)


def texto_proibido(texto: str) -> bool:
    """O mesmo filtro por exclusao aplicado a um texto JA numa unica linha (D59.1/D59.2).

    E esta a verificacao que fecha o defeito da tentativa 1: o filtro por linha nao ve a tag
    que a JUNCAO das linhas sobreviventes remonta (`Esta tudo bem <invoke` + `name="Bash">`).
    `resumo_falado` chama isto sobre a frase exata que vai ser lida, depois de juntar e depois
    de cortar; nada vai a voz sem passar aqui.
    """
    numa_linha = " ".join(texto.split())
    if not numa_linha:
        return False
    return _CRASE_TRIPLA in numa_linha or _linha_proibida(numa_linha)


def _sem_blocos_de_tres_crases(texto: str) -> str:
    """Tira blocos ```...``` inteiros. Uma crase tripla por fechar descarta o resto (D59.2):
    o que vem depois dela nunca se sabe se e seguro, por isso nao se arrisca.
    """
    sem_fechados = re.sub(r"```.*?```", " ", texto, flags=re.S)
    return sem_fechados.split("```", 1)[0]


def _linhas_falaveis(texto: str) -> list[str]:
    """As linhas que sobram depois do filtro por exclusao (D59.1/D59.2).

    Varrimento por BLOCO, nao so por linha: uma vez que uma linha e proibida, as linhas
    seguintes ficam tambem de fora ate a proxima linha em branco — e assim que uma tabela git
    status, um traceback com o corpo do codigo por baixo, ou um diff com linhas de contexto sem
    marca nenhuma saem inteiros, em vez de deixar passar o meio do bloco tecnico por nao ter, ele
    proprio, nenhuma das marcas da lista.

    E aqui, e so aqui, que vale o teto por LINHA (`_linha_longa_demais`): e este o unico sitio
    onde "linha" quer mesmo dizer uma linha do texto de entrada.
    """
    faladas: list[str] = []
    dentro_de_bloco_proibido = False
    for linha in texto.splitlines():
        if not linha.strip():
            dentro_de_bloco_proibido = False
            faladas.append(linha)
            continue
        if dentro_de_bloco_proibido or _linha_longa_demais(linha) or _linha_proibida(linha):
            dentro_de_bloco_proibido = True
            continue
        faladas.append(linha)
    return faladas


def texto_falavel(resposta: str) -> str:
    """So o que passa o filtro por exclusao, numa unica linha, sem crases nem espacos a mais.

    Funcao pura, sem limite de caracteres nem prefixo — usada por `resumo_falado` e pelos
    testes que querem verificar o filtro isoladamente.

    A exclusao corre DUAS vezes: linha a linha (para tirar o pedaco proibido inteiro e manter o
    texto natural a volta) e depois outra vez sobre o resultado JA JUNTO. Sem a segunda, duas
    linhas inocentes uma a uma voltam a formar a tag da linha 652 do log depois do filtro; com
    ela, um texto junto que volte a casar com uma categoria proibida devolve "" e quem chama cai
    na frase de recurso (D59.2: inteiro ou nada, nunca meio).
    """
    sem_blocos = _sem_blocos_de_tres_crases(resposta or "")
    linhas = _linhas_falaveis(sem_blocos)
    junto = " ".join(" ".join(linhas).replace("`", "").split())
    if texto_proibido(junto):
        return ""
    return junto


#: Pontuacao que pode fechar uma frase falada.
_PONTUACAO_DE_FIM_DE_FRASE = ".!?"


def _ultimo_fim_de_frase(texto: str, limite: int) -> int:
    """Indice da ultima pontuacao dentro de `limite` que e MESMO um fim de frase (D59.3).

    E fim de frase so quando o caracter seguinte, no texto INTEIRO (nao na janela), e espaco ou
    nao existe. Sem esta verificacao, o ponto de `A versao 3.14` ou de `jarvis.app` passa por
    fim de frase e a voz diz "a versão três vírgula" — o corte a meio da palavra que o criterio
    3 proibe, e o "parece avariado" do inaceitavel n.7.
    """
    for indice in range(min(limite, len(texto)) - 1, 0, -1):
        if texto[indice] not in _PONTUACAO_DE_FIM_DE_FRASE:
            continue
        seguinte = texto[indice + 1] if indice + 1 < len(texto) else ""
        if seguinte == "" or seguinte.isspace():
            return indice
    return -1


def cortar_no_limite(texto: str, limite: int) -> str:
    """Corta `texto` a `limite` caracteres sem nunca partir uma palavra ao meio (D59.3).

    Prefere o fim de uma frase de verdade (ver `_ultimo_fim_de_frase`); nao havendo, corta no
    fim da ultima palavra completa e acrescenta "...". Se nem um espaco existir dentro do limite
    (uma unica palavra maior do que o limite), devolve "" — nao ha corte seguro, quem chama
    trata isso como "nada falavel".
    """
    if len(texto) <= limite:
        return texto
    fim_de_frase = _ultimo_fim_de_frase(texto, limite)
    if fim_de_frase > 0:
        cortado = texto[: fim_de_frase + 1].strip()
        if cortado:
            return cortado
    janela = texto[:limite]
    if texto[limite].isspace():
        # a janela acaba exatamente numa fronteira de palavra: nada ficou partido
        cortado = janela.rstrip()
        if cortado:
            return cortado + "..."
    fim_de_palavra = janela.rfind(" ")
    if fim_de_palavra > 0:
        return janela[:fim_de_palavra].rstrip() + "..."
    return ""


def resumo_falado(resposta: str, limite: int = MAXIMO_CARACTERES_FALADOS) -> str:
    """O que a voz le de uma resposta do Claude Code (D59, fecha o defeito da linha 650/652).

    So linguagem natural chega a voz (filtro por exclusao, `texto_falavel`); um pedaco proibido
    nunca se le em parte — ou sai inteiro, ou a resposta cai para a frase de recurso fixa; o
    corte por tamanho nunca parte uma palavra ao meio; a origem fica sempre nomeada, como frase
    nao verificada (D48.4/D59.5). Quem quiser a resposta inteira, em bruto, le o log (D59.6) —
    esta funcao nunca e chamada para o que vai para o log.
    """
    original = resposta or ""
    if not original.strip():
        return FRASE_RECURSO_SEM_TEXTO
    falavel = texto_falavel(original)
    if not falavel:
        return FRASE_RECURSO_SO_TECNICO
    cortado = cortar_no_limite(falavel, limite)
    if not cortado or texto_proibido(cortado):
        # o corte nunca devia criar conteudo proibido a partir de texto limpo, mas a frase que
        # vai as colunas e verificada na forma exata em que vai ser lida, nao na forma anterior
        return FRASE_RECURSO_SO_TECNICO
    falado = f"{PREFIXO_DA_RESPOSTA_DO_CLAUDE} {cortado}"
    if len(falado) > MAXIMO_ABSOLUTO_FALADO:
        # ultima rede, nunca deve disparar dado o limite de conteudo acima — mas se disparar,
        # corta-se pela mesma regra, nunca a meio de uma palavra (D59.3).
        falado = cortar_no_limite(falado, MAXIMO_ABSOLUTO_FALADO) or FRASE_RECURSO_SO_TECNICO
    return falado
