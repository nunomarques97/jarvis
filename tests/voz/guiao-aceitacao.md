# Guião da aceitação diária com a voz do Sponsor

Uma sessão de cerca de 15 minutos com o jarvis a sério: o Sponsor faz, pela
ordem, as tarefas da tabela abaixo, com o microfone dele e a voz dele. As
tarefas cobrem os quatro casos de uso: (a) ditar um prompt para a sessão do
projeto, (b) ouvir o estado e o relatório, (c) lançar, parar e retomar um run
FORJA e (d) a conversa mãos-livres com o Claude, mais os comandos locais.

`scripts/aceitacao_sponsor.py` mostra cada tarefa no ecrã com os marcadores
trocados pelos nomes do `config.toml` local, guarda a hora de início e de fim
de cada uma e, no fim, lê o log do jarvis (`logs/`, ignorada pelo Git) dessas
janelas para medir o acerto, a latência e a cobertura. Só esta sessão, com a
voz real, decide se as metas estão cumpridas; voz sintética e ficheiros WAV
nunca contam. Sem sessão, a evidência fica "PENDENTE — passo do Sponsor".

## Antes de começar

1. Tem só UM jarvis aberto. Abre-o numa janela:
   `.venv\Scripts\python -m jarvis` (ou o atalho do ambiente de trabalho) e
   espera pela linha `JARVIS PRONTO`. Se houver outra janela do jarvis,
   fecha-a primeiro.
2. Numa segunda janela, na pasta do jarvis:
   `.venv\Scripts\python scripts/aceitacao_sponsor.py --projeto-teste <nome>`,
   onde `<nome>` é um projeto do `config.toml` onde se pode lançar e parar um
   run de teste (árvore limpa, nenhum run ativo). Sem `--projeto-teste`, as
   tarefas do caso (c) são saltadas. As tarefas do caso (c) lançam e param a
   sério um run FORJA curto nesse projeto, com um objetivo que só lê.
3. Antes da primeira tarefa, o script mostra um ecrã curto com estas mesmas
   regras; Enter segue.
4. Para falar com o jarvis: carrega no Shift da direita enquanto falas, ou
   começa a frase com "hey jarvis".
5. Cada tarefa mostra três linhas em destaque: **O QUE DIZER** (a frase de
   exemplo), **NO FIM RESPONDE** (o que dizer ao jarvis quando ele acabar de
   ler o texto) e **O QUE DEVE ACONTECER**. Enter para começar, diz a frase ao
   jarvis, responde o que diz NO FIM RESPONDE, e Enter quando o jarvis acabar.
   Depois responde com `s` ou `n` às perguntas (cada uma tem uma frase de
   ajuda). `p` salta a tarefa; `q` guarda e sai (`--continuar` retoma onde
   ficou).
6. As respostas ao jarvis: YES ("yes" / "sim") envia; ABORT ("abort" /
   "aborta"; "cancel" / "cancela" também servem) cancela e nada é enviado;
   "no, change X to Y" / "não, muda X para Y" e "add ..." / "acrescenta ..."
   corrigem o texto antes de enviar. Diz-se logo depois de o jarvis acabar de
   ler o texto, sem "hey jarvis" nem tecla. Se o jarvis não perceber, pergunta
   de novo: "Say yes to send, or abort." A linha de estado mostra
   `À ESPERA DE CONFIRMAÇÃO — A OUVIR A RESPOSTA`. Tens
   `[interprete] confirmacao_s` segundos (30 por omissão), a contar do fim do
   texto lido. A tecla e "hey jarvis" também funcionam.
7. YES envia a sério para o Claude Code do projeto. Na primeira vez que isso
   acontece num projeto, abre-se uma janela do Claude Code com um aviso:
   aceita o aviso e deixa a janela aberta. Nunca feches essa janela.
8. Usa pedidos que só leem, para não mexer nos projetos: por exemplo "diz ao
   <projeto-1> para listar os ficheiros da pasta docs. Não mudes nada." As
   frases de O QUE DIZER já são assim.

Não faças mais nada com o jarvis durante a sessão: tudo o que ele ouvir dentro
da janela de uma tarefa conta para essa tarefa.

## Como se mede

- **Intenção e projeto à primeira** (meta >= 90%): a primeira frase de cada
  tarefa (não conta a resposta ao recap) tem a intenção e o projeto da tabela.
  `—` quer dizer que o jarvis não pode escolher projeto nenhum.
- **Ditados aceites sem correção** (meta >= 80%): nas tarefas de ditado com
  fluxo `confirmar` ou `conversa`, o primeiro recap foi aceite com "sim", sem
  "não, muda..." nem "acrescenta...". Um "sim" mal ouvido não conta como
  correção.
- **Pedidos inventados** (meta 0): em cada ditado e lançamento, o Sponsor
  carrega `s` se o que foi enviado tinha algum pedido que ele não fez.
- **Latências** (metas de ponta a ponta do jarvis integrado): horas, da fala
  ao início da resposta falada, p50 <= 1,2 s e p95 <= 2,0 s; ditado, da fala
  ao início do recap falado, p50 <= 2,5 s e p95 <= 4,0 s; primeiro sinal de
  vida (linha A PENSAR) sempre <= 1,0 s.
- **Cobertura**: pelo menos uma tarefa passada em cada caso (a), (b), (c) e
  (d). Uma tarefa passa quando o log mostra o fluxo pedido até ao fim e o
  Sponsor diz que o jarvis fez o que ele pediu.

Fluxos (o que responder no fim): `imediato` corre sem pergunta, não respondes
nada; `confirmar` é o texto lido e YES; `corrigir` é o texto lido, uma correção
("no, change X to Y") ou um acrescento ("add ..."), e YES ao texto novo;
`cancelar` é o texto lido e ABORT (ou "cancel"), sem nada enviado; `projeto` é
um ditado sem projeto, o nome do projeto quando o jarvis pergunta e YES;
`conversa` é um ditado que pede ao Claude uma pergunta, YES, a tua resposta dita
logo depois de o jarvis ler a pergunta do Claude, e YES outra vez.

Os exemplos são pedidos que só leem e acabam em "Don't change anything." /
"Não mudes nada.": diz a frase de O QUE DIZER, ou um pedido teu que também só
leia.

| id | caso | tarefa | exemplo (en) | exemplo (pt) | o que deve acontecer | intenção | projeto | fluxo |
|----|------|--------|--------------|--------------|----------------------|----------|---------|-------|
| l-01 | local | Pergunta as horas. | what time is it | que horas são | O jarvis diz as horas logo, sem perguntar nada. | horas | — | imediato |
| a-01 | a | Dita ao <projeto-1> um pedido curto, de uma frase, e confirma. | tell <projeto-1> to list the tests for the config loader. Don't change anything. | diz ao <projeto-1> para listar os testes do carregador da configuração. Não mudes nada. | O jarvis lê o texto e pergunta "Send it?" ("Envio?"); depois de YES diz "Sent to <projeto-1>" ("Enviado para o <projeto-1>") ou abre a janela do Claude Code. | ditar_prompt | <projeto-1> | confirmar |
| a-02 | a | Dita ao <projeto-2> um pedido com dois detalhes (um nome e um número) e confirma. | in <projeto-2>, find the timeout option and tell me if it is above thirty seconds. Don't change anything. | no <projeto-2>, procura a opção timeout e diz-me se passa de trinta segundos. Não mudes nada. | O jarvis lê o texto e pergunta "Send it?" ("Envio?"); depois de YES diz "Sent to <projeto-2>" ("Enviado para o <projeto-2>") ou abre a janela do Claude Code. | ditar_prompt | <projeto-2> | confirmar |
| a-03 | a | Dita ao <projeto-1> um pedido longo, de duas ou três frases, como o escreverias, e confirma. | for <projeto-1>: the startup is slow. Find out which step takes longest and tell me. Don't change anything. | para o <projeto-1>: o arranque está lento. Descobre que passo demora mais e diz-me. Não mudes nada. | O jarvis lê o texto e pergunta "Send it?" ("Envio?"); depois de YES diz "Sent to <projeto-1>" ("Enviado para o <projeto-1>") ou abre a janela do Claude Code. | ditar_prompt | <projeto-1> | confirmar |
| b-01 | b | Pergunta como está o run do <projeto-1>. | how is the run on <projeto-1> going | como está o run do <projeto-1> | O jarvis diz logo como está o run do <projeto-1>, sem perguntar nada. | estado | <projeto-1> | imediato |
| b-02 | b | Pede para ler o relatório do <projeto-1>. | read the report for <projeto-1> | lê o relatório do <projeto-1> | O jarvis lê logo o relatório do <projeto-1>, sem perguntar nada. | ler_relatorio | <projeto-1> | imediato |
| a-04 | a | Dita ao <projeto-2> um pedido; quando o jarvis o ler, corrige uma palavra e confirma o texto novo. | tell <projeto-2> to list the tests of the login screen. Don't change anything. | diz ao <projeto-2> para listar os testes do ecrã de login. Não mudes nada. | O jarvis lê o texto e pergunta "Send it?" ("Envio?"); depois de "no, change login to settings" lê o texto novo e pergunta outra vez; depois de YES diz "Sent to <projeto-2>" ("Enviado para o <projeto-2>") ou abre a janela do Claude Code. | ditar_prompt | <projeto-2> | corrigir |
| l-02 | local | Pede para abrir o editor no <projeto-2> e confirma. | open the editor on <projeto-2> | abre o editor no <projeto-2> | O jarvis pergunta "Confirm?" ("Confirmas?"); depois de YES abre o editor na pasta do <projeto-2>. | abrir_editor | <projeto-2> | confirmar |
| a-05 | a | Dita ao <projeto-1> um pedido; quando o jarvis o ler, acrescenta um detalhe e confirma o texto novo. | ask <projeto-1> to read the README and summarize it. Don't change anything. | pede ao <projeto-1> para ler o README e resumi-lo. Não mudes nada. | O jarvis lê o texto e pergunta "Send it?" ("Envio?"); depois de "add that the summary has three lines" lê o texto novo e pergunta outra vez; depois de YES diz "Sent to <projeto-1>" ("Enviado para o <projeto-1>") ou abre a janela do Claude Code. | ditar_prompt | <projeto-1> | corrigir |
| a-06 | a | Dita ao <projeto-2> um pedido e cancela-o. | tell <projeto-2> to list the old log files. Don't change anything. | diz ao <projeto-2> para listar os ficheiros de log antigos. Não mudes nada. | O jarvis lê o texto e pergunta "Send it?" ("Envio?"); depois de ABORT diz "Cancelled, nothing was sent." ("Cancelado, não enviei nada.") e não envia nada. | ditar_prompt | <projeto-2> | cancelar |
| l-03 | local | Pergunta as horas outra vez. | tell me the time | diz-me as horas | O jarvis diz as horas logo, sem perguntar nada. | horas | — | imediato |
| a-07 | a | Dita um pedido sem dizer o projeto; quando o jarvis perguntar, diz o <projeto-1> e confirma. | tell claude to list the tasks that are left. Don't change anything. | diz ao claude para listar as tarefas que faltam. Não mudes nada. | O jarvis lê o texto e pergunta para que projeto é; depois de dizeres <projeto-1> pergunta "Send it?" ("Envio?"); depois de YES diz "Sent to <projeto-1>" ("Enviado para o <projeto-1>") ou abre a janela do Claude Code. | ditar_prompt | — | projeto |
| b-03 | b | Pergunta o estado do <projeto-2>. | what is the status of <projeto-2> | qual é o estado do <projeto-2> | O jarvis diz logo o estado do <projeto-2>, sem perguntar nada. | estado | <projeto-2> | imediato |
| c-01 | c | Lança um run no <projeto-teste> com um objetivo que só lê e confirma. | start a run on <projeto-teste> to read the README and list its sections. Don't change anything. | lança um run no <projeto-teste> para ler o README e listar as secções. Não mudes nada. | O jarvis lê o objetivo e pergunta "Confirm?" ("Confirmas?"); depois de YES lança um run FORJA real e curto no <projeto-teste>. | lancar_run | <projeto-teste> | confirmar |
| c-02 | c | Pede para parar o run do <projeto-teste> e confirma. | stop the run on <projeto-teste> | para o run do <projeto-teste> | O jarvis pergunta "Confirm?" ("Confirmas?"); depois de YES para o run do <projeto-teste>. | parar_run | <projeto-teste> | confirmar |
| c-03 | c | Pede para retomar o run do <projeto-teste> e confirma. | resume the run on <projeto-teste> | retoma o run do <projeto-teste> | O jarvis pergunta "Confirm?" ("Confirmas?"); depois de YES volta a pôr a correr o run do <projeto-teste>. | retomar_run | <projeto-teste> | confirmar |
| c-04 | c | Para outra vez o run do <projeto-teste> e confirma. | interrupt the run on <projeto-teste> | interrompe o run do <projeto-teste> | O jarvis pergunta "Confirm?" ("Confirmas?"); depois de YES para o run do <projeto-teste>. | parar_run | <projeto-teste> | confirmar |
| d-01 | d | Dita ao <projeto-1> um pedido para o Claude te fazer uma pergunta; quando o jarvis a ler, responde em voz alta e confirma. | ask <projeto-1> to ask me which of the two test files I want to read first, and wait for my answer. Don't change anything. | pede ao <projeto-1> para me perguntar qual dos dois ficheiros de teste quero ler primeiro, e esperar pela resposta. Não mudes nada. | O jarvis lê o texto e pergunta "Send it?" ("Envio?"); depois de YES diz "Sent to <projeto-1>" ("Enviado para o <projeto-1>") ou abre a janela do Claude Code; depois lê a pergunta do Claude, ouve a tua resposta e envia-a depois de outro YES. | ditar_prompt | <projeto-1> | conversa |
| l-04 | local | Manda o jarvis dormir. | go to sleep | vai dormir | O jarvis diz "Going to sleep." ("Vou dormir.") e deixa de responder até o acordares. | dormir | — | imediato |
| l-05 | local | Acorda o jarvis (começa por "hey jarvis"). | wake up | acorda | O jarvis diz "I'm awake." ("Estou acordado.") e volta a responder. | acordar | — | imediato |
| d-02 | d | Outra conversa: pede ao <projeto-2> uma pergunta de sim ou não, responde em voz alta e confirma. | ask <projeto-2> to ask me if the changelog should mention the new option, and wait for my answer. Don't change anything. | pede ao <projeto-2> para me perguntar se o changelog deve falar da opção nova, e esperar pela resposta. Não mudes nada. | O jarvis lê o texto e pergunta "Send it?" ("Envio?"); depois de YES diz "Sent to <projeto-2>" ("Enviado para o <projeto-2>") ou abre a janela do Claude Code; depois lê a pergunta do Claude, ouve a tua resposta e envia-a depois de outro YES. | ditar_prompt | <projeto-2> | conversa |
| l-06 | local | Pergunta as horas uma última vez. | what's the time now | que horas são agora | O jarvis diz as horas logo, sem perguntar nada. | horas | — | imediato |
