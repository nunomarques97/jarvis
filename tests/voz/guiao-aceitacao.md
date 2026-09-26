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

1. Abre o jarvis numa janela: `.venv\Scripts\python -m jarvis` (ou o atalho
   do ambiente de trabalho) e espera pela linha `JARVIS PRONTO`.
2. Numa segunda janela, na pasta do jarvis:
   `.venv\Scripts\python scripts/aceitacao_sponsor.py --projeto-teste <nome>`,
   onde `<nome>` é um projeto do `config.toml` onde se pode lançar e parar um
   run de teste (árvore limpa, nenhum run ativo). Sem `--projeto-teste`, as
   tarefas do caso (c) são saltadas.
3. Para cada tarefa: Enter para começar, faz a tarefa na janela do jarvis
   (o pedido começa com a tecla de falar ou "hey jarvis"), Enter quando o
   jarvis acabar. Depois responde com `s` ou `n` às perguntas. `p` salta a
   tarefa; `q` guarda e sai (`--continuar` retoma onde ficou).
4. A resposta ao recap diz-se logo depois de o jarvis acabar de o ler, sem
   "hey jarvis" nem tecla: "yes" / "sim" envia, "abort" / "aborta" cancela
   ("cancel" / "cancela" também serve), "no, change X to Y" / "não, muda X
   para Y" e "add ..." / "acrescenta ..." corrigem. Se o jarvis não perceber,
   pergunta de novo: "Say yes to send, or abort." A linha de
   estado mostra `À ESPERA DE CONFIRMAÇÃO — A OUVIR A RESPOSTA`. Tens
   `[interprete] confirmacao_s` segundos (30 por omissão), a contar do fim do
   recap. A tecla e "hey jarvis" também funcionam.

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

Fluxos: `imediato` corre sem recap; `confirmar` é recap e "sim"; `corrigir` é
recap, uma correção ou um acrescento, e "sim" ao recap novo; `cancelar` é
recap e "abort" (ou "cancel"), sem nada enviado; `projeto` é um ditado sem projeto, a
resposta à pergunta "para que projeto?" e "sim"; `conversa` é um ditado que
pede ao Claude uma pergunta, a resposta dada na janela de conversa e "sim".

Os exemplos são só exemplos: nos ditados, diz um pedido teu, como o escreverias.

| id | caso | tarefa | exemplo (en) | exemplo (pt) | intenção | projeto | fluxo |
|----|------|--------|--------------|--------------|----------|---------|-------|
| l-01 | local | Pergunta as horas. | what time is it | que horas são | horas | — | imediato |
| a-01 | a | Dita ao <projeto-1> um pedido curto, de uma frase, e confirma. | tell <projeto-1> to add a test for the config loader | diz ao <projeto-1> para acrescentar um teste ao carregador da configuração | ditar_prompt | <projeto-1> | confirmar |
| a-02 | a | Dita ao <projeto-2> um pedido com dois detalhes (um nome e um número) e confirma. | in <projeto-2>, rename the timeout option to wait seconds and set it to thirty | no <projeto-2>, muda o nome da opção timeout para segundos de espera e põe-na a trinta | ditar_prompt | <projeto-2> | confirmar |
| a-03 | a | Dita ao <projeto-1> um pedido longo, de duas ou três frases, como o escreverias, e confirma. | for <projeto-1>: the startup is slow. Find out which step takes longest and tell me before changing anything | para o <projeto-1>: o arranque está lento. Descobre que passo demora mais e diz-me antes de mudar alguma coisa | ditar_prompt | <projeto-1> | confirmar |
| b-01 | b | Pergunta como está o run do <projeto-1>; o jarvis responde logo, sem recap. | how is the run on <projeto-1> going | como está o run do <projeto-1> | estado | <projeto-1> | imediato |
| b-02 | b | Pede para ler o relatório do <projeto-1>; o jarvis lê logo, sem recap. | read the report for <projeto-1> | lê o relatório do <projeto-1> | ler_relatorio | <projeto-1> | imediato |
| a-04 | a | Dita ao <projeto-2> um pedido; no recap corrige uma palavra ("não, muda X para Y") e confirma o recap novo. | tell <projeto-2> to add tests to the login screen | diz ao <projeto-2> para acrescentar testes ao ecrã de login | ditar_prompt | <projeto-2> | corrigir |
| l-02 | local | Pede para abrir o editor no <projeto-2> e confirma. | open the editor on <projeto-2> | abre o editor no <projeto-2> | abrir_editor | <projeto-2> | confirmar |
| a-05 | a | Dita ao <projeto-1> um pedido; no recap acrescenta um detalhe ("acrescenta que...") e confirma. | ask <projeto-1> to review the README | pede ao <projeto-1> para rever o README | ditar_prompt | <projeto-1> | corrigir |
| a-06 | a | Dita ao <projeto-2> um pedido e cancela no recap com "abort". | tell <projeto-2> to delete the old logs | diz ao <projeto-2> para apagar os logs antigos | ditar_prompt | <projeto-2> | cancelar |
| l-03 | local | Pergunta as horas outra vez. | tell me the time | diz-me as horas | horas | — | imediato |
| a-07 | a | Dita um pedido sem dizer o projeto; quando o jarvis perguntar, diz o <projeto-1> e confirma. | tell claude to list the tasks that are left | diz ao claude para listar as tarefas que faltam | ditar_prompt | — | projeto |
| b-03 | b | Pergunta o estado do <projeto-2>; o jarvis responde logo, sem recap. | what is the status of <projeto-2> | qual é o estado do <projeto-2> | estado | <projeto-2> | imediato |
| c-01 | c | Lança um run no <projeto-teste> com um objetivo curto e inofensivo e confirma. | start a run on <projeto-teste> to fix a typo in the README | lança um run no <projeto-teste> para corrigir uma gralha no README | lancar_run | <projeto-teste> | confirmar |
| c-02 | c | Pede para parar o run do <projeto-teste> e confirma. | stop the run on <projeto-teste> | para o run do <projeto-teste> | parar_run | <projeto-teste> | confirmar |
| c-03 | c | Pede para retomar o run do <projeto-teste> e confirma. | resume the run on <projeto-teste> | retoma o run do <projeto-teste> | retomar_run | <projeto-teste> | confirmar |
| c-04 | c | Para outra vez o run do <projeto-teste> e confirma. | interrupt the run on <projeto-teste> | interrompe o run do <projeto-teste> | parar_run | <projeto-teste> | confirmar |
| d-01 | d | Dita ao <projeto-1> um pedido para o Claude te fazer uma pergunta; quando ela chegar, responde na janela de conversa e confirma. | ask <projeto-1> to ask me which of the two test files I want to keep, and wait for my answer | pede ao <projeto-1> para me perguntar qual dos dois ficheiros de teste quero manter, e esperar pela resposta | ditar_prompt | <projeto-1> | conversa |
| l-04 | local | Manda o jarvis dormir. | go to sleep | vai dormir | dormir | — | imediato |
| l-05 | local | Acorda o jarvis. | wake up | acorda | acordar | — | imediato |
| d-02 | d | Outra conversa: pede ao <projeto-2> uma pergunta de sim ou não, responde na janela e confirma. | ask <projeto-2> to ask me if the changelog should mention the new option | pede ao <projeto-2> para me perguntar se o changelog deve falar da opção nova | ditar_prompt | <projeto-2> | conversa |
| l-06 | local | Pergunta as horas uma última vez. | what's the time now | que horas são agora | horas | — | imediato |
