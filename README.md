# jarvis: voice control for Claude Code sessions

A local voice assistant for Windows. It listens for the wake word "hey jarvis", transcribes the request on your own PC, and either runs a small local action or hands the text to a Claude Code session, then answers out loud. It replaces typing prompts into the projects listed in your local configuration (`config.toml`).

## Example commands

- "Hey jarvis, open VS Code in `<project-1>`."
- "Hey jarvis, ask for the status of the last session in `<project-1>`."
- "Hey jarvis, read me the report of the last run."
- "Hey jarvis, start run X in project Y."

A fixed allow-list of local actions runs without calling Claude Code: telling the time and date, opening VS Code in a configured project, opening a configured folder, stopping speech, and putting jarvis to sleep or waking it up. Everything else goes to Claude Code as text.

A future trading integration may turn "prepare the trade" into a proposal with numbers; confirmation is always typed by the user, never given by voice.

## How it works

1. **Wake word**: openWakeWord with the pre-trained `hey_jarvis` model.
2. **Transcription**: faster-whisper (`medium` by default) on the GPU, through RealtimeSTT.
3. **Routing**: a deterministic router with a closed allow-list. No language model decides what runs.
4. **Action**: a local action, or delivery to a Claude Code session through the `claude` CLI, with the sentence sent over stdin.
5. **Spoken reply**: Piper with the European Portuguese `pt_PT-tugão-medium` voice, falling back to console text if speech fails.

Commands are spoken in European Portuguese. The router also has an English allow-list, but automatic language detection is off because it lowered accuracy in measurements.

## Principles

- Everything is local and free: no paid cloud, no API keys, no audio sent off the PC.
- No buy or sell orders by voice, now or later. An AI never places orders, and voice is not authorisation to spend money.
- Nothing is installed into other projects from here.
- No conversations, transcripts, audio or local tool state in commits. Recordings and transcripts are in `.gitignore`.
- No secrets, tokens, `.env` files or personal paths in the code or the docs. Personal configuration lives in ignored files, with a committed example.

## Setup

From the repository root, in a virtual environment inside the repository:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -U pip
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python scripts/verificar_ambiente.py
```

The last command checks that faster-whisper loads and transcribes on the GPU.

Copy `config.exemplo.toml` to `config.toml` and fill in your microphone name and your projects. `config.toml` is ignored by Git. The models used are listed, with source URLs and checksums, in [docs/MODELOS.md](docs/MODELOS.md).

## Usage

```powershell
.venv\Scripts\python -m jarvis.app                          # microphone
.venv\Scripts\python -m jarvis.app --wav <file.wav>         # feed a WAV file instead
.venv\Scripts\python -m jarvis.app --autoteste              # self-test of the pure parts
```

## Hardware

Developed and tested on Windows 11 with an RTX 5060 Ti (16 GB VRAM), an i5-14400F and 32 GB of RAM, with Ollama installed locally.

## License

AGPL-3.0. Copyright (c) 2026 Nuno Marques. See [LICENSE](LICENSE).
