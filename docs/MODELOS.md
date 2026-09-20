# Modelos descarregados para `models/` (D14e)

`models/` está no `.gitignore`: os ficheiros binários nunca entram no Git. Este
ficheiro é o registo versionado exigido pela D14e — URL de origem e sha256 de
cada modelo, para qualquer pessoa (ou sessão futura) reconstruir o mesmo
ambiente sem adivinhar de onde os ficheiros vieram. Sem caminhos absolutos do
disco do Sponsor: todos os caminhos abaixo são relativos à raiz do repositório.

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
repositório não apaga nada por sua iniciativa: a proposta de apagar a pasta
`models--Systran--faster-whisper-large-v3/` e os respetivos blobs fica no
relatório da task, para o Sponsor decidir. Se a D7 vier a exigir o `large-v3`,
o download volta a ser automático a partir do URL acima.

O `small` já tinha sido descarregado pela T1 (`docs/forja/DECISIONS.md` D42/D43);
o `medium` foi descarregado nesta task (T3), que é a que o usa por omissão
(fallback automático para `small` em `scripts/transcrever_ficheiro.py` se o
`medium` não carregar ou não transcrever no device pedido). Cada pasta de
snapshot inclui também `config.json`, `tokenizer.json` e `vocabulary.txt` —
ficheiros pequenos, não-executáveis, do mesmo download; não têm hash listado
aqui porque não afetam a integridade do modelo em si (o Hugging Face já
verifica o conteúdo do repositório pelo próprio Git/LFS no lado do servidor).

## Palavra de ativação — openWakeWord (D38, TECHNOLOGY.md S2)

Ainda não descarregado: a T3 não usa a palavra de ativação (só gera/transcreve
ficheiro e verifica o microfone). Fica para a task que ligar o `oww` do
RealtimeSTT; a entrada correspondente junta-se a este ficheiro nessa altura.
