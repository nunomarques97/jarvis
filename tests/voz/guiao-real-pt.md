# Guião de gravação com a voz real — português europeu

Frases que o Sponsor lê ao microfone com `scripts/gravar_voz.py --lingua pt`
e que `scripts/avaliar_voz.py` transcreve com cada motor STT. Só a voz real
dele decide qual motor e qual língua ficam; áudio sintético serve apenas para
testar a cadeia.

Ficheiro versionado: nenhum nome nem caminho real de projeto entra aqui. Os
projetos aparecem só como `<projeto-1>` e `<projeto-2>`; o gravador mostra no
ecrã os nomes do `config.toml` local (ignorado pelo Git) no lugar deles, e é
isso que se lê em voz alta. As gravações ficam em `recordings/` (ignorada).

Colunas (o parser de `scripts/gravar_voz.py` valida-as):

- `caso`: `a` ditado livre, `b` estado/relatório, `c` lançar/retomar/parar,
  `local` comando local, `confirmacao` resposta ao recap;
- `intenção`: intenção esperada, da lista fechada do intérprete mais as
  respostas de confirmação (`confirmar`, `corrigir`, `acrescentar`,
  `cancelar`);
- `projeto`: marcador esperado, ou `—` quando a frase não nomeia projeto;
- `ativação`: `sim` quando a frase começa pela palavra de ativação
  "boas jarvis" (metade das frases).

Como ler: voz normal, à secretária, com o ruído de casa habitual; carregar
Enter, dizer a frase, carregar Enter. Não é preciso ler a pontuação.

| id | caso | frase | intenção | projeto | ativação |
|----|------|-------|----------|---------|----------|
| pt-01 | a | boas jarvis, diz ao <projeto-1> para acrescentar testes ao módulo de configuração | ditar_prompt | <projeto-1> | sim |
| pt-02 | a | no <projeto-2>, corrige o erro de arranque que aparece quando falta o ficheiro de configuração | ditar_prompt | <projeto-2> | não |
| pt-03 | a | boas jarvis, pede ao <projeto-1> para explicar porque é que o teste de latência está a falhar | ditar_prompt | <projeto-1> | sim |
| pt-04 | a | manda ao <projeto-2>: revê o README e encurta a secção de instalação | ditar_prompt | <projeto-2> | não |
| pt-05 | a | boas jarvis, no <projeto-1> muda a mensagem de erro para dizer qual é o ficheiro em falta | ditar_prompt | <projeto-1> | sim |
| pt-06 | a | diz ao <projeto-2> que o botão de guardar tem de ficar desativado enquanto grava | ditar_prompt | <projeto-2> | não |
| pt-07 | a | boas jarvis, pergunta ao <projeto-1> quanto tempo demora a suite de testes | ditar_prompt | <projeto-1> | sim |
| pt-08 | a | no <projeto-2>, acrescenta um registo com a hora de cada pedido | ditar_prompt | <projeto-2> | não |
| pt-09 | a | boas jarvis, escreve um prompt para o <projeto-1>: procura código duplicado nos scripts e propõe como juntar | ditar_prompt | <projeto-1> | sim |
| pt-10 | a | diz ao claude para rever as alterações que fiz hoje | ditar_prompt | — | não |
| pt-11 | a | boas jarvis, pede ao <projeto-2> um resumo do que mudou esta semana | ditar_prompt | <projeto-2> | sim |
| pt-12 | a | dita para o claude: quero uma lista das tarefas que faltam | ditar_prompt | — | não |
| pt-13 | b | boas jarvis, como está o run do <projeto-1> | estado | <projeto-1> | sim |
| pt-14 | b | boas jarvis, qual é o estado do <projeto-2> | estado | <projeto-2> | sim |
| pt-15 | b | boas jarvis, lê o relatório do <projeto-2> | ler_relatorio | <projeto-2> | sim |
| pt-16 | b | o <projeto-1> ainda está a trabalhar | estado | <projeto-1> | não |
| pt-17 | b | boas jarvis, lê-me o resumo do último run do <projeto-1> | ler_relatorio | <projeto-1> | sim |
| pt-18 | b | em que task vai o <projeto-2> | estado | <projeto-2> | não |
| pt-19 | b | boas jarvis, o run do <projeto-2> está bloqueado | estado | <projeto-2> | sim |
| pt-20 | b | lê o relatório mais recente do <projeto-1> | ler_relatorio | <projeto-1> | não |
| pt-21 | c | boas jarvis, lança um run no <projeto-1> | lancar_run | <projeto-1> | sim |
| pt-22 | c | retoma o run do <projeto-2> | retomar_run | <projeto-2> | não |
| pt-23 | c | boas jarvis, para o run do <projeto-1> | parar_run | <projeto-1> | sim |
| pt-24 | c | abre uma sessão do claude no <projeto-2> | lancar_run | <projeto-2> | não |
| pt-25 | c | boas jarvis, retoma a sessão do <projeto-1> | retomar_run | <projeto-1> | sim |
| pt-26 | c | começa um run novo no <projeto-2> para corrigir os testes | lancar_run | <projeto-2> | não |
| pt-27 | c | boas jarvis, interrompe o run do <projeto-2> | parar_run | <projeto-2> | sim |
| pt-28 | c | continua o run do <projeto-1> | retomar_run | <projeto-1> | não |
| pt-29 | local | boas jarvis, que horas são | horas | — | sim |
| pt-30 | local | boas jarvis, diz-me as horas | horas | — | sim |
| pt-31 | local | boas jarvis, abre o editor no <projeto-1> | abrir_editor | <projeto-1> | sim |
| pt-32 | local | abre o vs code no <projeto-2> | abrir_editor | <projeto-2> | não |
| pt-33 | local | boas jarvis, abre a pasta do <projeto-2> | abrir_pasta | <projeto-2> | sim |
| pt-34 | local | abre a pasta do <projeto-1> | abrir_pasta | <projeto-1> | não |
| pt-35 | local | cala-te | calar | — | não |
| pt-36 | local | boas jarvis, dorme | dormir | — | sim |
| pt-37 | local | boas jarvis, acorda | acordar | — | sim |
| pt-38 | local | boas jarvis, para de falar | calar | — | sim |
| pt-39 | confirmacao | sim, envia | confirmar | — | não |
| pt-40 | confirmacao | não, muda testes para documentação | corrigir | — | não |
| pt-41 | confirmacao | acrescenta que é urgente | acrescentar | — | não |
| pt-42 | confirmacao | cancela | cancelar | — | não |
| pt-43 | confirmacao | sim | confirmar | — | não |
| pt-44 | confirmacao | não, muda o <projeto-1> para o <projeto-2> | corrigir | <projeto-2> | não |
