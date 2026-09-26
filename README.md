# jarvis: voice control for Claude Code sessions

A local voice assistant for Windows. You hold a key (or say the wake word), speak, and jarvis transcribes the request on your own PC, works out what you want and which project it is for, and tells you back what it understood. Only after you say "yes" does it act: a dictated prompt goes into the Claude Code session of the right project, as if you had typed it, and local actions run on the PC. Replies are spoken out loud. The projects it knows are the ones listed in your local configuration (`config.toml`).

## Starting it

```powershell
.venv\Scripts\python -m jarvis
```

This starts the resident process. It warms up the speech recognition, the voice and the local language model in parallel and prints `JARVIS PRONTO em <n> s` when it is ready (the target is 30 seconds or less), together with the free GPU memory before and after. A status line shows what jarvis is doing at every moment: `A OUVIR` (listening), `A PENSAR` (thinking), `À ESPERA DE CONFIRMAÇÃO` (waiting for your yes), `A FALAR` (speaking) and `A DORMIR` (asleep). The same state is shown in the window title.

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

**Sleep and wake up.** "dorme" / "go to sleep" puts jarvis to sleep: while asleep it interprets and sends nothing. To wake it, say "acorda" / "wake up" (with the key or the wake word), or say the wake word followed by a short wake-up ("hey jarvis, wake up", "hey jarvis, awake", or just "hey jarvis" and then stay quiet for a few seconds). The speech engine often hears only "Up." after the wake word, and that wakes jarvis too, but only when the wake word was detected with a score at or above `[ouvido] limiar_ativacao`. If you give jarvis a normal request with the wake word while it is asleep, it says once that it is asleep and how to wake it; later requests in the same sleep are ignored silently.

## Talking to it

**Push-to-talk key.** Hold the key, speak, release. The default key is right Ctrl; `[ouvido] tecla` in `config.toml` can change it (right Alt, right Shift, Scroll Lock or F13 to F24). This is the main way to talk to jarvis: no false wake-ups and no waiting for the end of speech.

**Wake word, hands-free.** Say the wake word followed by the request; jarvis stops listening when you stop talking. The language is set by `[ouvido] lingua`:

- `"en"`: the wake word is "hey jarvis" (pre-trained openWakeWord model).
- `"pt"`: the wake word is "boas jarvis". Its model is trained locally with your own recordings (`scripts/treinar_ativacao.py`). Until `models/openwakeword/boas_jarvis.onnx` exists, hands-free listening is off and the key still works.

`[ouvido] limiar_ativacao` sets how sure the detector must be before it wakes up.

## Confirming, correcting, cancelling

Everything that has an effect (sending a prompt to Claude Code, opening the editor or a folder, launching, resuming or stopping a FORJA run) is first recapped out loud and on screen, for example "Para o atlas: Corrige o teste do login. Envio?". Then you answer straight away, without the wake word: as soon as the recap finishes being spoken, jarvis listens for your reply until the end of speech and the status line shows `À ESPERA DE CONFIRMAÇÃO — A OUVIR A RESPOSTA`. The key and the wake word also work. Without voice activity detection (`--sem-ativacao`) only the key does.

| You say (pt) | You say (en) | What happens |
|---|---|---|
| "sim", "envia", "manda", "confirma" (also with "por favor") | "yes", "yeah", "yep", "sure", "send it", "go ahead", "do it", "confirm", or clear combinations such as "Go, yes.", "yes please", "yes, send it", "yeah go ahead", "ok yes" | the recapped prompt is sent, exactly as shown |
| "não, muda testes para documentação" | "no, change tests to documentation" | jarvis corrects the prompt and recaps again |
| "acrescenta que é urgente" | "add that it is urgent" | jarvis adds it and recaps again |
| "aborta" (or "cancela") | "abort" (or "cancel") | nothing is sent |

"abort" is the main word to cancel and "cancel" still works. Because cancelling never sends anything, a misheard cancel also counts ("Can't sell it.", "Uh castle.", "a board", "aboard"), and it is recognised before any correction, so it is never mistaken for a financial request. Sending is exact: the whole answer must be made only of yes words and courtesy words ("ok", "please"), so anything close but different ("yes but ...", "go to atlas") is never taken as a yes, and an answer that mixes yes and abort ("yes abort") sends nothing and jarvis asks again: "Say yes to send, or abort." Asking for a project's status or report ("What is the status of atlas?", "read the report for atlas") only reads, so it runs straight away without a recap; if you did not name the project, jarvis asks "Which project?" and runs as soon as you say it. With no answer within `[interprete] confirmacao_s` seconds (30 by default, counted from the moment the recap finishes being spoken) the request is cancelled without sending anything. Noise or an "uh" on its own does not count as an answer. Telling the time or date, "cala-te" (be quiet), "dorme" (go to sleep) and "acorda" (wake up) run straight away, because they only read or silence.

The first prompt to a project opens that project's Claude Code session in a new window, with the jarvis channel attached; accept Claude Code's notice in that window once and the prompt follows. If the channel cannot be used, the prompt goes to a headless session in the project folder, and the command to resume that session is shown on screen. Nothing is written into your projects or into `~/.claude`: the files jarvis generates stay in `.jarvis/`, ignored by Git.

## General questions

A question that is not about a project or a local command ("what's the temperature in Porto today", "what football games are on today") is answered straight away, without a recap, because it only reads. jarvis says "Let me check." / "Deixa-me ver." and passes the question to a headless Claude Code (`claude -p`) that can only search and read the web: it runs in a neutral folder under the system temp folder, outside jarvis and every project, with no shell, no file editing and no MCP servers. The answer goes through the same spoken-reply filter as project replies; "cala-te", Ctrl+C, "dorme" or a new request drop an answer that has not arrived yet. These questions use your Claude subscription quota. The model, the time limit and your default location are in the optional `[perguntas]` table of `config.toml` (see `config.exemplo.toml`). Asset prices, quotes and trading are refused before anything leaves the PC.

## Conversation and notices

**Hands-free conversation.** When Claude's reply ends with a question you heard, jarvis listens for 8 seconds without the wake word (the key works too). Your answer keeps your words, without hesitations ("uh", "um") or an address to jarvis ("Jarvis, answer no, not right now" sends "No, not right now."). A short answer of up to 5 words ("Yes.", "the first one") is sent at once and jarvis says "Sent." / "Enviado."; a longer one goes into a quick recap, for example "Reply to Claude in atlas: keep the newer file and delete the older one. Send it?", and is sent only after your "yes". This window is the only place anything is sent without a recap, and a money or trading request is refused there too. Saying "sai da conversa" / "exit the conversation", or 8 seconds of silence, closes the window without sending anything.

**Spoken notices.** The sessions jarvis opens start with `--settings .jarvis/sessoes/<project>/settings.json`, which adds two Claude Code hooks (see `config/hooks-jarvis.exemplo.json`): `Stop`, and `Notification` for `idle_prompt` and `permission_prompt`. The hook sends jarvis only the kind of event, over the same authenticated local connection as the channel, never the message or the transcript. jarvis then says "atlas acabou" / "atlas is done" or "atlas está à espera de ti" / "atlas is waiting for you". With `[forja]` configured it also checks the FORJA run of each project every 30 seconds and says when a run finishes, fails or gets blocked. Notices wait their turn: never over your voice, another reply or a pending recap; at most one per session per minute; "cala-te" drops them and, while asleep, they only go to the log.

## The status orb

While jarvis runs with the microphone, a small borderless window stays on top of the other windows in a corner of the screen. It shows an orb that follows what jarvis is doing, in the style of a voice mode:

| Orb | Meaning |
| --- | --- |
| Small slate dot, breathing slowly | Waiting for the wake word or the key |
| Blue disc with a ring, growing with your microphone level | Listening to you (also during a conversation) |
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
3. **Understanding**: a local language model in Ollama (`qwen3:8b`, falling back to `qwen3:4b` when GPU memory is short) returns the intent, the project and, for a dictation, a clear rewritten prompt. It never adds requests of its own, and a project is never guessed: it has to be named.
4. **Confirmation**: the recap above; nothing leaves the PC before your "yes", except a general question (below).
5. **Action**: a local action, or the prompt into the project's Claude Code session.
6. **Spoken reply**: a resident voice that starts speaking while it is still synthesising (Kokoro, male British voice by default, for English when its model files are present, Piper `pt_PT-tugão-medium` for European Portuguese). Code, tool calls and file paths in Claude's replies are never read out; the full reply stays in the log.

Every sentence is logged with timestamps for each stage in `logs/jarvis-<date>.log` (ignored by Git), including the time from the end of your speech to the first sign of life and to the start of the spoken reply.

To measure the whole chain from WAV files, with a fake channel and without playing any sound:

```powershell
.venv\Scripts\python scripts/medir_ponta_a_ponta.py --verificar
```

It fails if "what time is it" takes more than 1.2 s (median) or 2.0 s (95th percentile) from the end of speech to the start of the reply, if a dictation takes more than 2.5 s / 4.0 s to the start of the recap, if the first sign of life takes more than 1.0 s, or if startup takes more than 30 s. The test WAVs are made by the local voice and only measure time; recognition accuracy has to be measured with your own recordings.

## Roadmap

Planned work, such as talking to jarvis in European Portuguese, is in [docs/ROADMAP.md](docs/ROADMAP.md).

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

Tests:

```powershell
.venv\Scripts\python -m unittest discover -s tests
```

## Hardware

Developed and tested on Windows 11 with an RTX 5060 Ti (16 GB VRAM), an i5-14400F and 32 GB of RAM, with Ollama installed locally.

## License

AGPL-3.0. Copyright (c) 2026 Nuno Marques. See [LICENSE](LICENSE).
