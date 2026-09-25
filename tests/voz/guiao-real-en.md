# Guião de gravação com a voz real — inglês

Mesmas frases e mesma estrutura de `tests/voz/guiao-real-pt.md`, em inglês,
lidas pelo Sponsor com o sotaque dele (`scripts/gravar_voz.py --lingua en`).
A palavra de ativação é "hey jarvis". Só a voz real dele decide a comparação
entre as duas línguas; áudio sintético serve apenas para testar a cadeia.

Ficheiro versionado: nenhum nome nem caminho real de projeto entra aqui; só
os marcadores `<projeto-1>` e `<projeto-2>`, que o gravador troca no ecrã
pelos nomes do `config.toml` local. As gravações ficam em `recordings/`
(ignorada). Colunas e regras de leitura: as do guião português.

| id | caso | frase | intenção | projeto | ativação |
|----|------|-------|----------|---------|----------|
| en-01 | a | hey jarvis, tell <projeto-1> to add tests to the configuration module | ditar_prompt | <projeto-1> | sim |
| en-02 | a | in <projeto-2>, fix the startup error that appears when the config file is missing | ditar_prompt | <projeto-2> | não |
| en-03 | a | hey jarvis, ask <projeto-1> to explain why the latency test is failing | ditar_prompt | <projeto-1> | sim |
| en-04 | a | send to <projeto-2>: review the README and shorten the install section | ditar_prompt | <projeto-2> | não |
| en-05 | a | hey jarvis, in <projeto-1> change the error message to say which file is missing | ditar_prompt | <projeto-1> | sim |
| en-06 | a | tell <projeto-2> that the save button must stay disabled while it is saving | ditar_prompt | <projeto-2> | não |
| en-07 | a | hey jarvis, ask <projeto-1> how long the test suite takes | ditar_prompt | <projeto-1> | sim |
| en-08 | a | in <projeto-2>, add a log line with the time of each request | ditar_prompt | <projeto-2> | não |
| en-09 | a | hey jarvis, write a prompt for <projeto-1>: look for duplicated code in the scripts and suggest how to merge it | ditar_prompt | <projeto-1> | sim |
| en-10 | a | tell claude to review the changes I made today | ditar_prompt | — | não |
| en-11 | a | hey jarvis, ask <projeto-2> for a summary of what changed this week | ditar_prompt | <projeto-2> | sim |
| en-12 | a | dictate to claude: I want a list of the tasks that are left | ditar_prompt | — | não |
| en-13 | b | hey jarvis, how is the run on <projeto-1> going | estado | <projeto-1> | sim |
| en-14 | b | hey jarvis, what is the status of <projeto-2> | estado | <projeto-2> | sim |
| en-15 | b | hey jarvis, read the report for <projeto-2> | ler_relatorio | <projeto-2> | sim |
| en-16 | b | is <projeto-1> still working | estado | <projeto-1> | não |
| en-17 | b | hey jarvis, read me the summary of the last run on <projeto-1> | ler_relatorio | <projeto-1> | sim |
| en-18 | b | which task is <projeto-2> on | estado | <projeto-2> | não |
| en-19 | b | hey jarvis, is the run on <projeto-2> blocked | estado | <projeto-2> | sim |
| en-20 | b | read the latest report for <projeto-1> | ler_relatorio | <projeto-1> | não |
| en-21 | c | hey jarvis, start a run on <projeto-1> | lancar_run | <projeto-1> | sim |
| en-22 | c | resume the run on <projeto-2> | retomar_run | <projeto-2> | não |
| en-23 | c | hey jarvis, stop the run on <projeto-1> | parar_run | <projeto-1> | sim |
| en-24 | c | open a claude session on <projeto-2> | lancar_run | <projeto-2> | não |
| en-25 | c | hey jarvis, resume the session on <projeto-1> | retomar_run | <projeto-1> | sim |
| en-26 | c | start a new run on <projeto-2> to fix the tests | lancar_run | <projeto-2> | não |
| en-27 | c | hey jarvis, interrupt the run on <projeto-2> | parar_run | <projeto-2> | sim |
| en-28 | c | continue the run on <projeto-1> | retomar_run | <projeto-1> | não |
| en-29 | local | hey jarvis, what time is it | horas | — | sim |
| en-30 | local | hey jarvis, tell me the time | horas | — | sim |
| en-31 | local | hey jarvis, open the editor on <projeto-1> | abrir_editor | <projeto-1> | sim |
| en-32 | local | open vs code on <projeto-2> | abrir_editor | <projeto-2> | não |
| en-33 | local | hey jarvis, open the <projeto-2> folder | abrir_pasta | <projeto-2> | sim |
| en-34 | local | open the folder for <projeto-1> | abrir_pasta | <projeto-1> | não |
| en-35 | local | be quiet | calar | — | não |
| en-36 | local | hey jarvis, go to sleep | dormir | — | sim |
| en-37 | local | hey jarvis, wake up | acordar | — | sim |
| en-38 | local | hey jarvis, stop talking | calar | — | sim |
| en-39 | confirmacao | yes, send it | confirmar | — | não |
| en-40 | confirmacao | no, change tests to documentation | corrigir | — | não |
| en-41 | confirmacao | add that it is urgent | acrescentar | — | não |
| en-42 | confirmacao | cancel | cancelar | — | não |
| en-43 | confirmacao | yes | confirmar | — | não |
| en-44 | confirmacao | no, change <projeto-1> to <projeto-2> | corrigir | <projeto-2> | não |
