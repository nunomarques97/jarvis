# jarvis status orb: visual contract

The status orb is a small window that sits in a screen corner above every
other window. It shows what jarvis is doing, so you can see from across the
room whether jarvis is waiting, hearing you, thinking, talking or waiting for
your yes. It is implemented in `jarvis/bolinha.py` (standard library tkinter
only). `scripts/capturar_bolinha.py` captures every state in both themes.

## The question it answers

"Whose turn is it, and is jarvis hearing me?" Everything else is secondary.

## Organising rule

**One shape, sized by who is making sound.** The orb is the only dominant
element. It grows with the sound of whoever holds the turn: your microphone
level while jarvis listens to you, jarvis' own voice level while it speaks.
When nobody holds the turn it shrinks to a quiet dot. The glyph inside the orb
says what kind of turn it is. Colour reinforces the glyph but never replaces
it. The caption is secondary and only appears when there is text worth
reading.

## Directions compared

| Direction | Verdict |
| --- | --- |
| **Orb plus optional caption pill** (ChatGPT voice mode style) | **Chosen.** Readable at a glance in a corner, grows with sound, and leaves room below for the heard phrase and the recap text. |
| Orb only | Rejected: no place for the recap text, which is what you check before saying yes. |
| Waveform bars | Rejected: needs more width, and a flat line reads like "nothing" in the idle, thinking and sleeping states. It is harder to read at a glance in a corner. |

## Window

- Logical size 224 x 192 px at 96 DPI, scaled with the display DPI: a 128 px
  high orb zone with a 64 px caption zone below it.
- No frame and no taskbar button. It stays on top of other windows and never
  becomes the active window (`WS_EX_NOACTIVATE`), so state changes and clicks
  never take focus from the application you are typing in.
- The background is a colour key that Windows makes transparent. Only the orb
  and the caption pill are visible, and clicks on the empty area go through to
  the window below.
- Default placement: bottom-right corner of the primary monitor's work area,
  16 px from both edges.
- Drag the orb to move it. A press that moves 4 px or more is a drag. A press
  released within 0.6 s without moving that far is a click. The position is
  saved in `.jarvis/bolinha.json` (ignored by Git). On start it is clamped
  back into the work area of the monitor that shows most of the window, or the
  nearest one if the monitor is gone.
- Click (orb or caption) = silence jarvis, like the stop command, without
  closing it.

## States

Radii are logical pixels. The "cue" column is the non-colour cue: every state
can be told apart without colour.

| State (`estado`) | When | Cue (shape) | Size | Animation | Static equivalent |
| --- | --- | --- | --- | --- | --- |
| `repouso` | Waiting for the wake word | Plain small dot, no glyph | r 20 | Slow breathing, r 18.5-21.5 over 4 s | Plain dot, r 20 |
| `ouvir` | Listening to you | Large disc with an outer ring | r 30 + 18 x mic level | Orb and ring grow with the mic level (fast attack 50 ms, release 180 ms) | Disc r 30 with fixed ring at r 37 |
| `pensar` | Thinking | Three dots inside | r 28 | Dots swell in turn, 1.2 s wave | Three equal dots |
| `falar` | Speaking | Three vertical bars inside | r 30 + 10 x voice level | Orb grows and bars move with the voice level | Bars at fixed heights (short, tall, short) |
| `confirmar` | Waiting for your yes (also while it hears the answer) | `?` glyph and a ring | r 30 + 12 x mic level | Ring pulses outward every 1.6 s | `?` with a fixed ring |
| `dormir` | Asleep | Crescent moon | r 18 | None | Same |
| `erro` | Something failed | `!` glyph | r 28 | One short shake (0.45 s) on entry, then still | `!`, still |

With no new level for 0.5 s, the level falls back to 0, so the orb never stays
large when the sender stops. Entering `repouso` or `dormir` clears the
caption.

Suggested mapping from the console states that jarvis prints (`estado | ...`):
`A OUVIR` -> `repouso`, capture start -> `ouvir`, `EM CONVERSA` -> `ouvir`,
`A PENSAR` -> `pensar`, `A FALAR` -> `falar`, `À ESPERA DE CONFIRMAÇÃO` (with
or without "A OUVIR A RESPOSTA") -> `confirmar`, `A DORMIR` -> `dormir`.

## Colours

The theme follows the Windows app theme (`AppsUseLightTheme`), re-read every
5 s.

| Role | Dark theme | Light theme |
| --- | --- | --- |
| `repouso` | `#6B7A90` slate | `#8C97A8` slate |
| `ouvir` | `#3D8BFD` blue | `#1F6FEB` blue |
| `pensar` | `#9B7BF7` violet | `#7C4DDB` violet |
| `falar` | `#1FB89A` teal | `#0E9477` teal |
| `confirmar` | `#F5A524` amber (distinct from every other state) | `#E09200` amber |
| `dormir` | `#56607A` dim slate | `#AEB6C2` pale slate |
| `erro` | `#F05252` red | `#D93636` red |
| Glyph, dots, bars | `#FFFFFF` (`#1B1405` on amber) | `#FFFFFF` (`#1B1405` on amber) |
| Orb outline (1.5 px) | `#0D1117` | `#FFFFFF` |
| Caption pill / border / text | `#202124` / `#3C4043` / `#ECEDEE` | `#FFFFFF` / `#D0D4DA` / `#1F2328` |

## Caption

- An optional pill below the orb, Segoe UI 9 pt, centred, wrapped at 200 px,
  with at most three lines.
- It carries the phrase jarvis heard and, during the recap, the text it will
  send.
- The text is cleaned before display: control and direction-override
  characters are removed, whitespace is collapsed, and it is cut to 80
  characters at a word boundary with an ellipsis. If wide letters still need
  more than three lines, it is shortened further until it fits.

## Reduced motion

When Windows "Animation effects" is off (`SPI_GETCLIENTAREAANIMATION`, re-read
every 5 s), or with `--sem-animacao`, each state uses its static equivalent
above. The shape cue stays, sizes stay fixed and levels are not shown. A still
screenshot cannot prove the animation itself. The captures show one frame at a
fixed instant plus the static equivalents.

## Line protocol (`python -m jarvis.bolinha`)

The orb runs as a child process so it can never block or slow the voice
pipeline. Messages are UTF-8 lines of at most 512 bytes.

| Direction | Line | Meaning |
| --- | --- | --- |
| stdin | `estado <id>` | One of `repouso ouvir pensar falar confirmar dormir erro` |
| stdin | `nivel <number>` | Mic or voice level; clamped to 0..1; NaN/inf rejected |
| stdin | `legenda <text>` | Caption text; `legenda` alone clears it |
| stdout | `pronto` | The window is on screen |
| stdout | `clique` | You clicked the orb or caption (not a drag) |

Unknown, malformed or oversized lines are ignored and logged to stderr.
Only the two messages above ever appear on stdout. When stdin closes, the
window closes and the process exits with code 0. Options: `--tema
auto|escuro|claro`, `--sem-animacao`, `--posicao <file>`.

## Known weaknesses

- Tk draws circles without anti-aliasing, so edges are slightly stepped at 96
  DPI. This is the price of staying on the standard library.
- The colour-key transparency is Windows-only. Elsewhere the window shows a
  plain background.
