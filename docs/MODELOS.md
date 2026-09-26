# Models downloaded into `models/`

`models/` is in `.gitignore`: the binary files never enter Git. This file is
the committed record of the source URL and sha256 of every model, so anyone can
rebuild the same environment without guessing where the files came from. There
are no absolute paths: every path below is relative to the repository root.

To verify a file that is already downloaded:

```
certutil -hashfile <path> SHA256
```

## Spoken reply: resident engine

`jarvis/voz.py` loads the voice engine once per process (at start-up, through
`voz.aquecer()`) and keeps it in memory; no synthesis process is started per
sentence. Both engines run inside the Python process on onnxruntime (CPU, four
threads) and stream: the first audio block plays while the rest of the reply
is still being synthesised. `scripts/medir_latencia_voz.py --verificar`
measures text -> first audio block on 20 fixed sentences (targets: 300 ms
p50, 600 ms p95) and the silencing time, without playing any sound.

The engine follows the language of the reply text, never what happens to be
installed (`voz.lingua_da_voz()`; `--lingua` on `python -m jarvis.voz` and on
the measurement script):

- **Portuguese (`pt`, the default):** **Piper `pt_PT-tugão-medium`** through
  the `piper-tts` library (next section). The jarvis replies are written in
  Portuguese today, so this is the voice that speaks them. A Portuguese reply
  never reaches the English engine, even when Kokoro is installed.
- **English (`en`):** **Kokoro-82M** (`kokoro-onnx`, male British voice
  `bm_fable` by default, set in the optional `[voz]` table of `config.toml`;
  see "Choosing the English voice" below), the natural English voice of the
  chosen product language. It
  is loaded and warmed up with one throw-away sentence; if the package or a
  model file is missing, or that first synthesis fails, Piper speaks instead
  and the start-up log line says why. The switch to `en` happens when the
  reply texts are written in English.

### Kokoro-82M (ONNX)

**Status: installed and verified (English voices `bm_fable` and `af_heart`, CPU).** To install, add the package into the venv and download
the two model files into `models/kokoro/`:

```
.venv\Scripts\python -m pip install "kokoro-onnx==0.6.1"
mkdir models\kokoro
curl.exe -L -o models\kokoro\kokoro-v1.0.onnx https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl.exe -L -o models\kokoro\voices-v1.0.bin https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
```

After the download, record the sha256 of each file here
(`Get-FileHash models\kokoro\* -Algorithm SHA256` in PowerShell) and run
`scripts/medir_latencia_voz.py --verificar --lingua en`: it measures the
Kokoro voice with English sentences and fails if the engine loaded for `en`
is not Kokoro.

| File | Source URL | sha256 |
|---|---|---|
| `models/kokoro/kokoro-v1.0.onnx` | `https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx` | `7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5` |
| `models/kokoro/voices-v1.0.bin` | `https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin` | `bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d` |

### Choosing the English voice

The English voice is a closed list in `jarvis/config.py` (`VOZES_INGLESAS`):
`af_heart` (American, the previous voice) and the four British male voices in
`voices-v1.0.bin`: `bm_george`, `bm_lewis`, `bm_daniel`, `bm_fable`. The `b`
voices use the `en-gb` phonemizer and the `a` voice `en-us`. `[voz] nome` in
`config.toml` picks one; an unknown name is a configuration error, and a voice
missing from the voices file falls back to Piper like any other Kokoro failure.

**Default: `bm_fable`.** Measured with
`scripts/medir_latencia_voz.py --vozes --rodadas 5 --verificar --evidencia`
(one model loaded once, every voice warmed up, the 20 fixed English sentences
x 5 rounds per voice, voice order rotated every sentence to cancel drift, CPU,
no sound):

| voice | phonemizer | speed | p50 ms | p95 ms |
|---|---|---|---|---|
| `af_heart` (reference) | en-us | 1.0 | 583 | 825 |
| `bm_george` | en-gb | 1.3 | 576 | 812 |
| `bm_lewis` | en-gb | 1.2 | 614 | 843 |
| `bm_daniel` | en-gb | 1.2 | 558 | 796 |
| **`bm_fable`** | en-gb | 1.35 | **501** | **668** |

Why these numbers decide it:

- **Speed per voice (`VELOCIDADE_DAS_VOZES` in `jarvis/voz.py`).** Time to
  first audio grows with the length of audio to synthesise. At speed 1.0 the
  British voices speak more slowly than `af_heart` (about 145 to 172 words per
  minute against 196 on the fixed sentences), and every one of them was slower
  to first audio (an earlier 3-round run at 1.0: `af_heart` 524/705 ms p50/p95,
  `bm_george` 655/832, `bm_lewis` 594/849, `bm_daniel` 583/772, `bm_fable`
  646/814). Each voice now runs at the speed that matches the `af_heart`
  pace. On the sample sentence of `scripts/amostras_voz.py` that gives
  `af_heart` 204, `bm_george` 196, `bm_lewis` 203, `bm_daniel` 192 and
  `bm_fable` 200 words per minute.
- **`bm_fable` is the only British voice clearly faster than `af_heart` at
  both p50 and p95 in every run.** It was 497/642 ms against 552/768 in a
  3-round run of all five voices, 485/644 against 555/746 in a 5-round run of
  three voices, and 501/668 against 583/825 in the run above.
- **`bm_george` is a tie with `af_heart`.** It passed one run (550/743 against
  552/768) and failed the next (556/771 against 555/746), so it cannot
  guarantee "not slower". `bm_daniel` and `bm_lewis` are also mixed or slower.
  In the model's own ranking
  ([VOICES.md](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md)),
  `bm_lewis` and `bm_daniel` get lower overall grades than `bm_fable` and
  `bm_george`, which share the best British male grade.

The latency is above the older 300/600 ms voice target for every Kokoro
voice, `af_heart` included. That is the pre-existing cost of Kokoro on this
CPU, and this choice does not make it worse.

To hear the voices before switching, write one WAV per voice to
`audio/amostras-voz/` (ignored by Git). Nothing plays without `--com-som`:

```
.venv\Scripts\python scripts/amostras_voz.py
.venv\Scripts\python scripts/amostras_voz.py --com-som
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

## Transcription: NVIDIA Parakeet TDT 0.6B v3 (ONNX)

The local STT engine chosen for short commands and dictation, measured against
faster-whisper `medium` and `large-v3-turbo` on the Sponsor's own voice by
`scripts/avaliar_voz.py`; the engine that wins on that measurement stays.
Model: NVIDIA Parakeet TDT 0.6B v3 (25 European languages, including
Portuguese and English; CC-BY-4.0), in the ONNX export that `onnx-asr`
publishes on Hugging Face (`istupakov/parakeet-tdt-0.6b-v3-onnx`, the
repository behind the `nemo-parakeet-tdt-0.6b-v3` alias of `onnx-asr`).

**Status: not downloaded yet.** `jarvis/stt.py` never downloads it: until the
two commands below have been run, the `parakeet-tdt-0.6b-v3` adapter reports
itself unavailable and the evaluator skips it with that reason.

Install the package into the venv, then download the model into `models/`:

```
.venv\Scripts\python -m pip install "onnx-asr[cpu,hub]==0.12.0"
.venv\Scripts\python -c "import onnx_asr; onnx_asr.load_model('nemo-parakeet-tdt-0.6b-v3', 'models/parakeet-tdt-0.6b-v3')"
```

The onnxruntime in this venv is CPU-only, so the evaluator runs this engine
with `--motores ...,parakeet-tdt-0.6b-v3:cpu`; the device each engine ran on is
written in the evidence.

After the download, record every file of `models/parakeet-tdt-0.6b-v3/` here
with its sha256 (`Get-FileHash models\parakeet-tdt-0.6b-v3\* -Algorithm SHA256`
in PowerShell) and its source URL
(`https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx/resolve/main/<file>`).

| File | Source URL | sha256 |
|---|---|---|
| (pending download) | — | — |

### Accent adaptation (English)

The Parakeet engine can be adapted to the Sponsor's Portuguese-accented
English without a new package and without training the model
(`jarvis/adaptacao.py`, `[adaptacao]` in `config.toml`):

- **Phrase boosting.** During greedy TDT decoding, tokens that continue one of
  jarvis' command phrases or a project name from `config.toml` get a logit
  bonus (`bonus`, default 1.5). This is the shallow-fusion word boosting that
  [NVIDIA NeMo documents](https://docs.nvidia.com/nemo-framework/user-guide/latest/nemotoolkit/asr/asr_customization/word_boosting.html)
  for RNN-T/TDT models, applied to onnx-asr 0.12.0 through its per-step
  `_decode` call. If that private hook is missing or fails, transcription goes
  on without boosting and the log says so. The boosted vocabulary is the
  product's own vocabulary. It is never taken from test-set errors.
- **Correction lexicon.** Whole-word "heard -> meant" rules, learned by
  `scripts/adaptar_sotaque.py` from the training side only. A rule is kept
  only if the heard form is never a correct word in a training phrase. It
  must also make no training phrase worse, in edit distance or in the intent
  the router gives, and it must improve at least one. The lexicon comes from
  the Sponsor's voice, so it lives in `models/adaptacao/lexico-en.json`, which
  is ignored by Git.

Fine-tuning (LoRA or adapters) was not chosen. It needs a NeMo install, a few
minutes of audio is too little for it, and the result would have to be
exported to ONNX again.

**Measurement.** `scripts/adaptar_sotaque.py --medir` splits `recordings/en/`
deterministically (seed 1790, half for training, stratified by the script's
case). Every recording in `recordings/treino-en/` goes to the training side
only. The command then measures on the test side only, in one run with one
model loaded: no adaptation, boosting only, lexicon only, and both. Boosted
and unboosted decoding alternate order on every phrase, so their latencies
are comparable. The goal for a variant: a lower word error rate *and* more
preserved intents than no adaptation in the same run, with p50 latency at
most 1.2x.

Result on 2026-09-26 (Parakeet CPU, 44 evaluation recordings, 0 training
recordings, so 22 phrases for training and 22 for testing, 8 lexicon rules
learned):

| variant | WER | preserved intent | project right | p50 |
|---|---|---|---|---|
| no adaptation | 24.8% | 20/22 | 14/22 | 124 ms |
| boosting only | 21.8% | 20/22 | 16/22 | 127 ms |
| lexicon only | 24.2% | 20/22 | 15/22 | 124 ms |
| boosting + lexicon | 22.4% | 20/22 | 16/22 | 127 ms |

Boosting lowers the word error rate and gets more project names right, with
no noticeable latency cost. No variant preserves more intents than no
adaptation on this test set, so **the goal is not met and `[adaptacao]` stays
off by default**. The next step needs the Sponsor: record the training script
(`tests/voz/guiao-treino-en.md`), pilot first, with
`scripts/gravar_voz.py --lingua en --treino`, then run
`scripts/adaptar_sotaque.py --medir` again. The README lists the steps under
"Adapting to your accent".

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

### Threshold and measurement on the Sponsor's voice

The detection threshold is `[ouvido] limiar_ativacao` in `config.toml`
(default `0.6`, documented in `config.exemplo.toml`). It is chosen from the
numbers, not guessed: `scripts/avaliar_ativacao.py` runs the Sponsor's
recordings through the same detector the ear uses
(`jarvis.ouvido.DetetorOpenWakeWord`, 80 ms steps) and writes the curve
threshold -> detection / false wakes per 30 min to `docs/forja/evidence/`
(ignored by Git), together with the threshold to put in `config.toml`.
Targets: detection >= 95% over 20 repetitions, <= 1 false wake per 30 min of
normal household noise. The recording script is `tests/voz/guiao-ativacao.md`.
Only recordings whose manifest says they came from the microphone count;
anything else is listed as set aside. Without those recordings the evidence
says "PENDENTE — passo do Sponsor" and the targets are not declared met;
synthetic voice never decides.

False wakes in the noise recording are counted at most once per 0.94 s: the
shortest time the ear ignores the detector after a wake (0.25 s of deafness,
90 ms of speech, 0.6 s of closing silence). Without speech the ear waits
longer (up to 5 s), so the count never falls below what the Sponsor would
hear.

The wake word is removed from the transcribed text before the interpreter
only when its exact words are there (`jarvis.ouvido.retirar_palavra_de_ativacao`);
misheard spellings are never mapped back to the wake word.

## Wake word: "boas jarvis" (Portuguese, trained locally)

Only needed when the product runs in Portuguese (`[ouvido] lingua = "pt"`);
English uses the pre-trained `hey_jarvis` above. There is no pre-trained
"boas jarvis" model, so `scripts/treinar_ativacao.py` trains one with what is
already in `.venv` and `models/` (openWakeWord 0.6.0, scikit-learn, piper-tts,
onnxruntime); nothing is downloaded or installed.

**Status: not trained yet** — it needs the Sponsor's training recordings
(step 1 below). The model file is `models/openwakeword/boas_jarvis.onnx`;
until it exists, Portuguese hands-free mode reports it missing and points
here.

How the model is built:

- **Features:** openWakeWord's own `melspectrogram.onnx` and
  `embedding_model.onnx` (above): 2 s windows -> 16 x 96 features, exactly
  what the detector gives the model every 80 ms.
- **Positives:** the Sponsor's 20 *training* repetitions (a set separate from
  the 20 evaluation repetitions), each augmented 15 times (gain, noise,
  position in the window), plus 300 "boas jarvis" syntheses by the Piper
  `pt_PT-tugão-medium` voice with varied speed and intonation.
- **Negatives:** near-miss phrases ("boas", "jarvis", "boa noite", "boas
  notícias"...), everyday household phrases and fragments of the voice-test
  script, all spoken by the same synthetic voice; the Sponsor's voice-test
  recordings that do not start with the wake word; generated white, pink and
  brown noise. The evaluation repetitions and the household noise recording
  are never read by the training, so the measurement is not inflated.
- **Model:** a small MLP (scikit-learn, one hidden layer of 64) with the
  feature normalisation folded into the weights, written as ONNX
  (`Flatten -> Gemm -> Relu -> Gemm -> Sigmoid`) by a minimal protobuf
  encoder in the script, because the `onnx` package is not in the venv. The
  file is checked with onnxruntime against scikit-learn and loaded by the
  ear's detector before it is reported ready.
- **Reproducible procedure:** fixed seed (`--semente`, default 1790) for the
  augmentation, the split and the MLP. The file itself is not byte-identical
  between runs: Piper samples its synthesis noise inside its ONNX model, so a
  re-run can give slightly different synthetic positives and another sha256.
  Next to the model the script writes `boas_jarvis.onnx.json`: date, sample
  counts, parameters, seed, package versions and the sha256; no transcripts
  and no paths. The sha256 recorded below identifies the file in use.
- **Only the Sponsor's microphone recordings** are used as real positives
  and negatives; recordings marked synthetic in their manifest are skipped.

Procedure:

1. Record the 20 training repetitions (script in `tests/voz/guiao-ativacao.md`):
   ```
   .venv\Scripts\python scripts/avaliar_ativacao.py --gravar-palavra --lingua pt --conjunto treino
   ```
2. Train (about 7 min on the CPU):
   ```
   .venv\Scripts\python scripts/treinar_ativacao.py
   ```
3. Record the printed sha256 in the table below, then measure with the
   evaluation set and the noise recording:
   ```
   .venv\Scripts\python scripts/avaliar_ativacao.py --lingua pt
   ```

| File | Source | sha256 |
|---|---|---|
| `models/openwakeword/boas_jarvis.onnx` | trained locally by `scripts/treinar_ativacao.py` | (pending: needs the Sponsor's training recordings) |

`--ensaio-sintetico --saida <folder outside the repository>` trains on
synthetic voice only, to prove the chain; it refuses to write to the real
model path. A trial like that says nothing about the Sponsor's voice. On
2026-09-25 a synthetic-only trial, streamed through the ear's detector on 14
new sentences from the same synthetic voice, scored the 4 sentences starting
with "boas jarvis" at 0.999-1.000 and the 10 without it at 0.153 at most. An
earlier trial without the near-miss and household phrases woke on "boa noite
a todos" (0.84), which is why they are in the negatives. Only the Sponsor's
recordings and the measured curve decide the real model and its threshold.

## Interpreter: Qwen3 8B through Ollama (fallback Qwen3 4B)

`jarvis/interprete.py` classifies each transcribed sentence (intent from a
closed list, project from `config.toml`) and rewrites dictation into a clear
prompt before the confirmation step. It talks to the local Ollama server over
HTTP on `127.0.0.1` only (no new Python dependency) and asks for JSON bound to
a schema, with thinking off. The models live in Ollama's own store, not in
`models/`.

Financial requests (buying, selling, investing, paying or placing money in any
asset) are always refused. A deterministic rule checks the sentence before the
model and the rewritten prompt after it, and the schema also has a boolean
`financeiro` field. When the model sets that field, the sentence is refused.
The field can only refuse a sentence; it can never let through one that the
rule caught.

**Status: both models pulled.** On a machine without them the interpreter
reports that no model is available and `scripts/avaliar_interprete.py
--verificar` exits with code 2 and prints these commands:

```
ollama pull qwen3:8b
ollama pull qwen3:4b
```

`qwen3:8b` is the main model; `qwen3:4b` is used when the main one is not
installed or does not fit in the free VRAM measured at start-up (the
evaluator prints the VRAM before and after loading, and the share of the model
that ended up in VRAM). Both names can be changed in the `[interprete]` table
of `config.toml` (see `config.exemplo.toml`).

Golden-set evaluation:

```
.venv\Scripts\python scripts/avaliar_interprete.py --verificar --evidencia
```

It fails if intent accuracy is below 95% or project accuracy below 98% in
either language, if any rewritten prompt adds a request, or if warm latency
exceeds 1.2 s p50 / 2.5 s p95.
