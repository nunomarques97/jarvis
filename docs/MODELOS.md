# Modelos descarregados para `models/` (D14e)

`models/` está no `.gitignore`: os ficheiros binários nunca entram no Git. Este
ficheiro é o registo versionado exigido pela D14e — URL de origem e sha256 de
cada modelo, para qualquer pessoa reconstruir o mesmo
ambiente sem adivinhar de onde os ficheiros vieram. Sem caminhos absolutos:
todos os caminhos abaixo são relativos à raiz do repositório.

Para verificar um ficheiro já descarregado:

```
certutil -hashfile <caminho> SHA256
```

## Voz de resposta — Piper `pt_PT-tugão-medium` (D37/D47, TECHNOLOGY.md S4)

Fonte: repositório `rhasspy/piper-voices` no Hugging Face (voz licenciada MIT,
independente da licença GPL-3.0 do motor Piper — ver TECHNOLOGY.md S4).

**Nota de instalação:** `python -m piper.download_voices pt_PT-tugão-medium`
falha em Windows com `UnicodeEncodeError` porque o nome da voz tem um `ã` e o
CLI do `piper-tts` (`piper/download_voices.py`) não faz percent-encoding do
URL antes de o passar ao `urllib`. Os ficheiros foram descarregados a direito
das mesmas URLs (com `urllib.parse.quote`) e guardados com o nome ASCII
`pt_PT-tugao-medium.*` que o resto do código deste repositório usa.

| Ficheiro | URL de origem | sha256 |
|---|---|---|
| `models/piper/pt_PT-tugao-medium.onnx` | `https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_PT/tug%C3%A3o/medium/pt_PT-tug%C3%A3o-medium.onnx` | `223a7aaca69a155c61897e8ada7c3b13bc306e16c72dbb9c2fed733e2b0927d4` |
| `models/piper/pt_PT-tugao-medium.onnx.json` | `https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_PT/tug%C3%A3o/medium/pt_PT-tug%C3%A3o-medium.onnx.json` | `fe0918dfc0f1a89264a6eea4afe8e95d8e9fed3cc6c81b5c2f87fcb2b50c7320` |
| `models/piper/MODEL_CARD-pt_PT-tugao-medium.txt` | `https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_PT/tug%C3%A3o/medium/MODEL_CARD` | (texto informativo, sem hash de verificação — não é executável nem afeta a síntese) |

Os md5 do `.onnx` (`0642048511ffe36c3b519520614b53f4`) e do `.onnx.json`
(`c4113a1da477aa6db28420454c142ebd`) foram confirmados contra o
`voices.json` oficial do `rhasspy/piper-voices` no momento do download.
Taxa de amostragem nativa da voz: 22050 Hz (ver o próprio `.onnx.json`) —
`scripts/gerar_wav.py` reamostra para os 16 kHz mono pedidos pela T3.

## Transcrição — faster-whisper (D39, TECHNOLOGY.md S3)

Fonte: repositório `Systran/faster-whisper-<tamanho>` no Hugging Face
(conversões CTranslate2 oficiais do Whisper da OpenAI, licença MIT). Descarga
automática pelo próprio `faster_whisper.WhisperModel(..., download_root=...)`
na primeira utilização; ficam em cache local dentro de `models/faster-whisper`
no layout `huggingface_hub` (`models--Systran--faster-whisper-<tamanho>/`).

| Modelo | Ficheiro de pesos | URL de origem | sha256 |
|---|---|---|---|
| `small` (fallback) | `models/faster-whisper/models--Systran--faster-whisper-small/snapshots/536b0662742c02347bc0e980a01041f333bce120/model.bin` | `https://huggingface.co/Systran/faster-whisper-small/resolve/main/model.bin` | `3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671` |
| `medium` (preferido) | `models/faster-whisper/models--Systran--faster-whisper-medium/snapshots/08e178d48790749d25932bbc082711ddcfdfbc4f/model.bin` | `https://huggingface.co/Systran/faster-whisper-medium/resolve/main/model.bin` | `9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae` |
| `large-v3` (não usado — ver nota) | `models/faster-whisper/models--Systran--faster-whisper-large-v3/snapshots/edaa852ec7e145841d8ffdb056a99866b5f0a478/model.bin` | `https://huggingface.co/Systran/faster-whisper-large-v3/resolve/main/model.bin` | `69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1` |
| `large-v3-turbo` (medido na T9, não adotado — ver nota) | `models/faster-whisper/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/snapshots/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf/model.bin` | `https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo/resolve/main/model.bin` | `e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da` |

**Nota sobre o `large-v3` (2,88 GB de pesos; a pasta `models/faster-whisper/blobs`
inteira ocupa 4,8 GB).** Nenhum código deste repositório o carrega: a D39 só
autoriza subir para `large-v3` **se** o WER da D7 não bater o limiar, e essa
medição ainda não existe. Foi descarregado durante o diagnóstico da T3 (ao
reproduzir a alucinação das frases curtas em vários tamanhos de modelo) e ficou
aqui registado porque a D14e pede o URL e o sha256 de **cada** modelo que entra
em `models/`, usado ou não.

Medido na T3, em cinco WAV sintéticos de ~0,72 s da mesma frase, com o mesmo
`initial_prompt` de vocabulário: `medium` acertou "horas" em 4, `large-v3` em 2
e `small` em 1 — ou seja, **subir de `medium` para `large-v3` não resolve o
problema das frases curtas isoladas** e não há razão medida para o manter. Este
repositório não apaga nada por sua iniciativa: a pasta
`models--Systran--faster-whisper-large-v3/` e os respetivos blobs podem ser
apagados à mão. Se a D7 vier a exigir o `large-v3`,
o download volta a ser automático a partir do URL acima.

O `small` já tinha sido descarregado antes;
o `medium` foi descarregado nesta task (T3), que é a que o usa por omissão
(fallback automático para `small` em `scripts/transcrever_ficheiro.py` se o
`medium` não carregar ou não transcrever no device pedido). Cada pasta de
snapshot inclui também `config.json`, `tokenizer.json` e `vocabulary.txt` —
ficheiros pequenos, não-executáveis, do mesmo download; não têm hash listado
aqui porque não afetam a integridade do modelo em si (o Hugging Face já
verifica o conteúdo do repositório pelo próprio Git/LFS no lado do servidor).

**Nota sobre o `large-v3-turbo` (T9, S8/D65; 1,51 GB de pesos).** Repositório de
origem `mobiuslabsgmbh/faster-whisper-large-v3-turbo` (licença MIT, conversão
CTranslate2 do `openai/whisper-large-v3-turbo`), já listado em
`faster_whisper.utils._MODELS` da versão instalada — nenhuma dependência nova.
Descarregado com o comando exato da S8:

```
.venv\Scripts\python -c "import faster_whisper; faster_whisper.WhisperModel('large-v3-turbo', device='cuda', compute_type='float16', download_root='models/faster-whisper')"
```

**Está nas duas listas fechadas** (`MODELOS_STT_PERMITIDOS` em `jarvis/app.py`,
`MODELOS_PERMITIDOS` em `scripts/transcrever_ficheiro.py`) para poder ser
medido, mas **NÃO é o modelo por omissão**: o preferido continua a ser o
`medium`. A decisão saiu dos números da D53, não do gosto — A/B controlado sobre
os MESMOS WAV (D66, ponto 7), `language='pt'` fixo nas duas pernas, quatro
combinações da D53:

| combinação | acerto de intenção `medium` | acerto de intenção `large-v3-turbo` |
|---|---|---|
| PT sem prefixo | **11/20 (55,0%)** | 10/20 (50,0%) |
| PT com prefixo | 10/20 (50,0%) | 10/20 (50,0%) |
| EN sem prefixo | 10/20 (50,0%) | 10/20 (50,0%) |
| EN com prefixo | 10/20 (50,0%) | 10/20 (50,0%) |

A regra de adoção exigia acerto **igual ou melhor nas quatro** combinações. Falha
em PT sem prefixo por uma linha, e é justamente a linha que mais custa: a frase
4, «abre a pasta do jarvis», que o `medium` transcreve «Abre a pasta dos Javis.»
(a lista branca ainda a apanha) e o `turbo` transcreve «Hava a pasta do Javis.»
(vai como texto para o Claude Code) — ou seja, o turbo perde a **única** ação
local que essa combinação acertava. Latência e VRAM passavam folgadamente
(p50 335-397 ms contra 424-576 ms do `medium`; p95 390-745 ms contra 614-1956 ms;
pico de 4503 MiB usados no GPU inteiro, ~2,4 GB do processo, com 11548 MiB ainda
livres), mas a ordem de corte da D53 item 3 põe o acerto primeiro. Segundo
precedente, depois do `large-v3` simples, de que **maior não é melhor** nesta
amostra de frases curtas isoladas.

Não foi preciso a válvula `int8_float16` da S8: a VRAM nunca apertou.

## Palavra de ativação — openWakeWord (D38, TECHNOLOGY.md S2)

Descarregado na T6, que é a que liga o `oww` do RealtimeSTT. Fonte: *release
assets* do repositório `dscripka/openWakeWord` (v0.5.1), os mesmos URLs que o
próprio pacote usa (`openwakeword.MODELS`, `openwakeword.FEATURE_MODELS`,
`openwakeword.VAD_MODELS`). Código Apache-2.0; **os modelos pré-treinados estão
sob CC-BY-NC-SA-4.0** (não comercial) — aceitável aqui por ser uso pessoal, e
nunca redistribuídos: `models/` está no `.gitignore`.

Comando exato usado para os descarregar para dentro do repositório:

```
.venv\Scripts\python -c "import openwakeword.utils as u; u.download_models(['hey_jarvis'], target_directory='models/openwakeword')"
```

| Ficheiro | URL de origem | sha256 |
|---|---|---|
| `models/openwakeword/hey_jarvis_v0.1.onnx` | `https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/hey_jarvis_v0.1.onnx` | `94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb` |
| `models/openwakeword/hey_jarvis_v0.1.tflite` | `https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/hey_jarvis_v0.1.tflite` | `14bff778604985e1b5c19f0f7bbe477a69cf281d8db34b232b3b972411f710e2` |
| `models/openwakeword/melspectrogram.onnx` | `https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.onnx` | `ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f` |
| `models/openwakeword/melspectrogram.tflite` | `https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.tflite` | `96fa0adccb6e8cf95cb14465409a1a2898ee4a96a85bb9ed3c7eb0e68bf163e8` |
| `models/openwakeword/embedding_model.onnx` | `https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.onnx` | `70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f` |
| `models/openwakeword/embedding_model.tflite` | `https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.tflite` | `c0aea21eb84a4ce90a08c870da41b7a7173b45269e6a3207c71d67c40f3a59d8` |
| `models/openwakeword/silero_vad.onnx` (não usado pelo jarvis) | `https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/silero_vad.onnx` | `a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28` |

`jarvis/app.py` aponta explicitamente para estes três ficheiros
(`hey_jarvis_v0.1.onnx`, `melspectrogram.onnx`, `embedding_model.onnx`), em
`inference_framework="onnx"`; os `.tflite` vêm no mesmo download e não são
carregados. O `silero_vad.onnx` também não: o VAD em uso é o do RealtimeSTT
(WebRTC + Silero do `torch.hub`).

**Segunda cópia, dentro do `.venv` (não é escolha do jarvis).** Quando o
`wakeword_backend="oww"` do RealtimeSTT arranca, ele chama sempre
`openwakeword.utils.download_models()` sem argumentos, que descarrega **todos**
os modelos pré-treinados (`alexa`, `hey_mycroft`, `hey_rhasspy`, `timer`,
`weather`, além do `hey_jarvis`) para dentro do pacote instalado
(`.venv/Lib/site-packages/openwakeword/resources/models/`, ~18 MB). Isso
acontece na primeira execução, é do mesmo *release* v0.5.1 e dos mesmos URLs da
tabela acima, e fica dentro do `.venv` — que o `.gitignore` também apanha e que
se apaga com a pasta. O jarvis não carrega nenhum desses modelos extra: passa
sempre o caminho do `hey_jarvis` de `models/openwakeword/`. Fica registado aqui
para ninguém descobrir tarde que a instalação traz mais modelos do que os que
este repositório escolheu.
