# Guião de treino com a voz real — inglês

Frases para adaptar o reconhecimento ao sotaque do Sponsor
(`scripts/gravar_voz.py --lingua en --treino`). Usam o vocabulário dos
comandos do jarvis — nomes de projeto, "add tests", "cancel", "claude",
confirmações e ditado — mas nenhuma repete uma frase de
`tests/voz/guiao-real-en.md`: esse guião é o conjunto de avaliação e o que
se usa para adaptar nunca pode servir para medir.

As gravações ficam numa pasta própria, `recordings/treino-en/` (ignorada
pelo Git), separada de `recordings/en/`. O gravador começa por um piloto com
as primeiras frases, verifica que há fala real e só depois pede o resto.

Ficheiro versionado: nenhum nome nem caminho real de projeto entra aqui; só
os marcadores `<projeto-1>` e `<projeto-2>`, que o gravador troca no ecrã
pelos nomes do `config.toml` local. Colunas e regras de leitura: as do
guião de avaliação.

| id | caso | frase | intenção | projeto | ativação |
|----|------|-------|----------|---------|----------|
| en-t01 | a | hey jarvis, tell <projeto-1> to add tests for the login flow | ditar_prompt | <projeto-1> | sim |
| en-t02 | a | ask claude to add tests before changing the parser | ditar_prompt | — | não |
| en-t03 | a | hey jarvis, in <projeto-2>, add tests that cover the empty list case | ditar_prompt | <projeto-2> | sim |
| en-t04 | a | tell <projeto-1> to rename the helper and add tests for it | ditar_prompt | <projeto-1> | não |
| en-t05 | a | hey jarvis, dictate to claude: check why the build is slow | ditar_prompt | — | sim |
| en-t06 | a | in <projeto-2>, ask claude to update the changelog | ditar_prompt | <projeto-2> | não |
| en-t07 | a | hey jarvis, send to <projeto-1>: remove the unused imports | ditar_prompt | <projeto-1> | sim |
| en-t08 | a | write a prompt for <projeto-2>: add tests and fix the flaky one | ditar_prompt | <projeto-2> | não |
| en-t09 | a | hey jarvis, ask claude what the next step on <projeto-1> is | ditar_prompt | <projeto-1> | sim |
| en-t10 | a | tell <projeto-2> to add tests for the date parsing | ditar_prompt | <projeto-2> | não |
| en-t11 | a | hey jarvis, ask <projeto-2> to cancel the old migration and add tests | ditar_prompt | <projeto-2> | sim |
| en-t12 | a | dictate to <projeto-1>: the error message should mention the config file | ditar_prompt | <projeto-1> | não |
| en-t13 | b | hey jarvis, how is <projeto-2> doing | estado | <projeto-2> | sim |
| en-t14 | b | hey jarvis, what is <projeto-1> working on right now | estado | <projeto-1> | sim |
| en-t15 | b | hey jarvis, give me the status of <projeto-1> | estado | <projeto-1> | sim |
| en-t16 | b | read me the report from <projeto-2> | ler_relatorio | <projeto-2> | não |
| en-t17 | b | hey jarvis, has <projeto-2> finished the run | estado | <projeto-2> | sim |
| en-t18 | b | what did claude report on <projeto-1> | ler_relatorio | <projeto-1> | não |
| en-t19 | b | hey jarvis, read the last report on <projeto-1> | ler_relatorio | <projeto-1> | sim |
| en-t20 | b | is the run on <projeto-2> still going | estado | <projeto-2> | não |
| en-t21 | c | hey jarvis, launch a run on <projeto-2> | lancar_run | <projeto-2> | sim |
| en-t22 | c | cancel the run on <projeto-2> | parar_run | <projeto-2> | não |
| en-t23 | c | hey jarvis, open a claude session for <projeto-1> | lancar_run | <projeto-1> | sim |
| en-t24 | c | pick up the run on <projeto-1> where it stopped | retomar_run | <projeto-1> | não |
| en-t25 | c | hey jarvis, start claude on <projeto-2> | lancar_run | <projeto-2> | sim |
| en-t26 | c | hey jarvis, halt the run on <projeto-1> | parar_run | <projeto-1> | sim |
| en-t27 | c | hey jarvis, resume <projeto-2> | retomar_run | <projeto-2> | sim |
| en-t28 | c | launch a new run on <projeto-1> to add tests | lancar_run | <projeto-1> | não |
| en-t29 | local | hey jarvis, do you know what time it is | horas | — | sim |
| en-t30 | local | what's the time | horas | — | não |
| en-t31 | local | hey jarvis, open vs code on <projeto-1> | abrir_editor | <projeto-1> | sim |
| en-t32 | local | open the editor for <projeto-2> | abrir_editor | <projeto-2> | não |
| en-t33 | local | hey jarvis, open the folder of <projeto-2> | abrir_pasta | <projeto-2> | sim |
| en-t34 | local | open the <projeto-1> folder | abrir_pasta | <projeto-1> | não |
| en-t35 | local | hey jarvis, silence | calar | — | sim |
| en-t36 | local | hey jarvis, stay quiet | calar | — | sim |
| en-t37 | local | hey jarvis, enter standby mode | dormir | — | sim |
| en-t38 | local | hey jarvis, back to work | acordar | — | sim |
| en-t39 | confirmacao | yes, go ahead | confirmar | — | não |
| en-t40 | confirmacao | cancel it | cancelar | — | não |
| en-t41 | confirmacao | abort | cancelar | — | não |
| en-t42 | confirmacao | no, cancel that | cancelar | — | não |
| en-t43 | confirmacao | no, change <projeto-2> to <projeto-1> | corrigir | <projeto-1> | não |
| en-t44 | confirmacao | add that claude should add tests too | acrescentar | — | não |
| en-t45 | confirmacao | yes, send it to claude | confirmar | — | não |
| en-t46 | confirmacao | no, send it to <projeto-2> instead | corrigir | <projeto-2> | não |
