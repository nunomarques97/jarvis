# jarvis: voice control for Claude Code sessions

A local voice assistant for Windows. You hold a key (or say the wake word), speak, and jarvis transcribes what you said on your own PC. Claude, running as jarvis's conversation brain, talks with you from start to finish: it answers questions, keeps the context of the conversation, and uses jarvis's functions as tools to read the state of your projects. Anything with an effect is read back to you first, and only after you say "yes" to jarvis does it act: a prompt goes into the Claude Code session of the right project, as if you had typed it, a FORJA run starts or stops, or a fact is saved. Replies are spoken out loud. The projects it knows are the ones listed in your local configuration (`config.toml`) plus the Git repositories it finds in your repositories folder (see "Projects" below).

## Starting it

```powershell
.venv\Scripts\python -m jarvis
```

This starts the resident process. It warms up the speech recognition, the voice and the conversation brain (a persistent `claude` process) in parallel and prints `JARVIS PRONTO em <n> s` when it is ready (the target is 30 seconds or less), together with the free GPU memory before and after. The local language model is not loaded at startup while the brain is on: it is only loaded if the brain becomes unavailable (see "When Claude is not available" below), which leaves the GPU free for other programs. A status line shows what jarvis is doing at every moment: `A OUVIR` (listening), `A PENSAR` (thinking), `À ESPERA DE CONFIRMAÇÃO` (waiting for your yes), `A OUVIR-TE` (listening without the wake word, with the seconds left), `A FALAR` (speaking) and `A DORMIR` (asleep). The same state is shown in the window title.

To start it with a double click, create the shortcut once:

```powershell
.venv\Scripts\python scripts/criar_atalho.py
```

It writes `jarvis.lnk` (and a `jarvis.cmd` fallback) to `.jarvis/atalho/`, a folder ignored by Git because the shortcut holds this machine's paths. Copy the shortcut to the desktop or pin it to the Start menu if you like.

Other options:

```powershell
.venv\Scripts\python -m jarvis --sem-ativacao        # push-to-talk key only, no wake word
.venv\Scripts\python -m jarvis --com-som             # short beep when listening starts and ends
.venv\Scripts\python -m jarvis --sem-voz             # replies on screen only
.venv\Scripts\python -m jarvis --sem-bolinha         # no status orb window
.venv\Scripts\python -m jarvis --wav a.wav b.wav     # WAV files instead of the microphone
```

Ctrl+C, closing the window or saying "cala-te" / "be quiet" silences the voice at once.

**Only one jarvis at a time.** Two jarvis windows would both hear and answer the same sentence, so jarvis with the microphone refuses to start while another one is running. It says so on screen ("Another jarvis is already running (PID ...). Close it first, then start jarvis again.") and exits before loading anything. Close the other jarvis window and start it again. The check uses `logs/jarvis.lock`, which holds the process ID of the running jarvis and is removed when it closes. If a jarvis crashed and left the file behind, the next start notices that the process is gone, replaces the file and says so in the log. `--wav` and `--autoteste` do not use the lock.

**Sleep and wake up.** "dorme" / "go to sleep" puts jarvis to sleep: while asleep it interprets and sends nothing. To wake it, say "acorda" / "wake up" (with the key or the wake word), or say the wake word followed by a short wake-up ("hey jarvis, wake up", "hey jarvis, awake", or just "hey jarvis" and then stay quiet for a few seconds). The speech engine often hears only "Up." after the wake word, and that wakes jarvis too, but only when the wake word was detected with a score at or above `[ouvido] limiar_ativacao`. If you say the wake word followed by a normal request while jarvis is asleep ("hey jarvis, what time is it?"), it wakes up and handles that request at once, without a separate "I'm awake" and without asking you to repeat it; anything with an effect still gets its recap and waits for your "yes", and a money or trading request is refused as always. This only happens when the wake word was detected at or above `[ouvido] limiar_ativacao`: below it, with the push-to-talk key (unless you say "wake up") or with speech around you, requests are still ignored while asleep. "hey jarvis, go to sleep" or "hey jarvis, be quiet" does not wake it; it says once that it is asleep and how to wake it.

## Talking to it

**Push-to-talk key.** Hold the key, speak, release. The default key is right Ctrl; `[ouvido] tecla` in `config.toml` can change it (right Alt, right Shift, Scroll Lock or F13 to F24). This is the main way to talk to jarvis: no false wake-ups and no waiting for the end of speech.

**Wake word, hands-free.** Say the wake word followed by the request; jarvis stops listening when you stop talking. The language is set by `[ouvido] lingua`:

- `"en"`: the wake word is "hey jarvis" (pre-trained openWakeWord model).
- `"pt"`: the wake word is "boas jarvis". Its model is trained locally with your own recordings (`scripts/treinar_ativacao.py`). Until `models/openwakeword/boas_jarvis.onnx` exists, hands-free listening is off and the key still works.

`[ouvido] limiar_ativacao` sets how sure the detector must be before it wakes up.

When the wake word itself is misheard and ends up at the start of the transcript ("AJar is", "A Jarvis", "Hey Jar", "Jarvis is", "Ajarvis"), jarvis removes it before interpreting the request, from a fixed list of known forms. Only these forms count, a sentence is never emptied by it, and a question after a comma keeps its "is" ("Jarvis, is atlas blocked?").

**Keep talking after a reply.** Once jarvis has answered, you do not need the key or the wake word again until you stop: see "Conversation mode" below.

**A conversation with Claude.** Everything you say goes to the conversation brain as the next turn of one running conversation, so a short follow-up is understood from what came before (ask for help planning a weekend in Lisbon, then just "and on Sunday?"). Small talk, questions, plans and requests about your projects all go the same way; Claude answers in short spoken sentences, usually one to three, and asks one question back when something is unclear. The first sentence is spoken as soon as it is written, while the rest is still coming (see "General questions" below).

**What stays on your PC.** A few things never go to Claude and are answered by fixed rules, so they are fast and use no quota: be quiet, go to sleep and wake up, the time and the date, your answer to a recap ("yes", "abort", a correction), "that's all" to end the conversation, noise or courtesy words heard in conversation mode, and the refusal of any money or trading request.

**What the brain can do.** Besides searching and reading the web, Claude can use only jarvis's own tools: the time and date, your project list, a project's status and latest report, your saved facts and the project notices that are waiting. Tools that would change something (send a text to a project's Claude Code session, launch, stop or resume a FORJA run, save or forget a fact) never act by themselves: they only prepare the recap below, and jarvis says it. Only your spoken "yes" to jarvis carries it out; a "yes" written by Claude, found on a web page or inside a report confirms nothing. One recap at a time. The brain has no shell, no access to your project files and no credentials. jarvis tells you the outcome itself, and it also goes back to Claude as data with your next sentence (sent, done, cancelled, expired, failed), so the conversation knows what happened.

**When Claude is not available.** If the brain cannot start (no `claude` command), you are not logged in, your subscription quota runs out, or it fails twice in a row (for example without internet), jarvis says once "I can't reach Claude right now, so I'll keep to the basics for a bit." and falls back to the local language model in Ollama (`qwen3:8b`) for `[cerebro] reintentar_s` seconds (120 by default), then tries the brain again. In that fallback project control, the memory commands, the time and date, sleep and wake up work as before, short social phrases ("how are you", "who are you", "tudo bem") get a short local reply, and a general question gets "I can't answer general questions without Claude." instead of an answer. Nothing leaves the PC in the fallback before your "yes". `[cerebro] ativo = false` in `config.toml` turns the brain off and keeps jarvis on the local model all the time.

## Confirming, correcting, cancelling

Everything that has an effect (sending a prompt to Claude Code, opening the editor or a folder, launching, resuming or stopping a FORJA run) is first recapped out loud in one short sentence that ends in a light question, for example "Add tests to the login page, for atlas - send it?" ("Corrige o teste do login, para o atlas - envio?"). A long prompt is said by its topic in a few words, followed by "the full text is on screen"; the exact text that will be sent is always shown on screen. Then you answer straight away, without the wake word: as soon as the recap finishes being spoken, jarvis listens for your reply until the end of speech and the status line shows `À ESPERA DE CONFIRMAÇÃO — A OUVIR A RESPOSTA`. The key and the wake word also work. Without voice activity detection (`--sem-ativacao`) only the key does.

| You say (pt) | You say (en) | What happens |
|---|---|---|
| "sim", "envia", "manda", "confirma" (also with "por favor") | "yes", "yeah", "yep", "sure", "send it", "go ahead", "do it", "confirm", or clear combinations such as "Go, yes.", "yes please", "yes, send it", "yeah go ahead", "ok yes" | the recapped prompt is sent, exactly as shown |
| "não, muda testes para documentação" | "no, change tests to documentation" | jarvis corrects the prompt and recaps again |
| "acrescenta que é urgente" | "add that it is urgent" | jarvis adds it and recaps again |
| "aborta" (or "cancela") | "abort" (or "cancel") | nothing is sent |

"abort" is the main word to cancel and "cancel" still works. Because cancelling never sends anything, a misheard cancel also counts ("Can't sell it.", "Uh castle.", "a board", "aboard"), and it is recognised before any correction, so it is never mistaken for a financial request. Sending is exact: the whole answer must be made only of yes words and courtesy words ("ok", "please"), so anything close but different ("yes but ...", "go to atlas") is never taken as a yes, and an answer that mixes yes and abort ("yes abort") sends nothing and jarvis asks again: "Say yes to send, or abort." Asking for a project's status or report ("What is the status of atlas?", "read the report for atlas") only reads, so it runs straight away without a recap; if you did not name the project, jarvis uses the last project you used (see below) and the answer says which one ("On atlas: ..."), and with no recent project it asks which one and runs as soon as you say it.

While the brain is on, a correction is applied by fixed rules only, without the local language model: swapping one word or phrase that appears once in the prompt, switching to another project, or adding a sentence at the end. Anything else keeps the request as it was and jarvis says it could not apply the correction; say "abort" and ask again in your own words. In the fallback without the brain, corrections use the local language model as before.

**The last project used.** A dictation that names no project, said within `[interprete] ultimo_projeto_min` minutes (10 by default; 0 turns this off) of the last request that ran on a project, is taken as being for that project, and the recap says so: "Add tests to the login page, still for atlas - send it?". It is still sent only after your "yes". To send it elsewhere, answer "no, for forja" ("não, para o forja"): jarvis switches the project and recaps again, still without sending. "no" or "abort" cancels. Only dictations and status or report requests assume the project; opening the editor or a folder and launching, resuming or stopping a run always ask which project. A sentence that names a project, even misheard, or offers a choice ("in atlas or forja") is never replaced by the last one, and a cancelled or failed request does not count as used. With no answer within `[interprete] confirmacao_s` seconds (30 by default, counted from the moment the recap finishes being spoken) the request is cancelled without sending anything. Noise or an "uh" on its own does not count as an answer. Telling the time or date, "cala-te" (be quiet), "dorme" (go to sleep) and "acorda" (wake up) run straight away, because they only read or silence.

The first prompt to a project opens that project's Claude Code session in a new window, with the jarvis channel attached; accept Claude Code's notice in that window once and the prompt follows. If the channel cannot be used, the prompt goes to a headless session in the project folder, and the command to resume that session is shown on screen. Nothing is written into your projects or into `~/.claude`: the files jarvis generates stay in `.jarvis/`, ignored by Git.

## General questions

A question that is not about a project ("what's the temperature in Porto today", "what football games are on today", "how do you make pancakes") is answered by the brain straight away, without a recap, because it only reads; Claude searches the web when it needs to (at most two searches or page reads per exchange). The brain is one persistent Claude Code process started at startup and kept warm, driven over `--input-format stream-json --output-format stream-json` with your normal Claude subscription login (`--bare` is not used because it needs a paid API key). It runs in a neutral folder under the system temp folder, outside jarvis and every project, with only the web tools and jarvis's own tools, no shell, no file editing, no hooks, skills or project `CLAUDE.md` files, and nothing saved to disk. Your sentences, the date, your location, your facts and summaries go to it only through its input, never on its command line.

The answer streams back, and each sentence is spoken as soon as it is complete and has passed the same spoken-reply filter as project replies, while Claude is still writing the rest. Only when no sentence is ready after about a second does jarvis first say a short, varied acknowledgement ("One sec.", "Let me see."); a fast answer gets none. If a web search runs past about 8 seconds, it says a second one ("Still looking."). A list of sources, references or links ("Sources:", "References:", "Links:", "Fontes:", "Referências:", also as a Markdown heading or in bold) is never read aloud: the voice stops before it, and the full answer with its sources stays on screen and in the log. Once a sentence has been spoken it is never taken back: if the rest of the answer would change what the filter keeps, jarvis just stops there. "cala-te", Ctrl+C, "dorme" or a new request cancel the turn mid-answer and nothing more is said; if the answer times out or breaks after jarvis has started speaking, it says one short sentence ("Sorry, I lost the rest of that.") instead. The log records, for each sentence, how long it took from the end of your voice to the first sentence being ready and to the first sentence being heard, whether the web was used, and the tokens of the exchange. Your default location is in the optional `[perguntas]` table of `config.toml`, and the model, the time limit and the other brain settings are in `[cerebro]` (see `config.exemplo.toml`). Asset prices, quotes and trading advice are refused: a money or trading request is never sent to Claude, and a reply from Claude that contains one is replaced by the refusal before it is spoken.

**What it costs.** Every sentence that goes to the brain uses your Claude subscription quota, and nothing else: no API key and no other paid service. A typical exchange without a web search reads about 5,000 to 10,000 input tokens, most of them from Claude's prompt cache, and writes 30 to 150; a web search adds a few thousand. jarvis enforces a ceiling whatever the model does: at most 4 model calls per exchange, each with at most 32,000 tokens of context, so at most 128,000 input tokens per exchange, at most 2 web uses, and a limit on the length and time of the reply. If usage credits are turned on for your subscription, Claude Code can keep working past the plan's limit and that is paid; keep them off (or set a spend limit) to keep the extra cost at zero. To measure latency and tokens per exchange on your PC with fictitious data only (this uses your quota):
```
.venv\Scripts\python scripts/medir_cerebro.py --com-claude
```
It compares `claude-haiku-4-5` (the default) with `sonnet`; the report goes to `docs/forja/evidence/`, which Git ignores.

## Memory

jarvis remembers in two ways, both with fixed ceilings so every exchange has a guaranteed maximum size (and quota cost).

**The conversation.** The running conversation lives only in the memory of the brain process and of jarvis, and is never written to disk. When it grows past `[cerebro] contexto_max_tokens` (16,000 tokens by default), or after `[cerebro] inativo_min` minutes without talking (30 by default), the next sentence starts a fresh session seeded with a summary of at most 120 words and your saved facts; older detail is gone, as with a person.

**Facts about you.** "Remember that my favourite team is Benfica" ("lembra-te que ...") reads the fact back ("Remember: My favourite team is Benfica. Save it?") and saves it only after your "yes"; "abort", "no" or no answer within the confirmation time saves nothing. "Forget that ..." ("esquece que ...") finds the most similar fact and deletes it only after your "yes"; if no fact is similar, nothing is deleted. "What do you remember about me?" ("o que te lembras de mim?") is answered locally, without Claude: a short spoken summary, with the full list on screen. The facts are kept in `memoria/factos.json` in the jarvis folder, a local file that Git ignores; a damaged file never stops jarvis, it is noted in the log and set aside as `factos.json.estragado` on the next save. The notebook holds at most 50 facts and 4000 characters (each fact is cut to 300 characters); when it is full, jarvis says so and saves nothing until you ask it to forget a fact. jarvis never keeps secrets (passwords, PINs, codes, tokens, API keys) or financial data (bank accounts, IBAN, card numbers, balances, salary, holdings, amounts of money, and anything the money and trading rule catches): those sentences are refused before the read-back and never written. With the brain on, these requests go through Claude, which uses the fact tools: saving or forgetting still needs the read-back and your spoken "yes", and secrets and financial data are still refused before any recap. In the fallback without the brain they are fixed rules handled on your PC and never go to the local language model.

**What is sent to Claude.** Your facts go to the brain at the start of every session, and through the `factos_guardados` tool, never on its command line, in clearly marked sections that the model is told to treat as data and never as instructions. The limits are in the optional `[memoria]` table of `config.toml` (see `config.exemplo.toml`): `factos` (1 to 50) and `caracteres` (200 to 4000). They can only be lowered, never raised above those ceilings.

## Conversation and notices

**Hands-free conversation.** When Claude's reply ends with a question you heard, jarvis listens for 8 seconds without the wake word (the key works too). Your answer keeps your words, without hesitations ("uh", "um") or an address to jarvis ("Jarvis, answer no, not right now" sends "No, not right now."). A short answer of up to 5 words ("Yes.", "the first one") is sent at once and jarvis says "Sent." / "Enviado."; a longer one goes into a quick recap, for example "Reply to Claude in atlas: keep the newer file and delete the older one - send it?", and is sent only after your "yes". This window is the only place anything is sent without a recap, and a money or trading request is refused there too. Saying "sai da conversa" / "exit the conversation", or 8 seconds of silence, closes the window without sending anything.

**Conversation mode.** After jarvis says something to you, just keep talking: you do not need "hey jarvis" or the key. Every time jarvis answers, it listens again, so a conversation can go on for as many exchanges as you like. It stops listening when you stay quiet for 15 seconds after its last reply, or when you say "that's all", "thanks, that's it" or "nothing else" ("é tudo", "mais nada").

- **How you know it is listening.** The orb shows it is listening and the status line shows `A OUVIR-TE | sem palavra de ativacao, N s`. A soft sound plays when the conversation starts (not again after every reply) and a different sound when it ends. It never listens while it is speaking, so it does not hear itself.
- **What it ignores**, so people talking around you do not set it off: silence, noise, an "uh" and only courtesy words ("thanks", "okay") are dropped on your PC. Anything else heard without the wake word goes to the brain marked as such, and Claude answers nothing when it was clearly not meant for jarvis. In the fallback without the brain, a sentence that asks nothing of it and a general question that is not put to it as a question are ignored too (to count, a question has to start like one, such as "what", "how", "tell me" or "qual", end with a question mark in the transcript, or start with "Jarvis"). Ignored speech gets no reply and does not change the time left.
- **What stays the same.** Anything with an effect still gets its recap and waits for your "yes", and money or trading requests are refused. The key and "hey jarvis" always work; "hey jarvis" said during the conversation is simply removed from what you said. After a recap you have the 30 seconds to answer, and after a question from Claude the conversation window above applies; only one of them listens at a time. While jarvis is asleep it never listens like this, and spoken notices wait until the conversation has ended.
- **Settings**, in the optional `[escuta]` table of `config.toml` (see `config.exemplo.toml`): `seguimento_s` is the silence in seconds that ends the conversation (3 to 30, 15 by default), `sons = false` turns the sounds off and `volume` sets how loud they are. With `--wav` the sounds only play with `--com-som`.

**Spoken notices.** The sessions jarvis opens start with `--settings .jarvis/sessoes/<project>/settings.json`, which adds two Claude Code hooks (see `config/hooks-jarvis.exemplo.json`): `Stop`, and `Notification` for `idle_prompt` and `permission_prompt`. The hook sends jarvis only the kind of event, over the same authenticated local connection as the channel, never the message or the transcript. jarvis then says "atlas acabou" / "atlas is done" or "atlas está à espera de ti" / "atlas is waiting for you". With `[forja]` configured it also checks the FORJA run of each project every 30 seconds and says when a run finishes, fails or gets blocked. Notices, and a project's reply that arrives in the middle of a conversation, go into a short queue with no duplicates and an expiry, and are never spoken over the conversation: not over your voice, a reply, a pending recap, a brain turn or an open conversation window. While you are talking, the brain gets the waiting notices once as data with your next sentence (and through its `avisos_pendentes` tool) and mentions them when it fits or when you ask; a late project reply is never read out in the middle, its full text stays on screen and in the log. The notices left are said together in one short sentence after `[cerebro] espera_dos_avisos_s` seconds (20 by default) without conversation. At most one per session per minute; "cala-te" drops them and, while asleep, they only go to the log.

## The status orb

While jarvis runs with the microphone, a small borderless window stays on top of the other windows in a corner of the screen. It shows an orb that follows what jarvis is doing, in the style of a voice mode:

| Orb | Meaning |
| --- | --- |
| Small slate dot, breathing slowly | Waiting for the wake word or the key |
| Blue disc with a ring, growing with your microphone level | Listening to you (also during a conversation with Claude and in conversation mode after jarvis speaks) |
| Violet orb with three dots | Thinking |
| Teal orb with three bars, moving with the voice | Speaking |
| Amber orb with `?` | Waiting for your yes, also while it hears the answer |
| Dim crescent moon | Asleep |
| Red orb with `!` | Something failed (shown for a few seconds) |

Below the orb, a short caption shows the phrase jarvis heard and, during the recap, the text it will send. Click the orb to silence jarvis, the same as saying "be quiet": the voice stops and jarvis keeps running. Drag it to move it; the position is saved in `.jarvis/bolinha.json` (ignored by Git). Colours follow the Windows light or dark theme, and with animation effects turned off in Windows the orb stays still. The design contract is in [docs/DESIGN.md](docs/DESIGN.md).

The orb runs as a separate process, so it never slows down listening or speaking. If it cannot start or it closes, jarvis writes one line in the log and carries on without it. `--sem-bolinha` starts jarvis without it, and `--wav` never opens it.

## The voice

English replies are spoken by a male British Kokoro voice, `bm_fable`, by default. To hear the alternatives, write one sample WAV per voice to `audio/amostras-voz/` (ignored by Git); nothing plays unless you add `--com-som`:

```
.venv\Scripts\python scripts/amostras_voz.py --com-som
```

To switch, set `nome` in the optional `[voz]` table of `config.toml` to one of `bm_george`, `bm_lewis`, `bm_daniel`, `bm_fable` (British) or `af_heart` (American), and restart jarvis. Why `bm_fable` is the default, with the latency numbers, is in [docs/MODELOS.md](docs/MODELOS.md).

## Adapting to your accent

Transcription can be adapted to your voice in English. There are two parts, and both stay off until the measurement shows they help. **Boosting** favours jarvis' command phrases and your project names while the words are recognised. The **correction lexicon** learns your recurring confusions (for example "castle" for "cancel") from your training recordings. Everything learned from your voice stays in ignored folders: recordings in `recordings/`, the lexicon in `models/adaptacao/`, and the measurement in `docs/forja/evidence/`.

Only your own recordings count. Nothing here uses a synthetic voice. The steps:

1. **Record the pilot.** This command starts with 3 training phrases:
   ```
   .venv\Scripts\python scripts/gravar_voz.py --lingua en --treino
   ```
   Press Enter to start each phrase and Enter to stop. The training phrases go to `recordings/treino-en/`, a separate folder, so they never reach the test set.
2. **Check the pilot.** The recorder checks that each pilot phrase contains real speech. If one is silent, it stops and points you to `scripts/gravar_voz.py --verificar` to check the microphone. If the pilot passes, play the three WAV files it names in `recordings/treino-en/`, then type `yes` if your voice sounds right.
3. **Record the rest.** After your `yes`, the recorder asks for the remaining phrases. You can stop at any time; running the same command again resumes where you stopped.
4. **Run the adaptation.** This learns the lexicon from the training side only:
   ```
   .venv\Scripts\python scripts/adaptar_sotaque.py
   ```
5. **Measure.** This command splits your evaluation recordings in `recordings/en/` into a training half and a test half. The split is fixed and stratified by the script's case. It learns the lexicon again from the training side and then measures on the test side only, in one run: no adaptation, boosting only, lexicon only, and both.
   ```
   .venv\Scripts\python scripts/adaptar_sotaque.py --medir
   ```
   The report in `docs/forja/evidence/` has the word error rate, the preserved intent and the p50 transcription latency. It also lists the ids of both halves and prints the `[adaptacao]` settings for the best variant. A variant counts only if it lowers the word error rate, preserves more intents than no adaptation, and keeps p50 latency within 1.2x. Copy those settings into `config.toml` and restart jarvis.

The current result, and why the adaptation is off by default, is in [docs/MODELOS.md](docs/MODELOS.md).

## How it works

1. **Listening**: the push-to-talk key, or openWakeWord followed by voice activity detection (webrtcvad).
2. **Transcription**: NVIDIA Parakeet TDT 0.6B v3 (ONNX, on the CPU by default), loaded once and kept warm. faster-whisper can be chosen instead with `[ouvido] motor`.
3. **Understanding**: the conversation brain, one persistent Claude Code process on your subscription (`claude-haiku-4-5` by default, see "A conversation with Claude" and "General questions"), after a few fixed local rules (see "What stays on your PC"). Without the brain, a local language model in Ollama (`qwen3:8b`, falling back to `qwen3:4b` when GPU memory is short) returns the intent, the project and, for a dictation, a clear rewritten prompt. It never adds requests of its own, and it never picks a project from the wording: a project is either named, or, for a dictation or a status request that names none, it is the last project you used in the previous few minutes (see "The last project used"). That assumed project is always said in the recap or in the answer, and nothing is sent to it without your "yes". A rewrite may fix a known recognition slip from a small fixed list ("what tests" or "attest" for "add tests"); any other swapped word makes jarvis fall back to your own words, without the wake word, the address to the project ("tell atlas to") or hesitations.
4. **Confirmation**: the recap above; nothing is carried out before your spoken "yes". Talking with the brain and its web searches have no effect, so they need no "yes".
5. **Action**: a local action, or the prompt into the project's Claude Code session.
6. **Spoken reply**: a resident voice that starts speaking while it is still synthesising (Kokoro, male British voice by default, for English when its model files are present, Piper `pt_PT-tugão-medium` for European Portuguese). Claude's replies are spoken as they are, without naming the source; the source label ("Claude says:") and the full reply stay on screen and in the log. Code, tool calls and file paths in Claude's replies are never read out; when nothing is left to say, jarvis says in one short sentence that the full reply is on screen.

Every sentence is logged with timestamps for each stage in `logs/jarvis-<date>.log` (ignored by Git), including the time from the end of your speech to the first sign of life and to the start of the spoken reply. The first reply to each sentence also logs where that time went, from the last voiced audio chunk: the end-of-turn wait, speech recognition, the interpreter (with `origem=regra` when a rule answered, or `origem=llm`), the rest of the decision, and the voice up to its first audio. With the brain, the log line of each sentence says `intencao=cerebro` and adds the brain's own line (web search, tokens, context size, first text). In the fallback, common, unambiguous requests (the time or date, the status or report of a project you name, be quiet, sleep, wake up) are answered by rules without waiting for the local LLM.

To measure the whole chain from WAV files, with a fake channel and without playing any sound:

```powershell
.venv\Scripts\python scripts/medir_ponta_a_ponta.py --verificar
```

It fails if "what time is it" takes more than 1.2 s (median) or 2.0 s (95th percentile) from the end of speech to the start of the reply, if a dictation takes more than 2.5 s / 4.0 s to the start of the recap, if the first sign of life takes more than 1.0 s, or if startup takes more than 30 s. The test WAVs are made by the local voice and only measure time; recognition accuracy has to be measured with your own recordings.

## Roadmap

Planned work, such as talking to jarvis in European Portuguese, is in [docs/ROADMAP.md](docs/ROADMAP.md). The plan to make conversation feel natural, with its research and targets, is in [docs/NATURALNESS.md](docs/NATURALNESS.md). How the conversation brain was designed and built, where Claude talks with you from start to finish and uses jarvis's functions as tools, is in [docs/BRAIN.md](docs/BRAIN.md).

## Principles

- Listening, transcription and the voice run locally; no audio leaves the PC. The conversation goes to Claude on your existing subscription quota: no API keys and nothing else that costs money.
- No buy or sell orders by voice, now or later. An AI never places orders, and voice is not authorisation to spend money.
- Voice never approves Claude Code tool permissions. The brain can only use the tools listed above, and anything with an effect needs your spoken "yes" to jarvis, checked by jarvis's own code, never by the model.
- Nothing is installed into other projects from here.
- No conversations, transcripts, audio or local tool state in commits. Recordings, transcripts and logs are in `.gitignore`.
- No secrets, tokens, `.env` files or personal paths in the code or the docs. Personal configuration lives in ignored files, with a committed example.
- Tests never play sound unless run with `--com-som`.

## Setup

From the repository root, in a virtual environment inside the repository:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -U pip
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python scripts/verificar_ambiente.py
```

Copy `config.exemplo.toml` to `config.toml` and fill in your microphone name and your projects. `config.toml` is ignored by Git. The models used are listed, with source URLs and checksums, in [docs/MODELOS.md](docs/MODELOS.md). The brain needs Claude Code installed and logged in with your Claude subscription (the `claude` command). The local fallback model is installed in Ollama with `ollama pull qwen3:8b` (and `qwen3:4b` as its own fallback).

### Projects

jarvis knows the projects in `[[projetos]]` and also finds your Git repositories by itself, so you do not have to list each one by hand. At start-up it looks at the folders directly inside `Desktop/Repositorios` in your home folder: every folder with a `.git` becomes a project named after the folder. Only direct children count, never deeper folders. A folder without `.git`, a folder whose name cannot be a project name, a junction or symbolic link that points outside that folder, and a folder Windows does not let jarvis read are skipped, with one line in the log each. A missing folder is not an error. If `config.toml` cannot be loaded, jarvis starts without any projects and does not look for repositories either. A project in `[[projetos]]` always wins over a found folder with the same name. Found projects are recognised by name exactly like the configured ones, including the sound-alike matching, and are part of the accent-adaptation phrases when boosting is on.

To look somewhere else, or in several places, add the optional `[descoberta]` table to `config.toml`; an empty list turns discovery off:

```toml
[descoberta]
pastas = ["D:/path/to/repositories", "~/Code"]   # [] turns discovery off
```

When a request needs a project, you did not say one and jarvis cannot use the last project you used (see "The last project used"), it asks one short question that names up to four projects, the ones you used most recently first (then the ones with the most recent Git activity), for example "Which project is it for: jarvis, forja, chamora or another one?". The full list is on screen. Say the name (it can be a found repository that is not in `config.toml`) and the request goes on to its recap; nothing is sent without "yes".

Tests:

```powershell
.venv\Scripts\python -m unittest discover -s tests
```

## Hardware

Developed and tested on Windows 11 with an RTX 5060 Ti (16 GB VRAM), an i5-14400F and 32 GB of RAM, with Ollama installed locally.

## License

AGPL-3.0. Copyright (c) 2026 Nuno Marques. See [LICENSE](LICENSE).
