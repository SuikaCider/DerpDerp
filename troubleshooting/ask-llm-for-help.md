# DerpDerp — orientation for an AI assistant

**You (the AI) are being asked to help someone debug or modify DerpDerp.** They have
given you three things: the source file `derpderp.py`, this file, and a request of their
own. This file exists to get you up to speed on how `derpderp.py` works so you can act on
their request confidently. Read it, skim the source, then help them.

## Who you're helping

The user is a **capable but non-expert "vibe coder."** They can run Terminal commands and
edit a config line if you walk them through it, but they don't write code themselves. So:

- Go **one step at a time**, and confirm each step worked before moving on.
- Explain terms briefly; don't assume jargon.
- ⚠️ **Warn before anything that spends money, installs software, or changes system
  settings**, and let them confirm.
- **Keep it one file.** DerpDerp is deliberately a single script — don't split it into a
  package or add a dependency unless the user explicitly wants that.
- Preserve the author's casual voice in any user-facing strings/comments you touch.

## What DerpDerp is

A macOS "Spotlight for quick questions." Double-tapping **Right ⌘** toggles a floating
panel; the user types a short question, and DerpDerp answers by (optionally) fetching
search context and handing the whole thing to an LLM. It runs as a background **accessory
app** (no Dock icon). It is a **single Python file** using **PyObjC / AppKit** for the
native panel.

**Runtime:** macOS only · Python 3 · `pyobjc-framework-Cocoa`. The global hotkey needs
macOS **Input Monitoring** permission. Everything else is Python standard library
(`urllib`, `json`, `threading`, `fcntl`, `datetime`).

## The two-layer design (important)

DerpDerp is an **LLM wrapper** with two independent, swappable layers:

1. **Search layer** (optional context): **Wikipedia** (free, keyless, MediaWiki Action
   API) or **Tavily** (live web, needs a key).
2. **LLM layer** (writes the answer): **Groq** (default) or **Gemini**.

The user steers both with leading modifiers (see "Modifier parsing" below).

> ⚠️ **Design decision — do not "helpfully" undo it:** DerpDerp does its own searching and
> feeds the results to the LLM as plain text. It intentionally does **not** use Gemini's
> built-in Google Search grounding, because that grounding is **0-quota on the free tier
> for current Gemini models** (it returns HTTP 429 immediately). If you re-add
> `googleSearch` grounding to the Gemini call, you'll reintroduce that failure. Keep the
> search layer separate.

## Map of `derpderp.py` (top to bottom)

| Region | What's there |
|---|---|
| **SETTINGS block** | The built-in defaults, then `load_config()`: values in `derpderp_config.py` (next to the script; the user copies it from `derpderp_config.example.py`, which comments every setting; git ignores it) replace them. Names listed in `SETTING_NAMES`: `GROQ_API_KEY`, `TAVILY_API_KEY`, `GEMINI_API_KEY` (blank by default); `GROQ_MODEL`, `GEMINI_MODEL`; `GEMINI_DAILY_LIMIT`, `TAVILY_MONTHLY_LIMIT` (display-only); `RIGHT_CMD_KEYCODE` (54 = Right ⌘), `DOUBLE_TAP_TIMEOUT`; `USAGE_FILE`, `LOCK_FILE`, `HISTORY_FILE`; `KEEP_ANSWER_MINUTES`; `PERCENT_COMMAND` (blank = `%` lines off). |
| **Keys and settings from outside the file** | Right after SETTINGS, `load_secret(name)` reads the environment, then `export NAME="..."` lines in `~/.secrets`. `GROQ_POPUPSEARCH`, `TAVILY_POPUPSEARCH`, `GEMINI_POPUPSEARCH` and `DERPDERP_PERCENT_COMMAND` found there win over `derpderp_config.py`. Then derived `GROQ_URL`, `GEMINI_URL`. |
| **Prompts** | `SYSTEM` (the base instruction), `DIRECT_INSTRUCTION` (appended for `\d`/raw mode), `_system_prompt(direct)` (returns SYSTEM + the current local date/time so temporal questions work, + DIRECT_INSTRUCTION when direct), `HELP` (the `/help` cheatsheet as data: sections of lines, `backticks` drawn grey) and `help_attributed()` (lays it out). |
| **Usage tally** | `_gemini_day_key()` (Pacific midnight, Gemini's reset), `_month_key()`, `_load_usage()`, `bump_usage(name, period)`, `usage_count(name, period)`. Writes a small JSON at `USAGE_FILE`. Used only for the estimated Gemini/Tavily quota readout. |
| **Search layer** | `wiki_search(query)` (MediaWiki API over `urllib`: `list=search` top 3 titles → `prop=extracts` intros). `wiktionary_search(query)` (the `w` flag: `_wikt_candidates` picks the word(s) asked about, skipping question words and language names; `_wikt_clean` drops long reference sections and keeps only a language the question names). `tavily_search(query)` (POST `api.tavily.com/search`; bumps the Tavily tally). `web_search(query, live=False)` → Tavily when `live` and a key exists, else Wikipedia. Each takes `sources=[]` and appends the `(title, url)` of every page used, for the link line under the answer. |
| **LLM layer** | `llm_stream(messages, on_chunk, stats, engine, direct)` picks the engine and delegates. `stream_openai(...)` handles Groq (OpenAI-compatible SSE). `stream(...)` handles Gemini (`streamGenerateContent` SSE). |
| **UI** | `Panel(NSPanel)` (a borderless non-activating panel; `cancelOperation_` = Escape→hide). `Controller(NSObject)` builds and drives everything. |
| **Entrypoint** | `main()` → single-instance lock, accessory activation policy, and the global/local event monitors. |

### Controller — the methods worth knowing
- `_build()` — constructs the panel: an `NSVisualEffectView` (blur), an `NSTextField`
  (input), an `NSBox` (separator), an `NSTextView` in an `NSScrollView` (answer), and an
  `NSButton` (`⧉` copy button).
- `_place()` / `_grow()` — geometry (panel centered near top of screen; grows when an
  answer arrives).
- `show()` / `hide()` / `toggle()` — visibility. `hide()` records the time; `show()`
  within `KEEP_ANSWER_MINUTES` of it (and with no request still running) brings back the
  last answer and conversation, otherwise **it resets `self.messages = []`** (see
  "Conversation memory").
- `submit()` — reads the input, handles `/help`, `/history` and `% ` lines, **parses
  modifiers**, echoes the question, spawns a **worker thread**.
- `worker()` (inside `submit`) — runs the search(es), builds `api_messages`, calls
  `llm_stream`, then hands everything to `_finish_answer()`.
- `_finish_answer()` — keeps a complete answer for follow-ups, appends it to
  `HISTORY_FILE` (`_save_history`), copies it when the line had `c` or `d`, adds the
  source links (`_append_sources`) and the quota footer.
- `_run_percent()` — a `% ` line runs `PERCENT_COMMAND` with the rest of the line as its
  last argument and shows what it printed; no LLM.
- `appendText_()` / `appendFooter_()` — UI mutations; called on the main thread (see
  "Threading").
- `copyAnswer_()` / `local_keydown()` — the copy feature (⧉ button, ⌘C, ⌘↩ = copy and
  close; ⌘↩ on a typed line asks it, then copies and closes via `close_when_done`), Esc, and ⌘A/⌘C/⌘V/⌘X routing.
- `control_textView_doCommandBySelector_()` — Return submits, Esc hides, ↑/↓ step through
  `self.recall` (the lines asked since launch).
- `_quota_footer()` — builds the dim italic quota line under each answer.
- `check_double_tap()` / `global_flags()` / `local_flags()` — hotkey detection.

## Behaviors & gotchas you must respect

- **Modifier parsing (in `Controller.submit()`):** a leading `\` marks a command.
  0 backslashes → Groq + Wikipedia (default); 1 `\` → Groq, no search; 2 `\\` → Gemini,
  no search. After the backslash(es), flag letters may appear in any order (`\sd`,
  `\wc`, `\\sp`, …): **`s`** (add Tavily), **`w`** (add Wiktionary), **`d`** (direct/raw
  output, also copied), **`c`** (copy the answer when done), **`p`** (send the clipboard
  along with the question; searches still use the question alone). The flag cluster is
  only read if every character before the first space is one of `s d w c p`; otherwise
  it's treated as part of the question (so `\define x` is a question, not a flag). A bare
  leading `?` or any non-flag char is part of the question.
- **Engine selection:** `llm_stream` uses Groq when `GROQ_API_KEY` is set
  (`engine="auto"`), Gemini when forced by `\\` or when there's no Groq key. If Groq is
  still returning 429 after `stream_openai`'s one retry and a Gemini key exists, it says
  so in the panel and asks Gemini instead; without a Gemini key the 429 is reported.
- **Conversation memory:** `submit()` appends each turn to `self.messages` and the worker
  sends the **whole history** to the LLM, so follow-ups within an open session have
  context. `show()` clears it unless the panel is reopened within `KEEP_ANSWER_MINUTES`.
  Separately, every complete answer is appended to `HISTORY_FILE` as `Q: …` / `A: …`
  lines (blank = off), and `/history` shows the last 10. When searching, only the
  copy sent to the model (`api_messages`) is augmented with search results; the stored
  history keeps the clean question.
- **Threading:** all network I/O runs on a daemon **worker thread**. AppKit is not
  thread-safe, so UI updates are marshaled to the main thread via
  `performSelectorOnMainThread_withObject_waitUntilDone_` (that's why `appendText_`,
  `appendFooter_`, `showCopyButton_` exist as ObjC-exposed methods). If you add UI updates
  from a background thread, route them the same way.
- **Groq needs a real `User-Agent` header.** Groq is behind Cloudflare, which rejects
  `urllib`'s default agent with `403` error `1010`. `stream_openai` sets one — don't remove
  it.
- **Streaming formats differ:** Groq/OpenAI SSE emits `choices[].delta.content` and a
  `[DONE]` sentinel; Gemini SSE emits `candidates[].content.parts[].text`. Keep the two
  parsers separate.
- **Quota readout:** Groq's numbers are exact (read from `x-ratelimit-*` response
  headers into `stats`); Gemini and Tavily are **local estimates** counted in `USAGE_FILE`
  (the APIs don't report remaining quota).
- **Model names vary by account.** A `404 model does not exist` means the user's key can't
  access `GROQ_MODEL`/`GEMINI_MODEL`; the fix is to set that line in `derpderp_config.py`, not the code.
- **Single-instance lock:** `main()` holds an `fcntl.flock` on `LOCK_FILE`; a second
  instance exits cleanly. Relevant if the user runs it manually *and* via the LaunchAgent.
- **Settings:** the user's own values go in `derpderp_config.py`, never in `derpderp.py`
  (an update to the script would overwrite them). A typo there stops DerpDerp at launch
  with the line it's on; an unknown name is logged and ignored.
- **Keys:** pasting a key into `derpderp_config.py` works. To keep keys out of files
  instead, no code change is needed: put `export GROQ_POPUPSEARCH="gsk_..."` (and
  `TAVILY_POPUPSEARCH`, `GEMINI_POPUPSEARCH`) in `~/.secrets` or the environment;
  `load_secret` reads those first.
- **`%` lines:** a line starting `% ` never reaches an LLM. It runs `PERCENT_COMMAND`
  (blank = off, and the panel says so) with the rest of the line as the last argument,
  for hooking DerpDerp to the user's own note or to-do tool. `DERPDERP_PERCENT_COMMAND`
  in the environment or `~/.secrets` wins over `derpderp_config.py`. The `/help` "ESTATE" section
  describes the author's own to-do tool's syntax; it means nothing without that tool.
- **Auto-start:** if the user set up the optional macOS **LaunchAgent**, edits to
  `derpderp.py` don't take effect until they reload it:
  `launchctl kickstart -k gui/$(id -u)/com.derpderp.agent`. Remind them after any code
  change. Errors on launch go to `~/Library/Logs/derpderp.log`.

## Where to make common changes

| The user wants to… | Change this |
|---|---|
| Rebind the hotkey / tune the double-tap speed | `RIGHT_CMD_KEYCODE`, `DOUBLE_TAP_TIMEOUT` (`derpderp_config.py`) |
| Use different models | `GROQ_MODEL`, `GEMINI_MODEL` (`derpderp_config.py`) |
| Change answer tone/length | `SYSTEM`; for `\d` raw mode, `DIRECT_INSTRUCTION` |
| Add or change a modifier | the parser in `Controller.submit()` |
| Resize / restyle the panel | `PANEL_W`/`INPUT_H`/`FIELD_H`/`ANSWER_H`/`SIDE` + `_build()` (blur material, corner radius, fonts, colors) |
| Swap or add a search source | `wiki_search` / `wiktionary_search` / `tavily_search` / `web_search`, and the `searches` list in `submit()` |
| Wiktionary picks the wrong word | `WIKT_SKIP` (words never looked up); quoting the word in the question always works |
| Move or turn off the answer history | `HISTORY_FILE` (blank = off) |
| Keep the last answer longer / never | `KEEP_ANSWER_MINUTES` (0 = every open starts fresh) |
| Swap or add an LLM provider | `llm_stream` + `stream_openai` (it's OpenAI-compatible, so OpenRouter/others drop in with a new URL/model/key) |
| Fix the quota estimates | `GEMINI_DAILY_LIMIT`, `TAVILY_MONTHLY_LIMIT` (display only) |

## Porting to Windows / Linux

The **search and LLM layers are plain, cross-platform Python** (`wiki_search`,
`tavily_search`, `stream_openai`, `stream`) and can be reused as-is. What's macOS-specific
is the whole **UI + hotkey + autostart** layer: `NSPanel`/AppKit, the `NSEvent` global
monitors, Input Monitoring permission, and the LaunchAgent. To port, keep the layers above
and rebuild the shell — e.g. a **PySide6/PyQt** frameless always-on-top window plus a
cross-platform global-hotkey library, and OS-appropriate autostart. Warn the user this is a
real (if modest) rewrite, not a config tweak.

---

*After reading the above, address the user's actual request. If you need to see a specific
part of `derpderp.py`, ask them to paste it (or read it if you already have the file).*
