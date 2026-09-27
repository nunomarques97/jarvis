# Guião da sessão de naturalidade (20 trocas com a voz do Sponsor)

Uma sessão de cerca de 15 minutos com o jarvis a sério, o microfone do
Sponsor e a voz dele. São 20 trocas curtas, pela ordem da tabela: respostas
locais, estado e ditado para um projeto, perguntas gerais (uma cuja resposta
acaba numa pergunta), uma continuação sem "hey jarvis", uma tentativa de
interromper o jarvis enquanto fala e um pedido sem projeto.

`scripts/sessao_naturalidade.py` mostra cada troca no ecrã, com os marcadores
`<projeto-1>` e `<projeto-2>` trocados pelos nomes dos projetos que o jarvis
conhece (os do `config.toml` e os descobertos), guarda a hora de início e de
fim de cada troca e, depois de cada uma, pede UMA tecla:

| tecla | quer dizer |
|---|---|
| `1` | natural: soou como falar com uma pessoa |
| `2` | pouco natural (frase feita, pausa estranha, pergunta a mais, ...) |
| `3` | tive de repetir |
| `4` | disse "hey jarvis" sem precisar |

No fim, o script lê o log do jarvis (`logs/`, ignorada pelo Git) dessas
janelas e escreve o relatório em `docs/forja/evidence/` (também ignorada):

| medida | meta |
|---|---|
| trocas marcadas naturais | pelo menos 16 de 20 |
| resposta local, da última voz ao primeiro som | p50 <= 1,0 s, p95 <= 1,6 s |
| pergunta geral, da última voz à primeira frase da resposta | p50 <= 3,5 s, p95 <= 6 s |
| pergunta geral, da última voz ao primeiro som de qualquer tipo | <= 1,2 s |
| vezes que teve de repetir | no máximo 1 |
| "hey jarvis" sem precisar | 0 |
| perguntas desnecessárias do jarvis | no máximo 1 |
| interromper: do início da fala à voz parada | p95 < 300 ms, 0 interrupções falsas |

As latências contam a partir do último bloco de áudio com voz (o fim
verdadeiro da fala), não do fecho da escuta. A interrupção fica "não medida"
enquanto o log do jarvis não tiver as linhas dela. Só a voz real conta: voz
sintética e ficheiros WAV nunca contam, e um jarvis a correr com `--wav` fica
de fora. Sem sessão, o relatório diz "PENDING - Sponsor step".

## Passo do Sponsor

A sessão faz-se duas vezes: **uma agora, como linha de base**, e **outra no
fim das restantes melhorias**, para comparar.

1. Tem só UM jarvis aberto. Abre-o numa janela:
   `.venv\Scripts\python -m jarvis` (ou o atalho) e espera pela linha
   `JARVIS PRONTO`.
2. Numa segunda janela, na pasta do jarvis:
   - linha de base (agora):
     `.venv\Scripts\python scripts/sessao_naturalidade.py --fase linha-de-base`
   - no fim das melhorias:
     `.venv\Scripts\python scripts/sessao_naturalidade.py --fase final`
3. Para cada troca: Enter para começar, diz ao jarvis o que está em
   **O QUE DIZER** (com o Shift da direita ou "hey jarvis", salvo quando a
   troca diz para falar sem ele), Enter quando o jarvis acabar, e depois a
   tecla 1, 2, 3 ou 4. `p` salta a troca; `q` guarda e sai
   (`--continuar` retoma onde ficou).
4. No fim, o script escreve o relatório e diz onde ficou. Para o reescrever
   sem repetir a sessão: `.venv\Scripts\python scripts/sessao_naturalidade.py --relatorio`.

Os pedidos aos projetos só leem: nada muda nos projetos. Na troca do pedido
sem projeto, depois de o jarvis perguntar o projeto, diz o nome e depois
"abort", para nada ser enviado.

## As 20 trocas

Colunas: `id`; `tipo` (local, estado, ditado, pergunta, pergunta-que-pergunta,
seguimento, interromper, sem-projeto); `o que dizer` (em inglês, a frase a
dizer ao jarvis; os marcadores são trocados pelos nomes dos projetos);
`o que deve acontecer`; `pergunta esperada` (`sim` só quando o jarvis deve
fazer uma pergunta de esclarecimento; o recap antes de enviar não conta como
pergunta).

| id | tipo | o que dizer | o que deve acontecer | pergunta esperada |
|---|---|---|---|---|
| n-01 | local | what time is it | diz as horas numa frase curta | não |
| n-02 | local | what's the date today | diz a data | não |
| n-03 | local | how are you doing | responde numa frase social curta | não |
| n-04 | estado | what's the status of <projeto-1> | diz o estado do <projeto-1> em poucas frases | não |
| n-05 | estado | give me the report for <projeto-2> | lê o resumo do relatório do <projeto-2> | não |
| n-06 | ditado | tell <projeto-1> to list the files in the docs folder, don't change anything | um recap curto; responde "yes" e o pedido é enviado | não |
| n-07 | pergunta | what's the weather like in Lisbon today | um som ou uma frase curta logo, e a resposta falada a seguir | não |
| n-08 | seguimento | (sem "hey jarvis" nem tecla, logo a seguir à resposta) and what about tomorrow | continua a pergunta anterior sem pedir o projeto | não |
| n-09 | pergunta-que-pergunta | I want to start learning to cook, can you help me pick a first dish | a resposta acaba numa pergunta de volta | não |
| n-10 | seguimento | (sem "hey jarvis" nem tecla) responde à pergunta dele, por exemplo: something quick, I have twenty minutes | continua a pergunta geral, nunca pergunta o projeto | não |
| n-11 | sem-projeto | tell it to add a short section about tests to the readme | pergunta o projeto e diz os que conhece; diz "<projeto-1>" e depois "abort": nada é enviado | sim |
| n-12 | interromper | tell me the history of Lisbon in a few sentences; enquanto ele fala, diz: okay, that's enough | cala-se logo que começas a falar | não |
| n-13 | local | what time is it | diz as horas | não |
| n-14 | ditado | tell <projeto-2> to summarise the last commit in one line, don't change anything | um recap curto; responde "yes" e o pedido é enviado | não |
| n-15 | estado | how is <projeto-1> doing | diz o estado do <projeto-1> | não |
| n-16 | pergunta | who won the last Formula One race | a resposta falada numa ou duas frases | não |
| n-17 | local | go to sleep | diz que vai dormir | não |
| n-18 | local | (a dormir) hey jarvis, what time is it | acorda e diz as horas de uma vez | não |
| n-19 | pergunta | how long does it take to boil an egg | a resposta falada numa ou duas frases | não |
| n-20 | local | thanks, that's all | uma resposta curta, ou nada | não |
