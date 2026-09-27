# A conversation brain for jarvis

Before the brain, jarvis understood each sentence with a local classifier (qwen3:8b through Ollama) that picked one of about 15 intents, and only general questions went to Claude, one headless `claude -p` process per question. The Sponsor decided that jarvis gets a real conversation brain instead: Claude talks with him from start to finish and uses jarvis's functions as tools. The quota each sentence uses from the Sponsor's Claude subscription is accepted; nothing else that costs money is added without his decision.

This document is the research, the design and a description of what was built (next section). It says how the brain is connected, which model it uses, which tools it gets and what they return, the safety rules that stay outside the model, the latency budget, how long conversations are kept bounded, how project notices wait their turn, what happens to the local classifier, what an exchange costs in tokens and the ceiling jarvis guarantees, the targets, and the ordered changes. Sources are primary (Anthropic and Claude Code documentation, the official SDK repository and the MCP specification) and were read in September 2026.

It builds on [NATURALNESS.md](NATURALNESS.md): the ear, end-of-turn detection, barge-in, the voice, the status orb, the sounds, the recap with a spoken "yes", the financial rule and the memory all stay. The brain replaces the part in the middle.

## What was built

All seven planned changes (see the last table) are in place. What one sentence goes through now:

1. **The ear and the fast path** (`jarvis/app.py`). After transcription, regular expressions decide whether the sentence is one of the local cases: be quiet, sleep and wake up, the time and the date, an answer to a pending recap, "that's all", noise or courtesy words heard in the follow-up window, or a money or trading request, which is refused. These never reach the brain or the local model.
2. **The brain turn** (`jarvis/cerebro.py`). Anything else becomes the next turn of one persistent `claude` process, started when jarvis starts and kept warm, with the flags described below and the subscription login. The sentence goes in on stdin as delimited JSON data, together with the date and location, the saved facts on the first turn of a session, the waiting notices and the outcomes of earlier recaps. A sentence heard only through the follow-up window is marked as such, and the brain answers nothing when it was not meant for jarvis.
3. **Speech as it streams.** Each complete sentence of the reply passes the financial rule and the spoken-reply filter and is spoken at once. A short acknowledgement comes only when no sentence is ready after about 1 s, and a second one during a long web search. Be quiet, sleep, a new sentence or barge-in cancel the turn with an interrupt request on stdin, and the process is killed and restarted, seeded from jarvis's own transcript, if the turn does not end within 1 s.
4. **Tools** (`jarvis/cerebro_mcp.py`). The CLI starts jarvis's MCP stdio server from the generated `--mcp-config`; the server calls back into jarvis over the authenticated local IPC. Read tools answer with compact JSON. Effect tools only reserve the existing recap: jarvis drops the rest of that brain turn, speaks the recap itself, and executes only after the Sponsor's spoken "yes"; the outcome goes to the brain as data with the next sentence.
5. **Bounded context.** jarvis rolls over to a fresh session with a summary of at most 120 words and the facts when the reported context passes the limit or after a long pause, and enforces the per-exchange ceilings below.
6. **Notices** (`jarvis/avisos.py`) wait in a bounded queue and never interrupt; the brain gets them as data or through `avisos_pendentes`, and those left are said together after a quiet spell.
7. **The fallback.** When the brain is not available, jarvis says so once and the sentence goes to the local classifier instead (see "Fate of the local qwen3:8b classifier").

Every brain turn writes one log line with the outcome, whether the web was used, the tokens, the context size and the time to the first text, and the time from the last voiced audio to the first spoken sentence; `scripts/sessao_naturalidade.py` reads these lines to check the targets. The automatic tests drive the real `Cerebro` with a fake CLI that speaks the stream-json protocol and never start the real Claude Code, Ollama, a microphone or sound.

Still to do by the Sponsor, not code: one run of `scripts/medir_cerebro.py --com-claude` (it uses quota) to confirm the model choice and the tokens per exchange, the 20-exchange session with his own voice, and the decision on Smart Turn.

## Problems seen in the 2026-09-27 session

In the real naturalness session the Sponsor said jarvis still does not feel natural. The log shows why; the cases are paraphrased here, never quoted.

| What happened | Why | What fixes it |
|---|---|---|
| A social greeting ("how are you doing"), misheard by the transcriber, came back as the status of a project | The classifier has to pick one of its intents, and a misheard greeting looked closest to a status request | The brain gets the sentence as it was heard and answers it as conversation; project status is a tool it calls only when asked |
| A request for help learning to cook, followed by a short question about which dish to start with, was answered as two unrelated questions | Every general question started a new `claude -p` process that only saw a short summary of earlier turns | One persistent conversation: the follow-up arrives as the next turn of the same session |
| Another short follow-up was answered with a question about which project it meant | Anything not recognised as a general question fell through to the project path | No project question unless a tool actually needs a project |
| Status replies read like log lines (a count of sessions per state; a report summary that started with the word "Summary") | Status and reports were spoken from fixed templates | Tools return compact facts and the brain says them in a short natural sentence |
| After a one-word acknowledgement, 17 seconds of silence on a weather question | Cold start of a new process, a web search, and nothing said while it ran | A warm brain from startup, streaming sentence by sentence, a second short acknowledgement after about 8 s of search, a time limit |
| The local classifier took 9 s while another project's qwen3:14b held the GPU | Both models share one 16 GB GPU and one Ollama server | Normal conversation no longer touches the local model, and it is not warmed at startup |
| Late project replies and blocked-run notices were spoken in the middle of the conversation | Notices were spoken as soon as they arrived | A notices queue that never interrupts; the brain mentions them at the right moment |

## Connecting the brain

Three ways to talk to Claude with streaming and tools were compared.

| Option | How it authenticates | Cost to the Sponsor | Streaming, tools, cancel | New packages | Verdict |
|---|---|---|---|---|---|
| Persistent `claude` CLI with `--input-format stream-json --output-format stream-json` | The Claude subscription login, as long as `--bare` is not used | Subscription quota only (already accepted) | Token streaming with `--include-partial-messages`; MCP tools with `--mcp-config`; built-in tools limited with `--tools`; cancel by an interrupt request on stdin, with kill as fallback | None | **Chosen** |
| Claude Agent SDK for Python (`claude-agent-sdk`, `ClaudeSDKClient`) | Anthropic tells third-party developers to use API key authentication with the SDK, not the claude.ai login | Unknown for subscription use; with an API key it is paid per token | Same features; it drives the same CLI underneath | One new package | Rejected |
| Messages API directly | API key from the Claude Console | Paid per token (Haiku 4.5: $1 per 1M input tokens, $5 per 1M output tokens; web search $10 per 1,000 searches) | Everything, but tools and web search are ours to wire | An HTTP client at most | Rejected: paid |

Why the CLI:

- **It uses the subscription.** Claude Code's headless documentation says bare mode "doesn't use your subscription login" and needs `ANTHROPIC_API_KEY`; without `--bare` the CLI uses the normal login ([headless](https://code.claude.com/docs/en/headless)). The Agent SDK overview says that, unless previously approved, third-party developers may not offer claude.ai login or rate limits and must use API key authentication ([Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)).
- **It is already proven on this PC.** jarvis's channel to Claude Code sessions (`jarvis/canal_claude.py`) already drives a persistent `claude` process over stream-json on stdin and stdout; its first reply arrived in about 1.9 s. `jarvis/canal_mcp.py` is already a hand-written MCP stdio server that talks to jarvis over an authenticated local IPC.
- **The SDK has a known Windows problem.** Issue #208 in the official Python SDK repository, "ClaudeSDKClient hangs on Windows during initialization", was closed as "not planned" ([issue #208](https://github.com/anthropics/claude-agent-sdk-python/issues/208)).
- **The flags give exactly the posture needed** ([CLI reference](https://code.claude.com/docs/en/cli-reference)): `--system-prompt` replaces the whole system prompt; `--tools WebSearch,WebFetch` keeps only those built-in tools (`--restricted` removes WebFetch unless it is named there); `--strict-mcp-config` loads only the MCP servers jarvis passes; `--safe-mode` skips CLAUDE.md, hooks, plugins and skills; `--disable-slash-commands`, `--permission-prompts none` (denied instead of asked), `--no-session-persistence` (nothing saved to disk) and `--max-turns`.

One point still to confirm on the Sponsor's PC: the CLI reference lists MCP servers among the customizations `--safe-mode` turns off, and does not say whether that includes servers passed with `--mcp-config`. jarvis keeps `--safe-mode` and logs the state of its MCP server from the CLI's `system/init` event, so the first real run shows whether the jarvis tools connected; the conversation works either way, only without those tools. If they do not connect, the fix is to drop `--safe-mode` and keep the same posture through `--restricted` (only managed settings and `--settings` are read, so no user hooks or settings), `--strict-mcp-config`, `--disable-slash-commands` and the neutral folder with no CLAUDE.md.

The brain process runs in a neutral folder outside jarvis and every project, like today's general questions. Its command line holds only constants and the validated model name; the Sponsor's sentences, the date, the location, the facts and summaries go only through stdin as JSON. The executable is resolved to the real `claude` binary, never an npm `.CMD` shim, because `cmd.exe` would parse the line again.

**Cancelling a turn.** The official Python SDK interrupts a turn by writing `{"type": "control_request", "request_id": ..., "request": {"subtype": "interrupt"}}` to the CLI's stdin ([SDK source](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/query.py)). The CLI reference does not document this request, so jarvis treats it as best effort: if no result comes within 1 s, it kills the process and starts a new one, seeded with the conversation jarvis keeps in memory. After a cancel no further text from that turn is ever spoken.

**Quota errors.** When an API request fails with a retryable error, the CLI emits a `system/api_retry` event with an error category such as `rate_limit` or `authentication_failed` ([headless](https://code.claude.com/docs/en/headless)). jarvis uses it to tell "out of quota" and "not logged in" apart from a broken process, and falls back (see the local classifier below).

## Default model

| | Claude Haiku 4.5 (`claude-haiku-4-5`) | Claude Sonnet 5 (`claude-sonnet-5`) |
|---|---|---|
| Comparative latency (Anthropic) | Fastest | Fast |
| Context window | 200K tokens | 1M tokens |
| Tool use system prompt | 496 tokens | 354 tokens |
| API price, for scale only | $1 / $5 per 1M input / output tokens | $2 / $10 per 1M input / output tokens |
| Already used by jarvis | yes, for general questions | no |

Sources: [models overview](https://platform.claude.com/docs/en/about-claude/models/overview), [pricing](https://platform.claude.com/docs/en/about-claude/pricing), [tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview). The prices only show relative weight; the Sponsor pays with subscription quota, not per token.

The default is **`claude-haiku-4-5`**: the fastest model, the lightest on the quota and already the model of today's general questions. Voice replies are short, so the extra depth of a larger model matters less than time to first word. There is no automatic switch to a larger model in this plan: a switch would need its own trigger, and the wrong trigger costs latency on every exchange. `scripts/medir_cerebro.py` compares Haiku with Sonnet on the Sponsor's PC (time to first text, first sentence, total and tokens per exchange, on fixed synthetic English exchanges); `[cerebro] modelo` changes only after that measurement.

## Tools exposed to the brain

The brain gets two built-in tools and a small set of jarvis tools, served by a hand-written MCP stdio server (`jarvis/cerebro_mcp.py`) that the brain's own CLI starts through `--mcp-config` ([MCP in Claude Code](https://code.claude.com/docs/en/mcp), [MCP transports](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)). The server talks to jarvis over the local IPC on 127.0.0.1 with a per-start secret; the generated MCP configuration holds no secret. `--allowedTools` names each tool exactly (`mcp__jarvis__<tool>`); there is no Bash, Read, Edit, Write, no access to project files and no credentials.

Every tool returns compact JSON with a size limit and no file paths. Text read from project files (reports, run reasons) is marked as untrusted data, so an instruction inside it is never followed as an order.

| Tool | Effect | Needs a spoken "yes" | What it returns to the brain |
|---|---|---|---|
| `WebSearch` | Searches the web (at most 2 web uses per exchange) | no | Search results, as Claude Code returns them |
| `WebFetch` | Reads one web page | no | The page content, summarised by Claude Code |
| `hora_e_data` | Reads the clock | no | Local time, weekday, date and time zone |
| `listar_projetos` | Reads jarvis's project list | no | Known project names and the last one used |
| `estado_do_projeto` | Reads a project's run and sessions | no | Run state and reason, tasks done/total, sessions per state, the project name as heard and as resolved |
| `relatorio_do_projeto` | Reads a project's latest report | no | The report's age and a bounded summary, marked untrusted |
| `factos_guardados` | Reads the fact notebook | no | The saved facts, bounded |
| `avisos_pendentes` | Reads queued notices | no | Notices not yet mentioned, each with its age |
| `enviar_ao_projeto` | Sends a text to a project's Claude Code session | **yes** | `awaiting_spoken_yes`; later the outcome (sent, cancelled, expired, failed) |
| `lancar_run` | Starts a FORJA run with a goal | **yes** | `awaiting_spoken_yes`; later the outcome |
| `parar_run` | Stops a project's FORJA run | **yes** | `awaiting_spoken_yes`; later the outcome |
| `retomar_run` | Resumes a project's FORJA run | **yes** | `awaiting_spoken_yes`; later the outcome |
| `lembrar_facto` | Saves a fact in the notebook | **yes** | `awaiting_spoken_yes`; later the outcome |
| `esquecer_facto` | Removes a fact from the notebook | **yes** | `awaiting_spoken_yes`; later the outcome |

An unknown project name returns the known names and does nothing. An unknown tool, a malformed message or an IPC call without the right secret returns an error and does nothing. A second effect tool while a recap is pending returns `busy` and changes nothing. There is no tool to buy or sell anything and no generic command tool.

The system prompt tells the brain when to use each tool and to say the result in one or two short spoken sentences, without reading ids, paths or lists.

## Safety rules

These rules are code, outside the model. The model can ask for anything; jarvis decides what happens.

1. **Only the listed tools exist.** Built-in tools are limited to WebSearch and WebFetch; MCP servers are limited to jarvis's own; hooks, skills, plugins, slash commands and CLAUDE.md files are not loaded; permission prompts are denied, never asked.
2. **Effects need the Sponsor's spoken "yes".** An effect tool never executes. It only creates the existing recap through the confirmation module (same recap, same yes/abort words, same deadline, corrections, the assumed project said in the recap). jarvis speaks the recap itself and drops the rest of that brain turn. The Sponsor's answer to the recap is handled before the brain and never reaches the model; a "yes" written by the model, by a web page or by a report confirms nothing. One pending action at a time.
3. **No buy or sell orders by voice, ever.** The existing financial rule runs on the transcript before anything is written to the brain (the brain is not called at all), on every tool argument, and on the brain's text before it is spoken (asset quotes, prices and trading advice are replaced by the refusal).
4. **No project files, no shell, no credentials.** The brain runs in a neutral folder, with `--safe-mode`, `--restricted` and `--strict-mcp-config`. Project content only reaches it through the read tools, bounded and marked untrusted.
5. **Nothing private on the command line.** Sentences, facts and summaries go only through stdin as delimited JSON data; the argv holds constants and the validated model name, and the executable is the real binary, never a `.CMD` shim.
6. **Nothing of the conversation on disk.** The transcript lives in RAM; Claude Code session persistence is off; the log keeps the lines it keeps today.

## Latency budget

Target: the first spoken sentence of a brain reply starts at most **1.5 s** after the last voiced audio (median, exchanges without web search). The fast path (stop, sleep and wake, time and date, yes/abort to a recap, "that's all", the financial refusal) is regular expressions only and never touches the brain or the local model.

| Stage | Budget | Evidence today |
|---|---|---|
| End of turn: silence after the last voiced chunk | 360 ms | Smart Turn waited 0.36 s median on the Sponsor's recordings; the fixed silence waits 0.60 s ([MODELOS.md](MODELOS.md)) |
| Transcription (Parakeet, CPU) | 150 ms | 124 ms median on the Sponsor's test recordings |
| Fast path check, financial rule, write to stdin | 10 ms | regular expressions and one pipe write |
| Warm brain: first text token | 600 ms | not measured yet; `scripts/medir_cerebro.py` measures it |
| First sentence complete and past the spoken-reply filter | 130 ms | a short opening sentence of a few words; the system prompt asks for one |
| Voice: first audio of that sentence (Kokoro `bm_fable`) | 250 ms | 501 ms median on 20 full-length sentences; a short first sentence is faster, to be confirmed |
| **Total** | **1.5 s** | |

The budget is tight. With the fixed 0.6 s silence instead of Smart Turn it only holds if the model and the voice beat their share; switching Smart Turn on is still the Sponsor's decision. What makes the budget possible at all is keeping the brain process warm from startup (no process start per sentence), streaming sentence by sentence, and a first sentence that is short. If no sentence is ready after about 1 s, jarvis says a short, varied acknowledgement; if a web search runs past about 8 s, a second one; at the time limit, one short failure sentence.

Exchanges with a web search have their own targets (first sentence at most 3.5 s median, 6 s at the 95th percentile), because the search itself takes seconds.

## Context and long sessions

- **One running conversation.** Every sentence that is not on the fast path is the next turn of the same brain session, so a short follow-up that only makes sense after the previous turn is understood.
- **Bounded size.** Claude Code's own auto-compaction does not fit: its window can only be set from 100,000 tokens up ([environment variables](https://code.claude.com/docs/en/env-vars)), which is far more than a voice chat should carry into every turn. jarvis keeps its own transcript in RAM and rolls over to a new session when the context the CLI reports passes `[cerebro] contexto_max_tokens` (default 16,000). The new session is seeded with a summary of at most 120 words, asked from the old session, and with the fact notebook; if the summary fails, the last few exchanges are used instead.
- **After a long pause.** After `[cerebro] inativo_min` (default 30) minutes without exchanges, the next sentence starts a fresh session the same way. Older detail is gone, as with a person; saved facts remain.
- **The fact notebook** (remember/forget, stored locally and ignored by Git) goes into the context of every new session as delimited data.
- **Nothing is written to disk** except what the log already keeps; `--no-session-persistence` keeps Claude Code from saving the session.
- **Prompt caching.** Claude Code caches the stable start of the prompt; cache reads are much cheaper and faster than fresh input, and the cache lives 5 minutes after each use ([prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)). Haiku 4.5 caches only prefixes of at least 4,096 tokens, so a very short first turn may not be cached; within a conversation the prefix grows past that quickly.

## Notices queue

Session and run notices and late project replies go into a bounded queue with no duplicates and an expiry. They are never spoken while a brain turn runs, while jarvis speaks, while a recap is pending, while the follow-up window or conversation mode is open, or while the Sponsor is talking. A late reply never cuts speech; its full text stays on screen and in the log.

While the conversation is active, queued notices enter the next message to the brain once, as delimited data, and the `avisos_pendentes` tool returns them, so the brain mentions them when it is natural or when the Sponsor asks. After `[cerebro] espera_dos_avisos_s` (default 20 s) with no conversation, the ones left are said together in one short sentence. Asleep, they are dropped as today; "cala-te" drops them; a notice already given to the brain is never spoken again.

## Fate of the local qwen3:8b classifier

It stays only as a fallback, for when the brain is not available: no quota (`rate_limit`), not logged in, the CLI missing, a failed start or two failures in a row (which is what a lost internet connection looks like). Then jarvis says one short sentence, logs it, and the local classifier handles project control, the memory commands and short social phrases as before; the brain is tried again after `[cerebro] reintentar_s` seconds. A general question in the fallback gets one short sentence that it needs Claude, and nothing leaves the PC. With the brain active, qwen3:8b and the local persona are not warmed at startup, which also removes the GPU contention with the other project's model: they make no request to Ollama until the fallback is needed, and the first sentence (or recap correction) that goes to the classifier in each fallback writes a log line saying the model is loaded now. A correction to a recap while the brain is active ("no, change tests to documentation", "add that it is urgent", "no, for orbita") uses only the mechanical edit, without Ollama; anything it cannot apply keeps the request unchanged and jarvis says so. Without the brain (`[cerebro] ativo = false`) the classifier is warmed at startup as before.

The per-question `claude -p` path was removed once conversation through the brain was proven: `jarvis/pergunta_geral.py`, `scripts/medir_gasto_perguntas.py`, their tests, the short conversation history kept for it and the continuation of a general question in the follow-up window. The brain keeps what it needed from that path (the neutral folder, reading the stream, token accounting) in `jarvis/cerebro.py`, and `scripts/medir_cerebro.py` replaces the old token measurement. The `[perguntas]` table now only gives the default location; its old `modelo` and `limite_s` keys, and `[memoria] trocas`, are still accepted so an older `config.toml` loads, but have no effect.

## Token cost per exchange and guaranteed ceiling

Every exchange uses the Sponsor's subscription quota, which he accepted; there is no extra money. Estimate for a typical exchange without web search, to be confirmed by `scripts/medir_cerebro.py`:

| Part | Tokens |
|---|---|
| System prompt (voice persona and tool guidance) | about 800 |
| Tool use system prompt added by Claude (Haiku 4.5) | 496 |
| Tool definitions (2 built-in, 12 jarvis tools) | about 2,000-3,000 |
| Facts from the notebook | at most about 1,500 |
| Conversation so far | at most 16,000 before rollover |
| **Input per exchange** | **about 5,000-10,000, most of it read from the cache** |
| **Output per exchange** | **about 30-150** |

A web search adds its results as input, typically a few thousand tokens. For scale only, at API prices an exchange like this would be well under one cent on Haiku 4.5.

The ceiling jarvis enforces per exchange, whatever the model does:

- at most `max_turns` = 4 model calls per exchange;
- a hard context of 32,000 tokens per call: above it jarvis interrupts the turn;
- so at most **4 × 32,000 = 128,000 input tokens per exchange**;
- at most 2 web uses per exchange;
- a character limit on the reply text (jarvis interrupts above it) and a time limit.

One thing the Sponsor controls outside jarvis: if usage credits are turned on for the subscription, Claude Code keeps working past the plan's limit and that is paid ([costs](https://code.claude.com/docs/en/costs), [usage credits](https://support.claude.com/en/articles/12429409-extra-usage-for-paid-claude-plans)). jarvis treats `rate_limit` as "brain unavailable" and never retries in a loop; keeping usage credits off (or with a spend limit) keeps the cost at zero extra money.

## Numeric targets

Measured with the Sponsor's real voice in a guided 20-exchange session with `scripts/sessao_naturalidade.py` (social greeting, a cooking request with a follow-up that depends on context, a weather question with a search, a project status asked naturally, a send to a project with "yes", a notice arriving mid-conversation, an interruption, sleep and wake). Latency is measured from the last voiced audio chunk to the first audio of the reply. Synthetic voices never count.

| Metric | Target |
|---|---|
| Exchanges the Sponsor marks as natural | at least 16 of 20 |
| Brain reply without web search, last voice to first spoken sentence | median at most 1.5 s |
| Brain reply with web search, first spoken sentence | median at most 3.5 s, 95th percentile at most 6 s |
| Fast-path replies (time, stop, sleep and wake) | median at most 1.0 s, 95th percentile at most 1.6 s |
| Follow-ups answered without their context | 0 |
| Unnecessary questions from jarvis | at most 1 |
| Notices spoken over the conversation | 0 |
| Actions executed without the Sponsor's spoken "yes" | 0 (also in the automatic tests) |
| Buy or sell orders or asset quotes spoken | 0 (also in the automatic tests) |
| Input tokens, typical exchange without search | at most about 10,000, measured |
| Input tokens, any exchange | at most 128,000, enforced |

## Planned changes and the metric each moves

In order, so the Sponsor feels the free conversation with context first and the project tools come after. All seven are built; the metrics are confirmed by the Sponsor's measurements listed in "What was built". Automatic tests never use a microphone, sound or the real Claude: a fake CLI speaks the stream-json protocol.

| # | Change | Metric it should move |
|---|---|---|
| 1 | Persistent brain session over the stream-json CLI: streaming, cancel with kill fallback, context rollover, per-exchange ceilings, the financial rule before and after | makes the rest possible; token ceiling |
| 2 | jarvis talks through the brain: deterministic fast path, sentence-by-sentence speech, one conversation with context, fallback to the local classifier | natural exchanges; follow-ups without context; unnecessary questions; brain reply latency |
| 3 | Measurements: brain latency targets in the session harness, the updated 20-exchange script, the Haiku vs Sonnet and token measurement | makes the brain targets measurable; tokens per exchange |
| 4 | Local MCP server with the read-only tools (time, projects, status, report, facts) | natural exchanges (status in plain sentences); unnecessary questions |
| 5 | Effect tools that only create the recap; only the spoken "yes" executes | actions without a spoken "yes" stays 0; natural exchanges |
| 6 | Notices queue that never interrupts the conversation | notices spoken over the conversation |
| 7 | qwen3:8b only as fallback, remove the per-question `claude -p`, document what was built | startup and GPU contention; fast-path latency |

After the build, the Sponsor's steps (not tasks): run `scripts/medir_cerebro.py --com-claude` once (it uses quota), do the 20-exchange session, and decide on Smart Turn.

## Sources

- Claude Code: [headless mode](https://code.claude.com/docs/en/headless), [CLI reference](https://code.claude.com/docs/en/cli-reference), [tools reference](https://code.claude.com/docs/en/tools-reference), [MCP](https://code.claude.com/docs/en/mcp), [environment variables](https://code.claude.com/docs/en/env-vars), [model configuration](https://code.claude.com/docs/en/model-config), [costs](https://code.claude.com/docs/en/costs), [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)
- Claude Agent SDK for Python: [repository](https://github.com/anthropics/claude-agent-sdk-python), [issue #208](https://github.com/anthropics/claude-agent-sdk-python/issues/208), [interrupt request in the SDK source](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/query.py)
- Claude platform: [models overview](https://platform.claude.com/docs/en/about-claude/models/overview), [pricing](https://platform.claude.com/docs/en/about-claude/pricing), [tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview), [prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- Claude support: [usage credits for paid plans](https://support.claude.com/en/articles/12429409-extra-usage-for-paid-claude-plans)
- Model Context Protocol: [transports](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
