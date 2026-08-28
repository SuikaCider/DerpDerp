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
| **CONFIG block** | `GROQ_API_KEY`, `TAVILY_API_KEY`, `GEMINI_API_KEY` (blank in the public copy); `GROQ_MODEL`, `GEMINI_MODEL`; `GEMINI_DAILY_LIMIT`, `TAVILY_MONTHLY_LIMIT` (display-only); `RIGHT_CMD_KEYCODE` (54 = Right ⌘), `DOUBLE_TAP_TIMEOUT`; `USAGE_FILE`, `LOCK_FILE`. Then derived `GROQ_URL`, `GEMINI_URL`. |
| **Prompts** | `SYSTEM` (the base instruction), `DIRECT_INSTRUCTION` (appended for `\d`/raw mode), `_system_prompt(direct)` (returns SYSTEM + the current local date/time so temporal questions work, + DIRECT_INSTRUCTION when direct), `HELP_TEXT` (the `/help` cheatsheet). |
| **Usage tally** | `_today_key()`, `_month_key()`, `_load_usage()`, `bump_usage(name, period)`, `usage_count(name, period)`. Writes a small JSON at `USAGE_FILE`. Used only for the estimated Gemini/Tavily quota readout. |
| **Search layer** | `wiki_search(query)` (MediaWiki API over `urllib`: `list=search` top 3 titles → `prop=extracts` intros). `tavily_search(query)` (POST `api.tavily.com/search`; bumps the Tavily tally). `web_search(query, live=False)` → Tavily when `live` and a key exists, else Wikipedia. |
| **LLM layer** | `llm_stream(messages, on_chunk, on_done, stats, engine, direct)` picks the engine and delegates. `stream_openai(...)` handles Groq (OpenAI-compatible SSE). `stream(...)` handles Gemini (`streamGenerateContent` SSE). |
| **UI** | `Panel(NSPanel)` (a borderless non-activating panel; `cancelOperation_` = Escape→hide). `Controller(NSObject)` builds and drives everything. |
| **Entrypoint** | `main()` → single-instance lock, accessory activation policy, and the global/local event monitors. |

### Controller — the methods worth knowing
- `_build()` — constructs the panel: an `NSVisualEffectView` (blur), an `NSTextField`
  (input), an `NSBox` (separator), an `NSTextView` in an `NSScrollView` (answer), and an
  `NSButton` (`⧉` copy button).
- `_place()` / `_grow()` — geometry (panel centered near top of screen; grows when an
  answer arrives).
- `show()` / `hide()` / `toggle()` — visibility. **`show()` resets `self.messages = []`**
  (see "Conversation memory").
- `submit()` — reads the input, **parses modifiers**, echoes the question, spawns a
  **worker thread**.
- `worker()` (inside `submit`) — runs the search, builds `api_messages`, calls
  `llm_stream`, then appends the quota footer.
- `appendText_()` / `appendFooter_()` / `showCopyButton_()` — UI mutations; called on the
  main thread (see "Threading").
- `copyAnswer_()` / `local_keydown()` — the copy feature (⧉ button and ⌘C).
- `_quota_footer()` — builds the dim italic quota line under each answer.
- `check_double_tap()` / `global_flags()` / `local_flags()` — hotkey detection.

## Behaviors & gotchas you must respect

- **Modifier parsing (in `Controller.submit()`):** a leading `\` marks a command.
  0 backslashes → Groq + Wikipedia (default); 1 `\` → Groq, no search; 2 `\\` → Gemini,
  no search. After the backslash(es), the flag letters **`s`** (add Tavily) and **`d`**
  (direct/raw output) may appear in any order (`\sd`, `\ds`, `\\sd`, …). The flag cluster
  is only read if every character before the first space is `s`/`d`; otherwise it's
  treated as part of the question (so `\define x` is a question, not a flag). A bare
  leading `?` or any non-flag char is part of the question.
- **Engine selection is by key presence, not failover:** `llm_stream` uses Groq when
  `GROQ_API_KEY` is set (or `engine="auto"`), Gemini when forced by `\\` or when there's
  no Groq key. **There is no automatic "Groq rate-limited → Gemini" failover.** On a 429,
  `stream_openai` retries once then reports the error.
- **Conversation memory:** `submit()` appends each turn to `self.messages` and the worker
  sends the **whole history** to the LLM, so follow-ups within an open session have
  context. `show()` clears it, so each fresh open starts blank. When searching, only the
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
  access `GROQ_MODEL`/`GEMINI_MODEL`; the fix is to change that constant, not the code.
- **Single-instance lock:** `main()` holds an `fcntl.flock` on `LOCK_FILE`; a second
  instance exits cleanly. Relevant if the user runs it manually *and* via the LaunchAgent.
- **Keys:** the public copy has blank inline keys in CONFIG. Some users instead point
  those lines at a hidden dotfile (e.g. reading `~/.secrets`) so keys never live in the
  script — a reasonable change to help with if asked.
- **Auto-start:** if the user set up the optional macOS **LaunchAgent**, edits to
  `derpderp.py` don't take effect until they reload it:
  `launchctl kickstart -k gui/$(id -u)/com.derpderp.agent`. Remind them after any code
  change. Errors on launch go to `~/Library/Logs/derpderp.log`.

## Where to make common changes

| The user wants to… | Change this |
|---|---|
| Rebind the hotkey / tune the double-tap speed | `RIGHT_CMD_KEYCODE`, `DOUBLE_TAP_TIMEOUT` (CONFIG) |
| Use different models | `GROQ_MODEL`, `GEMINI_MODEL` (CONFIG) |
| Change answer tone/length | `SYSTEM`; for `\d` raw mode, `DIRECT_INSTRUCTION` |
| Add or change a modifier | the parser in `Controller.submit()` |
| Resize / restyle the panel | `PANEL_W`/`INPUT_H`/`FIELD_H`/`ANSWER_H`/`SIDE` + `_build()` (blur material, corner radius, fonts, colors) |
| Swap or add a search source | `wiki_search` / `tavily_search` / `web_search` |
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
