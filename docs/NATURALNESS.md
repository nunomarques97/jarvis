# Making jarvis sound natural

jarvis works, but it does not yet feel like talking to someone. In real use the Sponsor noticed:

- fixed, robotic phrases ("I got the request, but not the project. Which project?", "Claude says:", "Let me check.");
- questions it did not need to ask;
- no way to interrupt it while it speaks;
- listening rules to manage (the key, "hey jarvis", the 8-second follow-up window, the sounds);
- pauses of 1-2 s before local replies and 5-15 s before answers to general questions;
- transcription errors on his English (he is a Portuguese speaker).

This document looks at how the most natural voice assistants handle each of these, checks what can run locally and for free on this PC, and turns the result into a plan with targets measured on the Sponsor's own voice. Sources are primary (vendor documentation, project repositories and official announcements) and were read in September 2026.

Everything planned here stays within the project's principles: local, free, no audio leaves the PC, no buy or sell orders by voice, and nothing is installed without the Sponsor's approval.

## The current pipeline

| Stage | Today | Runs on |
|---|---|---|
| Wake word | openWakeWord "hey jarvis", or a push-to-talk key | CPU |
| Voice activity and end of turn | energy/VAD with a fixed 0.6 s final silence | CPU |
| Speech to text | NVIDIA Parakeet TDT 0.6B v3 with accent adaptation | CPU |
| Understanding | qwen3:8b through Ollama, about 15 intents | GPU |
| Replies | fixed phrase tables; general questions through a headless `claude -p` | - |
| Voice | Kokoro `bm_fable`, starts speaking while it synthesises | CPU |
| Follow-up | an 8-second window after each reply, marked with soft sounds | - |

## Hardware budget

The GPU is an RTX 5060 Ti with 16 GB. qwen3:8b already takes about 5.5 GB, and another project loads qwen3:14b into the same Ollama server, which takes most of what is left. Any new model therefore has to run on the CPU (an i5-14400F with 32 GB of RAM) or be small enough not to push either language model out of VRAM. Swapping a model back into VRAM costs seconds, which is exactly the kind of pause this plan removes, so "local" here means small CPU models: Silero VAD (about 2 MB, under 1 ms per 30 ms chunk on one CPU thread, [Silero VAD](https://github.com/snakers4/silero-vad)) and Smart Turn v3 (8 MB ONNX, about 10 ms on a CPU, [smart-turn](https://github.com/pipecat-ai/smart-turn)). The headset is a closed-back Razer BlackShark V2 Pro, which matters for echo (see below).

## Techniques

Each technique below says how ChatGPT voice, Gemini Live, Alexa+, Pipecat, LiveKit Agents and Home Assistant Assist approach it, whether it runs locally and for free here, the expected gain, the cost, and what the plan does with it. ChatGPT voice and Gemini Live are consumer apps whose internals are not published; where that is the case, the developer APIs from the same companies (the OpenAI Realtime API and the Gemini Live API) are used as the closest primary source, and the text says so.

### Turn-taking and barge-in

How others do it:

- **ChatGPT voice**: you can talk over it and it stops. OpenAI does not publish how the app does this. The OpenAI Realtime API, which exposes the same kind of speech-to-speech model to developers, has server-side voice activity detection with `interrupt_response`: when the user starts speaking, the reply in progress is cancelled ([Realtime VAD](https://developers.openai.com/api/docs/guides/realtime-vad), [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)).
- **Gemini Live**: the Gemini Live API runs voice activity detection on the incoming audio; "when VAD detects an interruption, the ongoing generation is canceled and discarded", and only what was already sent to the client stays in the history ([Live API capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities)).
- **Alexa+**: Amazon's posts describe a more conversational assistant but do not describe how turn-taking or interruption work ([Introducing Alexa+](https://www.aboutamazon.com/news/devices/new-alexa-generative-artificial-intelligence)).
- **Pipecat**: interruptions are on by default and are driven by a "user turn start strategy". Short utterances can be kept from interrupting with `MinWordsUserTurnStartStrategy(min_words=3)` so that "okay" or "yeah" do not stop the bot, and input can be muted while the bot speaks ([Interruptions](https://docs.pipecat.ai/pipecat/fundamentals/interruptions), [User input muting](https://docs.pipecat.ai/guides/fundamentals/user-input-muting)).
- **LiveKit Agents**: "the framework pauses the agent's speech whenever it detects user speech". If the user then says nothing, it is a false interruption and the agent resumes where it left off (`false_interruption_timeout`, `resume_false_interruption`); adaptive interruption handling separates real interruptions from backchannels ([Turn detection and interruptions](https://docs.livekit.io/agents/build/turns/)).
- **Home Assistant Assist**: the voice posts reviewed do not describe interrupting a spoken reply; what they do describe is the Voice Preview Edition's on-device echo cancellation, which keeps the microphone usable while the speaker plays ([Voice Preview Edition](https://www.home-assistant.io/blog/2024/12/19/voice-preview-edition-the-era-of-open-voice/)).

Local and free here: **yes**. The Silero VAD ONNX file is already on disk (it came with openWakeWord) and runs with the installed onnxruntime, so nothing new is installed. While jarvis speaks, the microphone keeps running through the VAD; a speech onset pauses the voice straight away (pause, not stop, as in LiveKit), then the captured phrase decides: real speech stops the reply and becomes the next turn without the wake word; noise, a cough or a backchannel ("yeah", "uh-huh", the Pipecat idea) resumes it.

Expected gain: high. It removes one of the Sponsor's main complaints and makes long answers bearable.
Cost: medium engineering, and one risk: jarvis hearing its own voice. That risk is measured (see echo handling) before it is turned on by default.

### Echo handling

How others do it:

- **ChatGPT voice** and **Gemini Live**: the apps rely on the phone's or browser's echo cancellation; neither company documents the details of its app. The developer APIs assume the client sends clean audio.
- **Alexa+**: Echo devices cancel their own playback in hardware; Amazon's Alexa+ posts do not go into it.
- **Pipecat**: relies on the transport and client (browser WebRTC echo cancellation) and offers optional noise and voice filters that need commercial licence keys ([Krisp VIVA filter](https://docs.pipecat.ai/server/utilities/audio/krisp-viva-filter), [ai-coustics filter](https://docs.pipecat.ai/server/utilities/audio/aic-filter)); it can also mute user input while the bot speaks, which it recommends to "reduce false transcriptions" ([User input muting](https://docs.pipecat.ai/guides/fundamentals/user-input-muting)).
- **LiveKit Agents**: echo cancellation comes from the WebRTC client; enhanced noise and voice cancellation is a LiveKit Cloud feature ([Noise and echo cancellation](https://docs.livekit.io/transport/media/noise-cancellation/)).
- **Home Assistant Assist**: the Voice Preview Edition's XMOS chip does echo cancellation, noise removal and gain control before audio reaches Home Assistant ([Voice Preview Edition](https://www.home-assistant.io/blog/2024/12/19/voice-preview-edition-the-era-of-open-voice/)).

Local and free here: **probably not needed**. The Sponsor uses a closed-back headset, so very little of jarvis's voice reaches the microphone. The plan measures it instead of assuming it: a script plays replies through the headset while recording and counts how often the VAD fires on jarvis's own voice (target 0). Only if leakage is measured would an acoustic echo canceller be added, and that needs a new package and the Sponsor's approval: the WebRTC AEC3 module ([WebRTC audio processing](https://webrtc.googlesource.com/src/+/refs/heads/main/modules/audio_processing/)) or SpeexDSP ([Speex manual](https://www.speex.org/docs/manual/speex-manual/node7.html), [speexdsp-python](https://github.com/xiongyihui/speexdsp-python)).

Expected gain: it is what makes barge-in safe. Cost: none with the headset; a new package and some CPU if speakers are ever used.

### Semantic end-of-turn detection

How others do it:

- **ChatGPT voice**: not published for the app. The Realtime API offers `semantic_vad`, which ends the turn "when the model believes based on the words said by the user that they have completed their utterance", with an `eagerness` setting; plain `server_vad` waits for a fixed silence ([Realtime VAD](https://developers.openai.com/api/docs/guides/realtime-vad)).
- **Gemini Live**: the Live API's automatic activity detection has start and end-of-speech sensitivities and a `silence_duration_ms` setting ([Live API capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities)).
- **Alexa+**: not described in Amazon's posts.
- **Pipecat**: since v0.0.102 the default turn-end strategy is Smart Turn v3, an audio model that judges whether the user finished, run locally with ONNX after a short VAD silence (0.2 s recommended) ([Smart Turn overview](https://docs.pipecat.ai/server/utilities/turn-detection/smart-turn-overview), [Speech input and turn detection](https://docs.pipecat.ai/guides/learn/speech-input)). The model is BSD-2, 8 MB, supports 23 languages including English and Portuguese ([smart-turn](https://github.com/pipecat-ai/smart-turn), [model card](https://huggingface.co/pipecat-ai/smart-turn-v3)).
- **LiveKit Agents**: a turn detector model on top of VAD; `v1-mini` runs locally on CPU for free, the full model runs in LiveKit Cloud ([Turn detector](https://docs.livekit.io/agents/build/turns/turn-detector/), [model](https://huggingface.co/livekit/turn-detector)).
- **Home Assistant Assist**: silence-based end of speech on the satellite; no semantic model is documented.

Local and free here: **yes**, with Smart Turn v3 on the CPU (the Sponsor approved the 8 MB download). Its audio features come from the Whisper feature extractor already inside faster-whisper, so no `transformers` package is needed. Without the file, jarvis keeps today's fixed 0.6 s silence.

Expected gain: fewer cut-offs when the Sponsor pauses to find an English word (common for a non-native speaker), and a shorter wait when the sentence is clearly finished. Cost: about 10 ms of CPU per check and an 8 MB model. It is enabled by default only if the Sponsor's own recordings show fewer cut-ins without a longer median wait.

### End-to-end streaming

How others do it:

- **ChatGPT voice** and **Gemini Live**: one speech-to-speech model streams audio out as it is generated. OpenAI reported audio responses "in as little as 232 milliseconds, with an average of 320 milliseconds" for GPT-4o ([Hello GPT-4o](https://openai.com/index/hello-gpt-4o/)); the Gemini Live API streams audio over a WebSocket session ([Live API](https://ai.google.dev/gemini-api/docs/live)).
- **Alexa+**: Amazon routes each request to the model that best balances accuracy and speed "for a crisp, conversational experience" ([Alexa+ technology](https://www.aboutamazon.com/news/devices/new-alexa-tech-generative-artificial-intelligence)).
- **Pipecat**: the language model streams tokens; the voice aggregates them into sentences and speaks each one as soon as it is complete, "end-to-end latency often under 200 ms" for the voice step ([Text to speech](https://docs.pipecat.ai/guides/learn/text-to-speech)).
- **LiveKit Agents**: preemptive generation starts the language model before the end of turn is confirmed, to cut perceived latency ([Speech and audio](https://docs.livekit.io/agents/build/audio/)).
- **Home Assistant Assist**: text-to-speech was overhauled to stream, because waiting for a whole LLM answer "meant a lot of waiting", especially with local voices like Piper ([Voice chapter 11](https://www.home-assistant.io/blog/2025/10/22/voice-chapter-11/)).

Local and free here: **yes**. General questions can stream through `claude -p --output-format stream-json --verbose --include-partial-messages` ([Claude Code headless mode](https://code.claude.com/docs/en/headless)); `--bare` is not used because it needs a paid API key. Kokoro already speaks sentence by sentence ([Kokoro](https://github.com/hexgrad/kokoro)). The spoken-reply filter has to work on a stream: nothing after a "Sources" heading, no code, paths or markup, the same length caps.

Expected gain: the largest single latency cut. General answers go from 5-15 s of silence to the first sentence in a few seconds. Cost: no new package; careful work on the filter and on cancellation.

### LLM-generated replies with a short persona

How others do it:

- **ChatGPT voice** and **Gemini Live**: every reply is generated; the developer APIs take system instructions and a voice ([Voice agents](https://developers.openai.com/api/docs/guides/voice-agents), [Live API capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities)).
- **Alexa+**: generated replies with a kept personality; users can pick one of four styles (Brief, Chill, Sweet, Sassy) that "shape her tone without changing her capabilities" ([Introducing Alexa+](https://www.aboutamazon.com/news/devices/new-alexa-generative-artificial-intelligence), [Alexa+ technology](https://www.aboutamazon.com/news/devices/new-alexa-tech-generative-artificial-intelligence)).
- **Pipecat** and **LiveKit Agents**: the language model writes every reply from a system prompt ([Pipecat LLM inference](https://docs.pipecat.ai/guides/learn/llm), [LiveKit Agents](https://github.com/livekit/agents)).
- **Home Assistant Assist**: the built-in agent answers with fixed templates and works without AI; an LLM agent, local through Ollama, is optional ([Voice chapter 11](https://www.home-assistant.io/blog/2025/10/22/voice-chapter-11/), [Ollama integration](https://www.home-assistant.io/integrations/ollama/)).

Local and free here: **yes, in part**. qwen3:8b is already loaded. The plan is hybrid: anything that decides safety (the project, the exact text to send, yes/abort, refusals) stays deterministic; qwen3:8b only words social and clarification replies from a one-line persona, with `think` off and a short token limit ([Ollama chat API](https://docs.ollama.com/api/chat)), and falls back to a template if it takes more than about 1.2 s. Every fixed phrase gets two to four variants and never repeats twice in a row.

Expected gain: medium to high for how natural it sounds; it removes the "same sentence every time" feeling. Cost: some GPU time on a model that is already loaded, and a latency budget that must hold.

### Context assumptions

How others do it:

- **ChatGPT voice** and **Gemini Live**: the conversation history is the context; the Gemini Live API keeps the session history, including what was said before an interruption ([Live API capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities)).
- **Alexa+**: remembers facts you tell it and "remembers the context" across devices ([Introducing Alexa+](https://www.aboutamazon.com/news/devices/new-alexa-generative-artificial-intelligence)).
- **Pipecat** and **LiveKit Agents**: the application keeps a chat context and conversation state ([Pipecat Flows](https://docs.pipecat.ai/guides/features/pipecat-flows), [LiveKit turns](https://docs.livekit.io/agents/build/turns/)).
- **Home Assistant Assist**: assumes the room of the device you spoke to, so "turn off the lights" needs no room ([Voice chapter 8](https://www.home-assistant.io/blog/2024/12/19/voice-chapter-8-assist-in-the-home/)); it keeps the conversation open when the LLM agent's reply is a question ([Voice chapter 10](https://www.home-assistant.io/blog/2025/06/25/voice-chapter-10/)).

Local and free here: **yes**, it is only state. A dictation with no project named, said within a few minutes of the last project used, assumes that project and names it in the recap; a status request without a project uses the last one and says which. When a general answer ends in a question, the next reply continues the general question. This changes the rule that a project is never guessed; it stays safe because the recap and "yes" remain before anything is sent.

Expected gain: fewer unnecessary questions, the second most common complaint. Cost: small.

### Light confirmations

How others do it:

- **Home Assistant Assist**: confirms only what matters. A command whose effect you can see in the same room gets a short beep instead of a sentence ("non-verbal confirmations"); protected actions such as unlocking a door get "Are you sure?"; a missing detail gets a short follow-up ("For how long?") ([Voice chapter 11](https://www.home-assistant.io/blog/2025/10/22/voice-chapter-11/), [Voice chapter 10](https://www.home-assistant.io/blog/2025/06/25/voice-chapter-10/)).
- **LiveKit Agents** and **Pipecat**: confirmation is left to the application, typically inside a tool call or a flow step ([Pipecat Flows](https://docs.pipecat.ai/guides/features/pipecat-flows), [LiveKit Agents](https://github.com/livekit/agents)).
- **ChatGPT voice**, **Gemini Live** and **Alexa+**: no confirmation policy is published for voice; the OpenAI Realtime API lets the application turn off automatic responses to check input first ([Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)).

Local and free here: **yes**. jarvis keeps "yes" before anything is sent to a project, but the recap becomes one short sentence ending in a light question ("Add tests to the login page, for your web app, okay?"); a long prompt is summarised in a few words and the exact text is always on screen. The yes/abort vocabulary and the exact-match rule stay.

Expected gain: medium; each dictation becomes shorter to confirm. Cost: small, plus tests for the edge cases ("yes but...", mixed "yes abort").

### Fillers and backchannels

How others do it:

- **LiveKit Agents**: background audio can play "thinking sounds" while the agent works ([Speech and audio](https://docs.livekit.io/agents/build/audio/)); adaptive interruption handling tells backchannels from real interruptions ([Turn detection and interruptions](https://docs.livekit.io/agents/build/turns/)).
- **Pipecat**: ignores short backchannels with a minimum word count, or with Krisp's interruption prediction model ([Interruptions](https://docs.pipecat.ai/pipecat/fundamentals/interruptions)).
- **Gemini Live**: "proactive audio" lets the model decide not to respond when the input is not relevant, and "affective dialog" adapts its style to the user's tone (both not supported on every Live model) ([Live API capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities)).
- **Home Assistant Assist**: a beep instead of a sentence when words add nothing ([Voice chapter 11](https://www.home-assistant.io/blog/2025/10/22/voice-chapter-11/)).
- **ChatGPT voice** and **Alexa+**: not documented.

Local and free here: **yes**. The fixed "Let me check." becomes a short, varied acknowledgement spoken only when an answer is not ready within about a second; fast answers get no filler at all. Backchannels while jarvis speaks ("yeah", "uh-huh") do not interrupt it. The existing soft sounds stay, configurable.

Expected gain: medium; silence feels like a failure, and a filler on every answer feels robotic. Cost: small.

### Prosody and speaking rate

How others do it:

- **ChatGPT voice**: not published for the app; the Realtime API has an output `speed` setting and voice selection ([Realtime client events](https://developers.openai.com/api/reference/resources/realtime/client-events)).
- **Gemini Live**: native-audio models are described as having "better pacing, voice naturalness, verbosity, and mood" ([Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing)); affective dialog adapts to the user's tone ([Live API capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities)).
- **Alexa+**: personality styles change tone; each model is tuned to Alexa's personality ([Alexa+ technology](https://www.aboutamazon.com/news/devices/new-alexa-tech-generative-artificial-intelligence)).
- **Pipecat** and **LiveKit Agents**: prosody comes from the chosen voice engine; both offer pronunciation and text transforms before speech ([Text to speech](https://docs.pipecat.ai/guides/learn/text-to-speech), [Speech and audio](https://docs.livekit.io/agents/build/audio/)).
- **Home Assistant Assist**: the local Piper voices, with new voices added over time ([Voice chapter 11](https://www.home-assistant.io/blog/2025/10/22/voice-chapter-11/)).

Local and free here: **yes, within Kokoro's limits**. Kokoro takes a `speed` argument ([Kokoro](https://github.com/hexgrad/kokoro), [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)). An optional speaking rate (0.8-1.3, default unchanged) lets the Sponsor pick from samples. Short sentences, contractions and punctuation also shape Kokoro's prosody, which the phrasing work covers. Emotion and tone that follow the user's voice are only available in the cloud models below.

Expected gain: small to medium. Cost: small.

## Summary

| Technique | Local and free here | Expected gain | Cost | In the plan |
|---|---|---|---|---|
| Turn-taking and barge-in | yes (Silero VAD on disk) | high | medium | yes |
| Echo handling | not needed with the headset; measured | enables barge-in | none, or a new package if measured leakage | measured first |
| Semantic end-of-turn | yes (Smart Turn v3, 8 MB, CPU) | medium | small model download (approved) | yes, if it wins on the Sponsor's recordings |
| End-to-end streaming | yes (`claude -p` stream-json, Kokoro) | high | medium | yes |
| Generated replies, short persona | partly (qwen3:8b, safety stays fixed) | medium-high | GPU time on a loaded model | yes |
| Context assumptions | yes | high | small | yes |
| Light confirmations | yes | medium | small | yes |
| Fillers and backchannels | yes | medium | small | yes |
| Prosody and speaking rate | partly (Kokoro speed) | small-medium | small | yes |

## Numeric targets

All targets are measured with the Sponsor's real voice in a guided session of 20 exchanges (local replies, project status and dictation, general questions including one whose answer ends in a question, a follow-up without the wake word, an interruption attempt, a missing-project case). Synthetic voices and WAV files never count toward these numbers. The session is a Sponsor step: once as a baseline before the audio changes, and again at the end. Until the baseline is recorded, it is "not measured yet".

Latency is measured from the last voiced audio chunk (the real end of the Sponsor's speech, not the moment the silence timer closes) to the first audio jarvis plays.

| Metric | Target |
|---|---|
| Exchanges the Sponsor marks as natural | at least 16 of 20 |
| Local reply latency | median at most 1.0 s, 95th percentile at most 1.6 s |
| General answer, first answer sentence | median at most 3.5 s, 95th percentile at most 6 s |
| General answer, first sound of any kind | at most 1.2 s |
| Times the Sponsor had to repeat himself | at most 1 |
| Wake words said without needing them | 0 |
| Unnecessary questions from jarvis | at most 1 |
| Interruption latency, speech onset to voice stopped | 95th percentile under 300 ms |
| False interruptions (noise, cough, backchannel) | 0 |
| Self-triggers (jarvis interrupting itself through the headset) | 0 |
| End of turn: cut-ins inside a phrase | fewer than with the fixed 0.6 s silence, with no higher median wait |

## Planned changes and the metric each moves

In order, so the Sponsor feels improvement early: the four small fixes first, then the measuring harness (so a baseline exists), then the cheap high-gain changes, then the harder audio work. Each audio change carries its own measurement with the Sponsor's voice.

| # | Change | Metric it should move |
|---|---|---|
| 1 | Stop speaking the "Claude says:" prefix; the label stays on screen and in the log | natural exchanges |
| 2 | While asleep, "hey jarvis" followed by a request wakes jarvis and does the request | repeats; wake words without need |
| 3 | Discover the git repositories in a configurable folder and name the known projects when asking which one | unnecessary questions; repeats |
| 4 | When a general answer ends in a question, the next reply continues it | unnecessary questions |
| 5 | This research and plan | - |
| 6 | Guided 20-exchange session: script, harness and report; log the last voiced chunk of each sentence | makes every metric measurable |
| 7 | Stream general answers and speak the first sentence while Claude is still writing; varied acknowledgement only when slow | general answer first sentence and first sound |
| 8 | Cut the pause before local replies: per-stage timings, a deterministic fast path for common requests | local reply latency |
| 9 | Natural, varied phrasing with a short persona; optional speaking rate | natural exchanges |
| 10 | Lighter recap and the last-project assumption, still with "yes" before sending | unnecessary questions; natural exchanges |
| 11 | Barge-in with the Silero VAD, pause then stop or resume; self-trigger measurement | interruption latency; false interruptions; self-triggers |
| 12 | Semantic end of turn with Smart Turn v3, fallback to the fixed silence | cut-ins; local reply latency; repeats |
| 13 | Conversation mode: keep listening across consecutive exchanges, close on silence or "that's all" | wake words without need; natural exchanges |

## Cloud speech-to-speech (information only)

These models hear and speak directly, without separate transcription and voice steps. They are the reference for natural conversation, and they are **not planned**: the Sponsor chose a 100% free local setup, and with them the Sponsor's audio would leave the PC, which goes against the project's principles. The numbers below are here only so the Sponsor can reconsider with facts.

Published prices (September 2026):

| Model | Audio input | Audio output | Source |
|---|---|---|---|
| Gemini Live (`gemini-3.8-live`, `gemini-3.1-flash-live-preview`) | $3.00 per 1M tokens, about $0.005 per minute | $12.00 per 1M tokens, about $0.018 per minute | [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing) |
| OpenAI `gpt-realtime` | $32.00 per 1M tokens ($0.40 cached) | $64.00 per 1M tokens | [gpt-realtime](https://developers.openai.com/api/docs/models/gpt-realtime), [OpenAI pricing](https://developers.openai.com/api/docs/pricing) |
| OpenAI `gpt-realtime-mini` | $10.00 per 1M tokens ($0.30 cached) | $20.00 per 1M tokens | [gpt-realtime-mini](https://developers.openai.com/api/docs/models/gpt-realtime-mini), [OpenAI pricing](https://developers.openai.com/api/docs/pricing) |

Notes on the prices: Gemini counts 25 audio tokens per second. OpenAI counts 1 token per 100 ms of user audio and 1 token per 50 ms of assistant audio, and every turn re-bills the conversation so far as input ([Realtime costs](https://developers.openai.com/api/docs/guides/realtime-costs)). The `gpt-realtime-mini` model page lists text prices only; the audio prices above are the ones the pricing page lists for the current mini tier (`gpt-realtime-2.1-mini`). The Gemini free tier exists, but its data is "used to improve our products", which rules it out for private conversations.

Assumptions of the estimate for the Sponsor's use:

- about 40 exchanges a day, 30 days a month: 1,200 exchanges;
- about 5 s of the Sponsor speaking and 8 s of the assistant speaking per exchange: roughly 100 minutes of input and 160 minutes of output audio a month;
- conversations of about 5 exchanges, so each turn re-sends on average two earlier exchanges as context;
- text instructions, tools and web search not counted (Gemini includes 5,000 free search requests a month).

| Model | Audio only | With context re-billed | Monthly estimate |
|---|---|---|---|
| Gemini Live | about $3.40 (100 min × $0.005 + 160 min × $0.018) | about $6 (about 520 more input minutes) | **about $3-6** |
| OpenAI `gpt-realtime` | about $14.20 (60k input tokens, 192k output tokens) | $14.40 if the context is cached, about $30 if not (about 500k context tokens) | **about $15-30** |
| OpenAI `gpt-realtime-mini` | about $4.40 | $4.60 cached, about $9.50 uncached | **about $5-10** |

Why it is not planned: the Sponsor chose free and local; the audio of every conversation would be sent to a cloud provider; it would add a paid account and an API key; and the voice-control safety rules (recap, "yes" before sending, no orders by voice) would have to be rebuilt around a model that answers on its own. The local plan above goes after the same naturalness levers (streaming, barge-in, semantic end of turn, generated phrasing) at no cost.

## What needs the Sponsor

- **Measurements with his voice**: the baseline session once the harness exists, the self-trigger and interruption measurement with barge-in, and the final session. The tasks deliver the script and the harness and say exactly what to do.
- **Downloads**: only the Smart Turn v3 ONNX file (8 MB, BSD-2), already approved; it is enabled by default only if his recordings show a gain.
- **Only if measured**: an echo canceller package, if the self-trigger measurement shows leakage through the headset.
- **Cloud speech-to-speech**: only if the Sponsor decides to reconsider the free-and-local choice.

## Sources

- OpenAI: [Realtime VAD](https://developers.openai.com/api/docs/guides/realtime-vad), [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations), [Realtime client events](https://developers.openai.com/api/reference/resources/realtime/client-events), [Realtime costs](https://developers.openai.com/api/docs/guides/realtime-costs), [Voice agents](https://developers.openai.com/api/docs/guides/voice-agents), [Pricing](https://developers.openai.com/api/docs/pricing), [gpt-realtime](https://developers.openai.com/api/docs/models/gpt-realtime), [gpt-realtime-mini](https://developers.openai.com/api/docs/models/gpt-realtime-mini), [Hello GPT-4o](https://openai.com/index/hello-gpt-4o/), [ChatGPT voice mode FAQ](https://help.openai.com/en/articles/8400625-voice-mode-faq)
- Google: [Gemini Live API](https://ai.google.dev/gemini-api/docs/live), [Live API capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities), [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing)
- Amazon: [Introducing Alexa+](https://www.aboutamazon.com/news/devices/new-alexa-generative-artificial-intelligence), [Alexa+ technology](https://www.aboutamazon.com/news/devices/new-alexa-tech-generative-artificial-intelligence)
- Pipecat: [repository](https://github.com/pipecat-ai/pipecat), [Speech input and turn detection](https://docs.pipecat.ai/guides/learn/speech-input), [Interruptions](https://docs.pipecat.ai/pipecat/fundamentals/interruptions), [User input muting](https://docs.pipecat.ai/guides/fundamentals/user-input-muting), [Text to speech](https://docs.pipecat.ai/guides/learn/text-to-speech), [LLM inference](https://docs.pipecat.ai/guides/learn/llm), [Pipecat Flows](https://docs.pipecat.ai/guides/features/pipecat-flows), [Smart Turn overview](https://docs.pipecat.ai/server/utilities/turn-detection/smart-turn-overview), [smart-turn](https://github.com/pipecat-ai/smart-turn), [Smart Turn v3 model](https://huggingface.co/pipecat-ai/smart-turn-v3), [Krisp VIVA filter](https://docs.pipecat.ai/server/utilities/audio/krisp-viva-filter), [ai-coustics filter](https://docs.pipecat.ai/server/utilities/audio/aic-filter)
- LiveKit: [Agents repository](https://github.com/livekit/agents), [Turn detection and interruptions](https://docs.livekit.io/agents/build/turns/), [Turn detector](https://docs.livekit.io/agents/build/turns/turn-detector/), [turn-detector model](https://huggingface.co/livekit/turn-detector), [Speech and audio](https://docs.livekit.io/agents/build/audio/), [Noise and echo cancellation](https://docs.livekit.io/transport/media/noise-cancellation/)
- Home Assistant: [Voice control](https://www.home-assistant.io/voice_control/), [Voice chapter 8](https://www.home-assistant.io/blog/2024/12/19/voice-chapter-8-assist-in-the-home/), [Voice chapter 10](https://www.home-assistant.io/blog/2025/06/25/voice-chapter-10/), [Voice chapter 11](https://www.home-assistant.io/blog/2025/10/22/voice-chapter-11/), [Voice Preview Edition](https://www.home-assistant.io/blog/2024/12/19/voice-preview-edition-the-era-of-open-voice/), [Ollama integration](https://www.home-assistant.io/integrations/ollama/)
- Local components: [Silero VAD](https://github.com/snakers4/silero-vad), [Kokoro](https://github.com/hexgrad/kokoro), [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M), [openWakeWord](https://github.com/dscripka/openWakeWord), [Ollama chat API](https://docs.ollama.com/api/chat), [Claude Code headless mode](https://code.claude.com/docs/en/headless), [WebRTC audio processing](https://webrtc.googlesource.com/src/+/refs/heads/main/modules/audio_processing/), [Speex manual](https://www.speex.org/docs/manual/speex-manual/node7.html), [speexdsp-python](https://github.com/xiongyihui/speexdsp-python)
