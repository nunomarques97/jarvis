# Roadmap

Planned work that is not implemented yet. Every option listed here is free and runs locally; each one still has to be measured on the Sponsor's own voice before it is adopted, and nothing is installed without the Sponsor's approval.

## European Portuguese

Goal: talk to jarvis in European Portuguese (pt-PT) as well as in English. Parts of Portuguese already exist (`[ouvido] lingua = "pt"`, the Piper pt-PT voice, the wake-word trainer), but it is not usable day to day yet. The earlier measurement with the Sponsor's voice gave a word error rate of 38-46% in Portuguese, which is too high for dictation.

What is needed, and the options to evaluate:

1. **A better pt-PT transcription engine.** The engine has to beat today's 38-46% on the same Portuguese recordings, with the same harness (`scripts/avaliar_voz.py`), without a slower first reply.
   - NVIDIA Parakeet TDT 0.6B v3, the current engine, plus the English accent adaptation (phrase boosting and a learned correction lexicon) carried over to Portuguese: [model card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3), [NeMo word boosting](https://docs.nvidia.com/nemo-framework/user-guide/latest/nemotoolkit/asr/asr_customization/word_boosting.html).
   - NVIDIA Canary 1B v2, a larger multilingual model that includes Portuguese: [model card](https://huggingface.co/nvidia/canary-1b-v2).
   - Whisper large-v3 through faster-whisper, already supported by `[ouvido] motor`: [model card](https://huggingface.co/openai/whisper-large-v3), [faster-whisper](https://github.com/SYSTRAN/faster-whisper).
2. **The "boas jarvis" wake word.** The trainer exists (`scripts/treinar_ativacao.py`, see `docs/MODELOS.md`); it still needs the Sponsor's training and evaluation recordings, and then a threshold measured on the Sponsor's voice. Option: openWakeWord, already used for "hey jarvis": [openWakeWord](https://github.com/dscripka/openWakeWord).
3. **A pt-PT voice.** Today's Portuguese voice is Piper `pt_PT-tugão-medium`; it has to be compared with the English voice on naturalness and time to first audio. Options: the Piper pt-PT voices: [piper-voices](https://huggingface.co/rhasspy/piper-voices/tree/main/pt/pt_PT). Kokoro's Portuguese voices are Brazilian, so they only count if the Sponsor accepts that accent: [Kokoro voices](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md).
4. **A bilingual interpreter.** jarvis would need to accept either language in the same session: pick the language of each sentence (the pt/en rule in `jarvis/lingua.py` exists but is off, because it lowered Portuguese intent accuracy when measured), keep the recap and the spoken reply in the language that was spoken, and extend the interpreter's golden set with pt-PT cases. Option: the current Qwen3 model in Ollama, which is multilingual: [Qwen3 announcement](https://qwenlm.github.io/blog/qwen3/), [Ollama qwen3](https://ollama.com/library/qwen3).

Order: engine first (it decides whether the rest is worth doing), then wake word, voice and the bilingual interpreter. Measurements need new Portuguese recordings by the Sponsor; they are never simulated with a synthetic voice.
