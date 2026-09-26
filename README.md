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
.venv\Scripts\python -m jarvis --wav a.wav b.wav     # WAV files instead of the microphone
```

Ctrl+C, closing the window or saying "cala-te" / "be quiet" silences the voice at once.

## Talking to it

**Push-to-talk key.** Hold the key, speak, release. The default key is right Ctrl; `[ouvido] tecla` in `config.toml` can change it (right Alt, right Shift, Scroll Lock or F13 to F24). This is the main way to talk to jarvis: no false wake-ups and no waiting for the end of speech.

**Wake word, hands-free.** Say the wake word followed by the request; jarvis stops listening when you stop talking. The language is set by `[ouvido] lingua`:

- `"en"`: the wake word is "hey jarvis" (pre-trained openWakeWord model).
- `"pt"`: the wake word is "boas jarvis". Its model is trained locally with your own recordings (`scripts/treinar_ativacao.py`). Until `models/openwakeword/boas_jarvis.onnx` exists, hands-free listening is off and the key still works.

`[ouvido] limiar_ativacao` sets how sure the detector must be before it wakes up.

## Confirming, correcting, cancelling

Everything that has an effect (sending a prompt to Claude Code, opening the editor or a folder) is first recapped out loud and on screen, for example "Para o atlas: Corrige o teste do login. Envio?". Then you answer straight away, without the wake word: as soon as the recap finishes being spoken, jarvis listens for your reply until the end of speech and the status line shows `À ESPERA DE CONFIRMAÇÃO — A OUVIR A RESPOSTA`. The key and the wake word also work. Without voice activity detection (`--sem-ativacao`) only the key does.

| You say (pt) | You say (en) | What happens |
|---|---|---|
| "sim", "envia", "manda", "confirma" | "yes", "yeah", "yep", "sure", "send it", "go ahead", "do it", "confirm" | the recapped prompt is sent, exactly as shown |
| "não, muda testes para documentação" | "no, change tests to documentation" | jarvis corrects the prompt and recaps again |
| "acrescenta que é urgente" | "add that it is urgent" | jarvis adds it and recaps again |
| "cancela" | "cancel" | nothing is sent |

Only those whole answers send; anything close but different ("yes but ...", "go to atlas") is never taken as a yes. With no answer within `[interprete] confirmacao_s` seconds (30 by default, counted from the moment the recap finishes being spoken) the request is cancelled without sending anything. Noise or an "uh" on its own does not count as an answer. Telling the time or date, "cala-te" (be quiet), "dorme" (go to sleep) and "acorda" (wake up) run straight away, because they only read or silence.

The first prompt to a project opens that project's Claude Code session in a new window, with the jarvis channel attached; accept Claude Code's notice in that window once and the prompt follows. If the channel cannot be used, the prompt goes to a headless session in the project folder, and the command to resume that session is shown on screen. Nothing is written into your projects or into `~/.claude`: the files jarvis generates stay in `.jarvis/`, ignored by Git.

## General questions

A question that is not about a project or a local command ("what's the temperature in Porto today", "what football games are on today") is answered straight away, without a recap, because it only reads. jarvis says "Let me check." / "Deixa-me ver." and passes the question to a headless Claude Code (`claude -p`) that can only search and read the web: it runs in a neutral folder under the system temp folder, outside jarvis and every project, with no shell, no file editing and no MCP servers. The answer goes through the same spoken-reply filter as project replies; "cala-te", Ctrl+C, "dorme" or a new request drop an answer that has not arrived yet. These questions use your Claude subscription quota. The model, the time limit and your default location are in the optional `[perguntas]` table of `config.toml` (see `config.exemplo.toml`). Asset prices, quotes and trading are refused before anything leaves the PC.

## Conversation and notices

**Hands-free conversation.** When Claude's reply ends with a question you heard, jarvis listens for 8 seconds without the wake word (the key works too). What you say goes into a quick recap word for word, for example "Responder ao Claude no atlas: sim, corre a suite inteira. Envio?", and is sent only after your "yes". Saying "sai da conversa" / "exit the conversation", or 8 seconds of silence, closes the window without sending anything.

**Spoken notices.** The sessions jarvis opens start with `--settings .jarvis/sessoes/<project>/settings.json`, which adds two Claude Code hooks (see `config/hooks-jarvis.exemplo.json`): `Stop`, and `Notification` for `idle_prompt` and `permission_prompt`. The hook sends jarvis only the kind of event, over the same authenticated local connection as the channel, never the message or the transcript. jarvis then says "atlas acabou" / "atlas is done" or "atlas está à espera de ti" / "atlas is waiting for you". With `[forja]` configured it also checks the FORJA run of each project every 30 seconds and says when a run finishes, fails or gets blocked. Notices wait their turn: never over your voice, another reply or a pending recap; at most one per session per minute; "cala-te" drops them and, while asleep, they only go to the log.

## How it works

1. **Listening**: the push-to-talk key, or openWakeWord followed by voice activity detection (webrtcvad).
2. **Transcription**: NVIDIA Parakeet TDT 0.6B v3 (ONNX, on the CPU by default), loaded once and kept warm. faster-whisper can be chosen instead with `[ouvido] motor`.
3. **Understanding**: a local language model in Ollama (`qwen3:8b`, falling back to `qwen3:4b` when GPU memory is short) returns the intent, the project and, for a dictation, a clear rewritten prompt. It never adds requests of its own, and a project is never guessed: it has to be named.
4. **Confirmation**: the recap above; nothing leaves the PC before your "yes", except a general question (below).
5. **Action**: a local action, or the prompt into the project's Claude Code session.
6. **Spoken reply**: a resident voice that starts speaking while it is still synthesising (Kokoro for English when its model files are present, Piper `pt_PT-tugão-medium` for European Portuguese). Code, tool calls and file paths in Claude's replies are never read out; the full reply stays in the log.

Every sentence is logged with timestamps for each stage in `logs/jarvis-<date>.log` (ignored by Git), including the time from the end of your speech to the first sign of life and to the start of the spoken reply.

To measure the whole chain from WAV files, with a fake channel and without playing any sound:

```powershell
.venv\Scripts\python scripts/medir_ponta_a_ponta.py --verificar
```

It fails if "what time is it" takes more than 1.2 s (median) or 2.0 s (95th percentile) from the end of speech to the start of the reply, if a dictation takes more than 2.5 s / 4.0 s to the start of the recap, if the first sign of life takes more than 1.0 s, or if startup takes more than 30 s. The test WAVs are made by the local voice and only measure time; recognition accuracy has to be measured with your own recordings.

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
