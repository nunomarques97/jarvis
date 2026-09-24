# Models downloaded into `models/`

`models/` is in `.gitignore`: the binary files never enter Git. This file is
the committed record of the source URL and sha256 of every model, so anyone can
rebuild the same environment without guessing where the files came from. There
are no absolute paths: every path below is relative to the repository root.

To verify a file that is already downloaded:

```
certutil -hashfile <path> SHA256
```

## Spoken reply: Piper `pt_PT-tugão-medium`

Source: the `rhasspy/piper-voices` repository on Hugging Face (the voice is
MIT-licensed, independently of the GPL-3.0 licence of the Piper engine).

**Installation note:** `python -m piper.download_voices pt_PT-tugão-medium`
fails on Windows with `UnicodeEncodeError`, because the voice name contains an
`ã` and the `piper-tts` CLI (`piper/download_voices.py`) does not percent-encode
the URL before passing it to `urllib`. The files were downloaded directly from
the same URLs (with `urllib.parse.quote`) and saved under the ASCII name
`pt_PT-tugao-medium.*` that the rest of the code uses.

| File | Source URL | sha256 |
|---|---|---|
| `models/piper/pt_PT-tugao-medium.onnx` | `https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_PT/tug%C3%A3o/medium/pt_PT-tug%C3%A3o-medium.onnx` | `223a7aaca69a155c61897e8ada7c3b13bc306e16c72dbb9c2fed733e2b0927d4` |
| `models/piper/pt_PT-tugao-medium.onnx.json` | `https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_PT/tug%C3%A3o/medium/pt_PT-tug%C3%A3o-medium.onnx.json` | `fe0918dfc0f1a89264a6eea4afe8e95d8e9fed3cc6c81b5c2f87fcb2b50c7320` |
| `models/piper/MODEL_CARD-pt_PT-tugao-medium.txt` | `https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_PT/tug%C3%A3o/medium/MODEL_CARD` | (informational text, no checksum: it is not executable and does not affect synthesis) |

The md5 of the `.onnx` (`0642048511ffe36c3b519520614b53f4`) and of the
`.onnx.json` (`c4113a1da477aa6db28420454c142ebd`) were checked against the
official `voices.json` of `rhasspy/piper-voices` at download time. The voice's
native sample rate is 22050 Hz (see the `.onnx.json` itself);
`scripts/gerar_wav.py` resamples to 16 kHz mono.

## Transcription: faster-whisper

Source: the `Systran/faster-whisper-<size>` repositories on Hugging Face
(official CTranslate2 conversions of OpenAI's Whisper, MIT licence). They are
downloaded automatically by `faster_whisper.WhisperModel(..., download_root=...)`
on first use and cached locally under `models/faster-whisper` in the
`huggingface_hub` layout (`models--Systran--faster-whisper-<size>/`).

| Model | Weights file | Source URL | sha256 |
|---|---|---|---|
| `small` (fallback) | `models/faster-whisper/models--Systran--faster-whisper-small/snapshots/536b0662742c02347bc0e980a01041f333bce120/model.bin` | `https://huggingface.co/Systran/faster-whisper-small/resolve/main/model.bin` | `3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671` |
| `medium` (preferred) | `models/faster-whisper/models--Systran--faster-whisper-medium/snapshots/08e178d48790749d25932bbc082711ddcfdfbc4f/model.bin` | `https://huggingface.co/Systran/faster-whisper-medium/resolve/main/model.bin` | `9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae` |
| `large-v3` (not used, see note) | `models/faster-whisper/models--Systran--faster-whisper-large-v3/snapshots/edaa852ec7e145841d8ffdb056a99866b5f0a478/model.bin` | `https://huggingface.co/Systran/faster-whisper-large-v3/resolve/main/model.bin` | `69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1` |
| `large-v3-turbo` (measured, not adopted, see note) | `models/faster-whisper/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/snapshots/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf/model.bin` | `https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo/resolve/main/model.bin` | `e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da` |

**About `large-v3` (2.88 GB of weights; the whole `models/faster-whisper/blobs`
folder takes 4.8 GB).** No code in this repository loads it: moving up to
`large-v3` is only justified if the word error rate of `medium` misses its
threshold, and that measurement does not exist yet. It was downloaded while
diagnosing hallucinations on short phrases across model sizes, and it is listed
here because every model that enters `models/` is recorded, used or not.

Measured on five synthetic WAV files of about 0.72 s with the same phrase and
the same vocabulary `initial_prompt`: `medium` got "horas" right in 4,
`large-v3` in 2 and `small` in 1. In other words, **moving from `medium` to
`large-v3` does not fix short isolated phrases**, and there is no measured
reason to keep it. Nothing is deleted automatically: the
`models--Systran--faster-whisper-large-v3/` folder and its blobs can be removed
by hand. If `large-v3` is ever needed, it downloads again automatically from
the URL above.

`medium` is the default model (with an automatic fallback to `small` in
`scripts/transcrever_ficheiro.py` if `medium` fails to load or to transcribe on
the requested device). Each snapshot folder also contains `config.json`,
`tokenizer.json` and `vocabulary.txt`: small, non-executable files from the
same download. They have no checksum here because they do not affect the
integrity of the model itself (Hugging Face already verifies repository content
through Git/LFS on the server side).

**About `large-v3-turbo` (1.51 GB of weights).** Source repository
`mobiuslabsgmbh/faster-whisper-large-v3-turbo` (MIT licence, a CTranslate2
conversion of `openai/whisper-large-v3-turbo`), already listed in
`faster_whisper.utils._MODELS` of the installed version, so no new dependency.
Downloaded with:

```
.venv\Scripts\python -c "import faster_whisper; faster_whisper.WhisperModel('large-v3-turbo', device='cuda', compute_type='float16', download_root='models/faster-whisper')"
```

**It is in both closed lists** (`MODELOS_STT_PERMITIDOS` in `jarvis/app.py`,
`MODELOS_PERMITIDOS` in `scripts/transcrever_ficheiro.py`) so it can be
measured, but **it is NOT the default model**: `medium` remains preferred. The
decision came from the numbers, not from preference: a controlled A/B on the
SAME WAV files, with `language='pt'` fixed on both sides, across four
combinations:

| Combination | Intent accuracy, `medium` | Intent accuracy, `large-v3-turbo` |
|---|---|---|
| PT without prefix | **11/20 (55.0%)** | 10/20 (50.0%) |
| PT with prefix | 10/20 (50.0%) | 10/20 (50.0%) |
| EN without prefix | 10/20 (50.0%) | 10/20 (50.0%) |
| EN with prefix | 10/20 (50.0%) | 10/20 (50.0%) |

The adoption rule required accuracy **equal or better in all four**
combinations. It fails on PT without prefix by one row, and it is the row that
costs most: phrase 4, «abre a pasta do jarvis» ("open the jarvis folder"),
which `medium` transcribes as «Abre a pasta dos Javis.» (the allow-list still
catches it) and `turbo` as «Hava a pasta do Javis.» (sent as text to Claude
Code). So turbo loses the **only** local action that combination got right.
Latency and VRAM passed comfortably (p50 335-397 ms against 424-576 ms for
`medium`; p95 390-745 ms against 614-1956 ms; a peak of 4503 MiB used on the
whole GPU, about 2.4 GB for the process, with 11548 MiB still free), but
accuracy comes first. It is the second case, after plain `large-v3`, where
**bigger is not better** on this sample of short isolated phrases.

The `int8_float16` fallback was not needed: VRAM was never tight.

## Wake word: openWakeWord

Source: the release assets of the `dscripka/openWakeWord` repository (v0.5.1),
the same URLs the package itself uses (`openwakeword.MODELS`,
`openwakeword.FEATURE_MODELS`, `openwakeword.VAD_MODELS`). The code is
Apache-2.0; **the pre-trained models are licensed CC-BY-NC-SA-4.0**
(non-commercial). That is acceptable for personal use, and they are never
redistributed: `models/` is in `.gitignore`.

Exact command used to download them into the repository:

```
.venv\Scripts\python -c "import openwakeword.utils as u; u.download_models(['hey_jarvis'], target_directory='models/openwakeword')"
```

| File | Source URL | sha256 |
|---|---|---|
| `models/openwakeword/hey_jarvis_v0.1.onnx` | `https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/hey_jarvis_v0.1.onnx` | `94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb` |
| `models/openwakeword/hey_jarvis_v0.1.tflite` | `https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/hey_jarvis_v0.1.tflite` | `14bff778604985e1b5c19f0f7bbe477a69cf281d8db34b232b3b972411f710e2` |
| `models/openwakeword/melspectrogram.onnx` | `https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.onnx` | `ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f` |
| `models/openwakeword/melspectrogram.tflite` | `https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.tflite` | `96fa0adccb6e8cf95cb14465409a1a2898ee4a96a85bb9ed3c7eb0e68bf163e8` |
| `models/openwakeword/embedding_model.onnx` | `https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.onnx` | `70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f` |
| `models/openwakeword/embedding_model.tflite` | `https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.tflite` | `c0aea21eb84a4ce90a08c870da41b7a7173b45269e6a3207c71d67c40f3a59d8` |
| `models/openwakeword/silero_vad.onnx` (not used by jarvis) | `https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/silero_vad.onnx` | `a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28` |

`jarvis/app.py` points explicitly at three of these files
(`hey_jarvis_v0.1.onnx`, `melspectrogram.onnx`, `embedding_model.onnx`), with
`inference_framework="onnx"`; the `.tflite` files come in the same download and
are not loaded. Neither is `silero_vad.onnx`: the VAD in use is RealtimeSTT's
(WebRTC plus Silero from `torch.hub`).

**A second copy, inside `.venv` (not chosen by jarvis).** When RealtimeSTT
starts with `wakeword_backend="oww"`, it always calls
`openwakeword.utils.download_models()` with no arguments, which downloads
**all** pre-trained models (`alexa`, `hey_mycroft`, `hey_rhasspy`, `timer`,
`weather`, as well as `hey_jarvis`) into the installed package
(`.venv/Lib/site-packages/openwakeword/resources/models/`, about 18 MB). This
happens on the first run, uses the same v0.5.1 release and the same URLs as the
table above, and stays inside `.venv`, which `.gitignore` also covers and which
is removed with the folder. jarvis loads none of those extra models: it always
passes the path of `hey_jarvis` in `models/openwakeword/`. It is recorded here
so nobody finds out late that the installation brings more models than this
repository chose.
