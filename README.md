# jarvis: voice control for Claude Code sessions

A local voice assistant for Windows. You hold a key (or say the wake word), speak, and jarvis transcribes the request on your own PC, works out what you want and which project it is for, and tells you back what it understood. Only after you say "yes" does it act: a dictated prompt goes into the Claude Code session of the right project, as if you had typed it, and local actions run on the PC. Replies are spoken out loud. The projects it knows are the ones listed in your local configuration (`config.toml`) plus the Git repositories it finds in your repositories folder (see "Projects" below).

## Starting it

```powershell
.venv\Scripts\python -m jarvis
```

This starts the resident process. It warms up the speech recognition, the voice and the local language model in parallel and prints `JARVIS PRONTO em <n> s` when it is ready (the target is 30 seconds or less), together with the free GPU memory before and after. A status line shows what jarvis is doing at every moment: `A OUVIR` (listening), `A PENSAR` (thinking), `À ESPERA DE CONFIRMAÇÃO` (waiting for your yes), `A OUVIR-TE` (listening without the wake word, with the seconds left), `A FALAR` (speaking) and `A DORMIR` (asleep). The same state is shown in the window title.

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

**Small talk.** A short social phrase addressed to jarvis ("how are you", "how are you doing today", "what's up", "who are you", "are you there", "tudo bem", "como estás", "quem és tu", "estás aí") gets one short friendly reply straight away, in the configured language. It is answered locally from a fixed list: it never goes to the local model or to Claude, and uses no quota. A real question ("how do you make pancakes", "what's up with the build in atlas") is not small talk and is handled as usual.

## Confirming, correcting, cancelling

Everything that has an effect (sending a prompt to Claude Code, opening the editor or a folder, launching, resuming or stopping a FORJA run) is first recapped out loud in one short sentence that ends in a light question, for example "Add tests to the login page, for atlas - send it?" ("Corrige o teste do login, para o atlas - envio?"). A long prompt is said by its topic in a few words, followed by "the full text is on screen"; the exact text that will be sent is always shown on screen. Then you answer straight away, without the wake word: as soon as the recap finishes being spoken, jarvis listens for your reply until the end of speech and the status line shows `À ESPERA DE CONFIRMAÇÃO — A OUVIR A RESPOSTA`. The key and the wake word also work. Without voice activity detection (`--sem-ativacao`) only the key does.

| You say (pt) | You say (en) | What happens |
|---|---|---|
| "sim", "envia", "manda", "confirma" (also with "por favor") | "yes", "yeah", "yep", "sure", "send it", "go ahead", "do it", "confirm", or clear combinations such as "Go, yes.", "yes please", "yes, send it", "yeah go ahead", "ok yes" | the recapped prompt is sent, exactly as shown |
| "não, muda testes para documentação" | "no, change tests to documentation" | jarvis corrects the prompt and recaps again |
| "acrescenta que é urgente" | "add that it is urgent" | jarvis adds it and recaps again |
| "aborta" (or "cancela") | "abort" (or "cancel") | nothing is sent |

"abort" is the main word to cancel and "cancel" still works. Because cancelling never sends anything, a misheard cancel also counts ("Can't sell it.", "Uh castle.", "a board", "aboard"), and it is recognised before any correction, so it is never mistaken for a financial request. Sending is exact: the whole answer must be made only of yes words and courtesy words ("ok", "please"), so anything close but different ("yes but ...", "go to atlas") is never taken as a yes, and an answer that mixes yes and abort ("yes abort") sends nothing and jarvis asks again: "Say yes to send, or abort." Asking for a project's status or report ("What is the status of atlas?", "read the report for atlas") only reads, so it runs straight away without a recap; if you did not name the project, jarvis uses the last project you used (see below) and the answer says which one ("On atlas: ..."), and with no recent project it asks which one and runs as soon as you say it.

**The last project used.** A dictation that names no project, said within `[interprete] ultimo_projeto_min` minutes (10 by default; 0 turns this off) of the last request that ran on a project, is taken as being for that project, and the recap says so: "Add tests to the login page, still for atlas - send it?". It is still sent only after your "yes". To send it elsewhere, answer "no, for forja" ("não, para o forja"): jarvis switches the project and recaps again, still without sending. "no" or "abort" cancels. Only dictations and status or report requests assume the project; opening the editor or a folder and launching, resuming or stopping a run always ask which project. A sentence that names a project, even misheard, or offers a choice ("in atlas or forja") is never replaced by the last one, and a cancelled or failed request does not count as used. With no answer within `[interprete] confirmacao_s` seconds (30 by default, counted from the moment the recap finishes being spoken) the request is cancelled without sending anything. Noise or an "uh" on its own does not count as an answer. Telling the time or date, "cala-te" (be quiet), "dorme" (go to sleep) and "acorda" (wake up) run straight away, because they only read or silence.

The first prompt to a project opens that project's Claude Code session in a new window, with the jarvis channel attached; accept Claude Code's notice in that window once and the prompt follows. If the channel cannot be used, the prompt goes to a headless session in the project folder, and the command to resume that session is shown on screen. Nothing is written into your projects or into `~/.claude`: the files jarvis generates stay in `.jarvis/`, ignored by Git.

## General questions

A question that is not about a project or a local command ("what's the temperature in Porto today", "what football games are on today") is answered straight away, without a recap, because it only reads. jarvis passes the question to a headless Claude Code (`claude -p`) that can only search and read the web: it runs in a neutral folder under the system temp folder, outside jarvis and every project, with no shell, no file editing and no MCP servers. The answer streams back (`--output-format stream-json --include-partial-messages`; `--bare` is not used because it needs a paid API key), and each sentence is spoken as soon as it is complete and has passed the same spoken-reply filter as project replies, while Claude is still writing the rest. Only when no sentence is ready after about a second does jarvis first say a short, varied acknowledgement ("One sec.", "Let me see."); a fast answer gets none. A list of sources, references or links ("Sources:", "References:", "Links:", "Fontes:", "Referências:", also as a Markdown heading or in bold) is never read aloud: the voice stops before it, and the full answer with its sources stays on screen and in the log. Once a sentence has been spoken it is never taken back: if the rest of the answer would change what the filter keeps, jarvis just stops there. "cala-te", Ctrl+C, "dorme" or a new request stop the Claude Code process mid-answer and nothing more is said; if the answer times out or breaks after jarvis has started speaking, it says one short sentence ("Sorry, I lost the rest of that.") instead. Only a complete answer goes into the conversation memory, as it was spoken. The log records, for each question, how long it took from the end of your voice to the first sentence being ready and to the first sentence being heard. These questions use your Claude subscription quota. The model, the time limit and your default location are in the optional `[perguntas]` table of `config.toml` (see `config.exemplo.toml`). Asset prices, quotes and trading are refused before anything leaves the PC.

## Memory

jarvis remembers in two ways, both with fixed ceilings so every general question has a guaranteed maximum size (and quota cost).

**The recent conversation.** jarvis keeps your last answered general questions and the answers exactly as they were spoken, and sends them with the next general question, so "and who scored?" after a question about a match has its context. At most 10 exchanges are kept, each question and answer cut to a few hundred characters; they live only in memory, are never written to disk, and are forgotten after 30 minutes without a general question. Cancelled, refused, failed or dropped answers are never kept. Say "new conversation" or "forget this conversation" ("nova conversa", "esquece esta conversa") to forget them at once.

**Facts about you.** "Remember that my favourite team is Benfica" ("lembra-te que ...") reads the fact back ("Remember: My favourite team is Benfica. Save it?") and saves it only after your "yes"; "abort", "no" or no answer within the confirmation time saves nothing. "Forget that ..." ("esquece que ...") finds the most similar fact and deletes it only after your "yes"; if no fact is similar, nothing is deleted. "What do you remember about me?" ("o que te lembras de mim?") is answered locally, without Claude: a short spoken summary, with the full list on screen. The facts are kept in `memoria/factos.json` in the jarvis folder, a local file that Git ignores; a damaged file never stops jarvis, it is noted in the log and set aside as `factos.json.estragado` on the next save. The notebook holds at most 50 facts and 4000 characters (each fact is cut to 300 characters); when it is full, jarvis says so and saves nothing until you ask it to forget a fact. jarvis never keeps secrets (passwords, PINs, codes, tokens, API keys) or financial data (bank accounts, IBAN, card numbers, balances, salary, holdings, amounts of money, and anything the money and trading rule catches): those sentences are refused before the read-back and never written. These commands are fixed rules handled on your PC; they never go to the local language model.

**What is sent to Claude.** Your facts and the recent conversation are sent with every general question, through the same headless Claude Code (never on its command line), in clearly marked sections that the model is told to treat as data and never as instructions. The limits are in the optional `[memoria]` table of `config.toml` (see `config.exemplo.toml`): `trocas` (exchanges, 1 to 10), `expira_min` (minutes, 1 to 30), `factos` (1 to 50) and `caracteres` (200 to 4000). They can only be lowered, never raised above those ceilings.

**What memory costs.** With the default limits the memory sent with a question can never exceed about 13,800 characters of normal text (26,900 in the worst case, where every character needs escaping). In a measurement with `claude-haiku-4-5`, a question without memory read about 5,500 input tokens (mostly Claude Code's own fixed instructions), a follow-up with 5 exchanges about 5,700, and a follow-up with 10 full-size exchanges and a full notebook about 8,900; each answer took 4 to 5 seconds. To measure it yourself with fictitious data only (this asks Claude three real questions and uses your subscription quota):
```
.venv\Scripts\python scripts/medir_gasto_perguntas.py --gasta-quota
```
The report, with the tokens, cost, response times and the ceiling for your `[memoria]` limits, goes to `docs/forja/evidence/`, which Git ignores. Your location, projects and notebook are never sent by this measurement.

## Conversation and notices

**Hands-free conversation.** When Claude's reply ends with a question you heard, jarvis listens for 8 seconds without the wake word (the key works too). Your answer keeps your words, without hesitations ("uh", "um") or an address to jarvis ("Jarvis, answer no, not right now" sends "No, not right now."). A short answer of up to 5 words ("Yes.", "the first one") is sent at once and jarvis says "Sent." / "Enviado."; a longer one goes into a quick recap, for example "Reply to Claude in atlas: keep the newer file and delete the older one - send it?", and is sent only after your "yes". This window is the only place anything is sent without a recap, and a money or trading request is refused there too. Saying "sai da conversa" / "exit the conversation", or 8 seconds of silence, closes the window without sending anything.

**Conversation mode.** After jarvis says something to you, just keep talking: you do not need "hey jarvis" or the key. Every time jarvis answers, it listens again, so a conversation can go on for as many exchanges as you like. It stops listening when you stay quiet for 15 seconds after its last reply, or when you say "that's all", "thanks, that's it" or "nothing else" ("é tudo", "mais nada").

- **How you know it is listening.** The orb shows it is listening and the status line shows `A OUVIR-TE | sem palavra de ativacao, N s`. A soft sound plays when the conversation starts (not again after every reply) and a different sound when it ends. It never listens while it is speaking, so it does not hear itself.
- **What it ignores**, so people talking around you do not set it off: silence, noise, an "uh", only courtesy words ("thanks", "okay"), a sentence that asks nothing of it, and a general question that is not put to it as a question. To count, a question has to start like one ("what", "how", "tell me", "qual"), end with a question mark in the transcript, or start with "Jarvis". Ignored speech gets no reply and does not change the time left.
- **What stays the same.** Anything with an effect still gets its recap and waits for your "yes", and money or trading requests are refused. The key and "hey jarvis" always work; "hey jarvis" said during the conversation is simply removed from what you said. After a recap you have the 30 seconds to answer, and after a question from Claude the conversation window above applies; only one of them listens at a time. While jarvis is asleep it never listens like this, and spoken notices wait until the conversation has ended.
- **Settings**, in the optional `[escuta]` table of `config.toml` (see `config.exemplo.toml`): `seguimento_s` is the silence in seconds that ends the conversation (3 to 30, 15 by default), `sons = false` turns the sounds off and `volume` sets how loud they are. With `--wav` the sounds only play with `--com-som`.

**Continuing a general question.** When the spoken answer to a general question ends with a question ("... Do you want the forecast for the weekend too?"), what you say next in conversation mode continues that general question: it goes to Claude with the recent conversation, as you said it without hesitations or an address to jarvis, and never becomes a project request or a "Which project?" question. Here "Yes." or "Yeah, please." is an answer, while "Thank you.", "okay" or an "uh" on its own still change nothing, and silence closes the window as usual. These still win inside that window: be quiet, go to sleep and wake up, "new conversation" and the other memory commands, a whole local command such as "what time is it", a sentence that names one of your projects and asks something of it (it goes to its recap, or to its status), and the money or trading refusal (nothing leaves the PC). An answer that is cancelled, fails or times out never opens this continuation, and neither does one you stopped halfway. The log names the path as `continuacao da pergunta geral`.

**Spoken notices.** The sessions jarvis opens start with `--settings .jarvis/sessoes/<project>/settings.json`, which adds two Claude Code hooks (see `config/hooks-jarvis.exemplo.json`): `Stop`, and `Notification` for `idle_prompt` and `permission_prompt`. The hook sends jarvis only the kind of event, over the same authenticated local connection as the channel, never the message or the transcript. jarvis then says "atlas acabou" / "atlas is done" or "atlas está à espera de ti" / "atlas is waiting for you". With `[forja]` configured it also checks the FORJA run of each project every 30 seconds and says when a run finishes, fails or gets blocked. Notices wait their turn: never over your voice, another reply or a pending recap; at most one per session per minute; "cala-te" drops them and, while asleep, they only go to the log.

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
3. **Understanding**: a local language model in Ollama (`qwen3:8b`, falling back to `qwen3:4b` when GPU memory is short) returns the intent, the project and, for a dictation, a clear rewritten prompt. It never adds requests of its own, and it never picks a project from the wording: a project is either named, or, for a dictation or a status request that names none, it is the last project you used in the previous few minutes (see "The last project used"). That assumed project is always said in the recap or in the answer, and nothing is sent to it without your "yes". A rewrite may fix a known recognition slip from a small fixed list ("what tests" or "attest" for "add tests"); any other swapped word makes jarvis fall back to your own words, without the wake word, the address to the project ("tell atlas to") or hesitations.
4. **Confirmation**: the recap above; nothing leaves the PC before your "yes", except a general question (below).
5. **Action**: a local action, or the prompt into the project's Claude Code session.
6. **Spoken reply**: a resident voice that starts speaking while it is still synthesising (Kokoro, male British voice by default, for English when its model files are present, Piper `pt_PT-tugão-medium` for European Portuguese). Claude's replies are spoken as they are, without naming the source; the source label ("Claude says:") and the full reply stay on screen and in the log. Code, tool calls and file paths in Claude's replies are never read out; when nothing is left to say, jarvis says in one short sentence that the full reply is on screen.

Every sentence is logged with timestamps for each stage in `logs/jarvis-<date>.log` (ignored by Git), including the time from the end of your speech to the first sign of life and to the start of the spoken reply. The first reply to each sentence also logs where that time went, from the last voiced audio chunk: the end-of-turn wait, speech recognition, the interpreter (with `origem=regra` when a rule answered, or `origem=llm`), the rest of the decision, and the voice up to its first audio. Common, unambiguous requests (the time or date, the status or report of a project you name, be quiet, sleep, wake up, and clear general questions such as "what's the weather in Porto") are answered by rules without waiting for the local LLM.

To measure the whole chain from WAV files, with a fake channel and without playing any sound:

```powershell
.venv\Scripts\python scripts/medir_ponta_a_ponta.py --verificar
```

It fails if "what time is it" takes more than 1.2 s (median) or 2.0 s (95th percentile) from the end of speech to the start of the reply, if a dictation takes more than 2.5 s / 4.0 s to the start of the recap, if the first sign of life takes more than 1.0 s, or if startup takes more than 30 s. The test WAVs are made by the local voice and only measure time; recognition accuracy has to be measured with your own recordings.

## Roadmap

Planned work, such as talking to jarvis in European Portuguese, is in [docs/ROADMAP.md](docs/ROADMAP.md). The plan to make conversation feel natural, with its research and targets, is in [docs/NATURALNESS.md](docs/NATURALNESS.md).

## Principles

- Everything runs locally and for free: no paid cloud, no API keys, no audio sent off the PC.
- No buy or sell orders by voice, now or later. An AI never places orders, and voice is not authorisation to spend money.
- Voice never approves Claude Code tool permissions.
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

Copy `config.exemplo.toml` to `config.toml` and fill in your microphone name and your projects. `config.toml` is ignored by Git. The models used are listed, with source URLs and checksums, in [docs/MODELOS.md](docs/MODELOS.md). The language model is installed in Ollama with `ollama pull qwen3:8b` (and `qwen3:4b` as the fallback).

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
