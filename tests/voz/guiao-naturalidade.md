# Guião da sessão de naturalidade (20 trocas com a voz do Sponsor)

Uma sessão de cerca de 15 minutos com o jarvis a sério, o microfone do
Sponsor e a voz dele, a conversar pelo cérebro (o Claude). São 20 trocas
curtas, pela ordem da tabela: uma saudação social, um pedido de ajuda para
cozinhar com seguimentos que só se percebem com o contexto, uma pergunta de
tempo que precisa de pesquisa, o estado de um projeto dito de forma natural,
um envio a um projeto com "yes", um aviso que chega a meio da conversa, um
pedido sem projeto, uma interrupção, dormir e acordar, e respostas locais.

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
janelas, classifica cada frase (caminho rápido local, cérebro sem pesquisa,
cérebro com pesquisa na web) e escreve o relatório em `docs/forja/evidence/`
(também ignorada):

| medida | meta |
|---|---|
| trocas marcadas naturais | pelo menos 16 de 20 |
| resposta do cérebro sem pesquisa, da última voz à primeira frase falada | p50 <= 1,5 s |
| resposta do cérebro com pesquisa, da última voz à primeira frase falada | p50 <= 3,5 s, p95 <= 6 s |
| resposta local (horas, data, dormir, acordar), da última voz ao primeiro som | p50 <= 1,0 s, p95 <= 1,6 s |
| vezes que teve de repetir | no máximo 1 |
| "hey jarvis" sem precisar | 0 |
| perguntas desnecessárias do jarvis | no máximo 1 |
| interromper: do início da fala à voz parada | p95 < 300 ms, 0 interrupções falsas |

As latências contam a partir do último bloco de áudio com voz (o fim
verdadeiro da fala), não do fecho da escuta. Uma frase do cérebro conta como
"com pesquisa" quando o log mostra que ele usou a web nesse turno. Sem
nenhuma resposta com pesquisa, essa meta fica "não medida"; a interrupção
também, enquanto o log não tiver as linhas dela. Só a voz real conta: voz
sintética e ficheiros WAV nunca contam, e um jarvis a correr com `--wav` fica
de fora. Sem sessão, o relatório diz "PENDING - Sponsor step". O relatório
também dá, como nota, os tokens de entrada de cada troca do cérebro.

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

Os pedidos aos projetos só leem: nada muda nos projetos. O aviso da troca
n-11 é a resposta do `<projeto-1>` ao pedido da troca n-10: chega sozinha
enquanto conversas. Na troca do pedido sem projeto, depois de o jarvis
perguntar o projeto, diz o nome e depois "abort", para nada ser enviado.

## As 20 trocas

Colunas: `id`; `tipo` (local, conversa, seguimento, pesquisa, estado, ditado,
aviso, sem-projeto, interromper); `o que dizer` (em inglês, a frase a dizer ao
jarvis; os marcadores são trocados pelos nomes dos projetos);
`o que deve acontecer`; `pergunta esperada` (`sim` só quando o jarvis deve
fazer uma pergunta de esclarecimento; o recap antes de enviar não conta como
pergunta).

| id | tipo | o que dizer | o que deve acontecer | pergunta esperada |
|---|---|---|---|---|
| n-01 | conversa | hey jarvis, how are you doing? | responde numa frase social curta, sem falar de projetos | não |
| n-02 | local | what time is it | diz as horas numa frase curta | não |
| n-03 | conversa | I want to start learning to cook. Can you help me? | diz que sim numa ou duas frases; pode acabar numa pergunta de volta | não |
| n-04 | seguimento | (sem "hey jarvis" nem tecla, logo a seguir à resposta) The first dish? | sugere um primeiro prato para quem começa: percebe que continua a conversa de cozinhar | não |
| n-05 | seguimento | (sem "hey jarvis" nem tecla) something quick, I have twenty minutes | ajusta a sugestão aos vinte minutos, sem perguntar o projeto | não |
| n-06 | pesquisa | what's the weather like in Lisbon today? | se demorar, um aviso curto; depois o tempo de hoje em Lisboa numa ou duas frases | não |
| n-07 | seguimento | (sem "hey jarvis" nem tecla) and tomorrow? | o tempo de amanhã em Lisboa, sem perguntar de onde | não |
| n-08 | estado | hey jarvis, how's <projeto-1> doing? | diz em poucas frases naturais como está o <projeto-1>, sem ler IDs nem listas | não |
| n-09 | estado | anything new in the report for <projeto-2>? | resume o último relatório do <projeto-2> numa ou duas frases | não |
| n-10 | ditado | tell <projeto-1> to list the files in the docs folder, don't change anything | um recap curto do pedido; responde "yes" e o pedido é enviado | não |
| n-11 | aviso | (logo a seguir, enquanto o <projeto-1> responde) tell me a fun fact about octopuses | conta o facto; a resposta do <projeto-1> que chega entretanto nunca corta a fala e fica para depois | não |
| n-12 | aviso | anything from <projeto-1>? | diz numa ou duas frases o que o <projeto-1> respondeu, ou que ainda não respondeu | não |
| n-13 | sem-projeto | tell it to add a short section about tests to the readme | pergunta qual projeto; diz "<projeto-2>" e, no recap, "abort": nada é enviado | sim |
| n-14 | interromper | tell me the history of Lisbon in a few sentences; enquanto ele fala, diz: okay, that's enough | cala-se logo que começas a falar | não |
| n-15 | pesquisa | who won the last Formula One race? | se demorar, um aviso curto; depois a resposta numa ou duas frases | não |
| n-16 | conversa | how long does it take to boil an egg? | a resposta numa ou duas frases | não |
| n-17 | local | go to sleep | diz que vai dormir e deixa de responder | não |
| n-18 | local | (a dormir) hey jarvis, wake up | acorda e diz que está acordado | não |
| n-19 | local | what's the date today | diz a data | não |
| n-20 | local | thanks, that's all | uma resposta curta, ou nada; a conversa fecha | não |
