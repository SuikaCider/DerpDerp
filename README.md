# quickask

**What this is** — a Spotlight-style panel you summon with a **double-tap of Right ⌘** to ask quick questions without interrupting your workflow. Type, hit Return, read the answer, dismiss. No browser, no app-switching.

**Audience** — a clueless-but-capable vibe coder (like me). I assume you can follow instructions with the help of an LLM.

**Conventions:** 🖥️ Do this · 👀 You'll see · ✅ Check · ⚠️ Gotcha · 💸 Costs money · 🔑 Needs a key

> 🏃 **Just want it running?** If you don't care how or why this works, **skip straight to [PART II](#part-ii--do-it) to start the setup.**

---

## Contents

**Part I — Understand it**
- [0. What it is & what it's for](#0-what-it-is--what-its-for)
- [1. How it's wired (search + LLM, kept separate)](#1-how-its-wired)
- [2. Why it doesn't use Gemini's own web search](#2-why-it-doesnt-use-geminis-own-web-search)
- [3. The modifiers — how you steer it](#3-the-modifiers)
- [4. The quota readout under each answer](#4-the-quota-readout)

**Part II — Do it**
- [5. Prerequisites](#5-prerequisites)
- [6. Get your keys](#6-get-your-keys)
- [7. Configure](#7-configure)
- [8. First run & permissions](#8-first-run--permissions)
- [9. Using it day-to-day](#9-using-it-day-to-day)
- [10. Make it launch at login](#10-make-it-launch-at-login)
- [11. Editing & reloading](#11-editing--reloading)

**Reference**
- [A. Config block reference](#appendix-a--config-block-reference)
- [B. Modifier grammar (full)](#appendix-b--modifier-grammar-full)
- [C. How the quota footer works](#appendix-c--how-the-quota-footer-works)
- [D. The LaunchAgent, explained](#appendix-d--the-launchagent-explained)
- [E. Troubleshooting](#appendix-e--troubleshooting)
- [F. A tour of the code](#appendix-f--a-tour-of-the-code)
- [The honest summary](#the-honest-summary)

---

# PART I — UNDERSTAND IT

## 0. What it is & what it's for

`quickask` is one Python file that puts a floating text box on your screen when you double-tap **Right ⌘**. You ask a question, it streams back a short answer, and Escape (or another double-tap) hides it. It has no Dock icon and no app-switcher entry — it just lives in the background waiting for the hotkey.

It's built for the kind of question you'd normally open a browser tab to Google — *"what's the HTML entity for a non-breaking space?"*, *"capital of Australia?"*, *"who won the last F1 race?"* — answered in place, in a second, without losing your focus.

## 1. How it's wired

The one idea worth understanding: **search and the language model are two separate, swappable layers.**

```
your question
   │
   ▼
[ SEARCH LAYER ]   →  optional context
   Wikipedia (free) or Tavily (live web)
   │
   ▼
[ LLM LAYER ]      →  streamed answer
   Groq (default) or Gemini
```

- The **search layer** fetches text to ground the answer. Wikipedia is free and keyless; Tavily is live-web and needs a key.
- The **LLM layer** writes the answer, optionally using that context. Groq is the default engine; Gemini is an alternative.

You mix and match these two layers with one-character [modifiers](#3-the-modifiers) at the start of your question. That's the whole tool.

## 2. Why it doesn't use Gemini's own web search

You might wonder why there's a separate search layer at all — Gemini has built-in "Google Search grounding." Short version: **on the free tier for current Gemini models, that grounding is 0-quota** — every grounded request is rejected immediately, regardless of your usage. So instead of relying on it, `quickask` does its own search (Wikipedia or Tavily) and hands the results to whichever model as plain context. Decoupling the two layers is what makes the tool work on free tiers.

## 3. The modifiers

You steer everything by prefixing your question with symbols. **Nothing** = the default; the rest are opt-in. (Type `/help` in the panel to see this cheatsheet any time.)

| Type this | Engine | Web search |
|---|---|---|
| `your question` | Groq | Wikipedia (free) |
| `\your question` | Groq | none |
| `\\your question` | Gemini | none |
| `\s your question` | Groq | **Tavily (live web)** 🔑💸 |
| `\d your question` | Groq | none, **direct/raw output** |
| `\\s`, `\\d`, `\sd`, `\\sd` … | combine them | see [Appendix B](#appendix-b--modifier-grammar-full) |

Two flags do the interesting work:
- **`s` = search the live web** with Tavily (for current events, prices, weather — things Wikipedia won't have). It's opt-in because Tavily's free tier is the scarce resource.
- **`d` = direct**: reply with *only* the bare answer as plain text — no preamble, no quotes, no backticks — so you can paste it straight into whatever you're doing.

Flags attach to a backslash and combine in any order: `\sd` = Groq + Tavily + direct; `\\sd` = Gemini + Tavily + direct. Full grammar in [Appendix B](#appendix-b--modifier-grammar-full).

## 4. The quota readout

Every answer ends with a dim line telling you what you have left, e.g.:

```
— Groq 998/1000 req today · 7782 tok/min left
```

- **Groq's numbers are exact** — read live from its API response headers.
- **Gemini and Tavily are local estimates** (marked with `~`), because those APIs don't report remaining quota. The tool counts its own calls in `~/.cache/quickask_usage.json` and subtracts from the caps in your config. They reset on their own schedule and won't know about calls made elsewhere. See [Appendix C](#appendix-c--how-the-quota-footer-works).

---

# PART II — DO IT

## 5. Prerequisites

⚠️ **The honest barrier isn't the API key — it's the setup.** You need:

- **macOS** (this uses native Mac UI; it won't run on Windows/Linux).
- **Python 3** (`python3 --version` should print 3.x).
- The **`pyobjc`** library, which lets Python draw the Mac panel:
  🖥️
  ```bash
  pip3 install pyobjc-framework-Cocoa
  ```
- **At least one API key** (next step). A single free **Groq** key is enough to get everything except live web search.

## 6. Get your keys

You need one required key and two optional ones. 🔑

| Provider | Required? | Get it at | Enables | Looks like |
|---|---|---|---|---|
| **Groq** | ✅ required | [console.groq.com](https://console.groq.com) | the default answer engine | `gsk_...` |
| **Tavily** | optional | [app.tavily.com](https://app.tavily.com) | the `\s` live-web modifier | `tvly-...` |
| **Gemini** | optional | [aistudio.google.com](https://aistudio.google.com) | the `\\` Gemini engine | `AIza...` |

All three have free tiers with no credit card. **Start with just Groq** — you'll have a fully working tool (Groq answers + free Wikipedia context + every modifier except `\s`). Add the others later if you want live web search or a second engine.

## 7. Configure

Open `derpderp.py` and find the **CONFIG** block near the top. Paste your key(s) between the quotes:

🖥️
```python
GROQ_API_KEY   = "gsk_your_key_here"
TAVILY_API_KEY = ""      # leave blank if you don't have one
GEMINI_API_KEY = ""      # leave blank if you don't have one
```

That's the only edit most people need. The rest of the block (models, quota caps, hotkey, file paths) has sensible defaults — see [Appendix A](#appendix-a--config-block-reference) if you want to change them.

✅ **Check:** at least `GROQ_API_KEY` has a value pasted in.

⚠️ **Don't commit your keys.** If you fork this to your *own* public repo, remember that your pasted keys live inside `derpderp.py` — pushing it with keys filled in leaks them. Either keep your keyed copy private/local, or blank the keys before committing (and revoke any key that slips out).

## 8. First run & permissions

🖥️
```bash
python3 derpderp.py
```

⚠️ **The first run, macOS will ask for Input Monitoring permission** — the global hotkey literally cannot work without it. If the double-tap does nothing:

1. Open **System Settings → Privacy & Security → Input Monitoring**
2. Enable **Python** (add it if it's not listed)
3. Re-run `python3 derpderp.py`

👀 **You'll see:** nothing on screen at first — it runs in the background. Then **double-tap Right ⌘** and the panel appears.

✅ **Check:** type `capital of france` and press Return — you should get "Paris" (plus the quota line).

## 9. Using it day-to-day

- **Open:** double-tap Right ⌘. **Dismiss:** Escape, or double-tap again.
- **Ask:** type and press Return; the answer streams in.
- **Steer:** prefix with a [modifier](#3-the-modifiers) (`\s` for live web, `\d` for raw output, etc.).
- **Cheatsheet:** type `/help` to see all modifiers in the panel.
- **Copy the answer:** press **⌘C** (with nothing selected) to copy the whole last answer, or click the **⧉** button in the top-right. Great with `\d` for paste-ready snippets.

## 10. Make it launch at login

By default you have to start it yourself. To have it always running after you log in, install it as a **LaunchAgent** (macOS's built-in "run this in the background at login" mechanism).

🖥️ Paste this whole block into a terminal — it fills in your own username/paths automatically:

```bash
PLIST="$HOME/Library/LaunchAgents/com.quickask.agent.plist"
PY="$(command -v python3)"
SCRIPT="$HOME/path/to/derpderp.py"   # ⬅️ change to where you saved it
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.quickask.agent</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$SCRIPT</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/quickask.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/quickask.log</string>
</dict></plist>
EOF
launchctl bootstrap gui/$(id -u) "$PLIST"
```

👀 **You'll see** nothing — but double-tap Right ⌘ and the panel is there. It'll come back automatically every login.

⚠️ **Gotcha:** the agent points at wherever the script lives (`$SCRIPT`). If you later move or rename that folder, the agent breaks — just re-run the block with the new path.

The script also holds a **single-instance lock**, so if it's ever started twice (agent + a manual run), the duplicate exits cleanly instead of creating a second hotkey listener. More detail in [Appendix D](#appendix-d--the-launchagent-explained).

## 11. Editing & reloading

The LaunchAgent starts one long-running process from the file **at login** — it doesn't re-read the file while running. So after you edit `derpderp.py`, reload it:

🖥️
```bash
launchctl kickstart -k gui/$(id -u)/com.quickask.agent
```

⚠️ If the panel stops appearing after an edit, you probably introduced a Python error — the traceback is in the log:
```bash
tail ~/Library/Logs/quickask.log
```

---

# REFERENCE

## Appendix A — Config block reference

Everything you can edit lives in the `CONFIG` block at the top of the file.

| Setting | What it does |
|---|---|
| `GROQ_API_KEY` / `TAVILY_API_KEY` / `GEMINI_API_KEY` | Your keys. Blank = that provider is disabled (the tool degrades gracefully). |
| `GROQ_MODEL` | Which Groq model answers. ⚠️ If you get a `404`, your account can't access this one — pick another from [console.groq.com](https://console.groq.com). |
| `GEMINI_MODEL` | Which Gemini model answers (a free-tier model with search grounding off). |
| `GEMINI_DAILY_LIMIT` / `TAVILY_MONTHLY_LIMIT` | Only used for the quota readout's `~` estimate. Changing them does **not** change any real limit — just the displayed number. |
| `RIGHT_CMD_KEYCODE` | Which key double-tap opens the panel (`54` = Right ⌘). |
| `DOUBLE_TAP_TIMEOUT` | Max seconds between the two taps. |
| `USAGE_FILE` / `LOCK_FILE` | Where the tool keeps its call-tally and single-instance lock. Fine to leave as-is. |

## Appendix B — Modifier grammar (full)

A leading **backslash marks a command**; letters attached to it are **flags**.

- **0 backslashes** → Groq engine + Wikipedia context (the default).
- **1 backslash (`\`)** → Groq engine, no Wikipedia.
- **2 backslashes (`\\`)** → Gemini engine, no Wikipedia.
- **flag `s`** → add a Tavily live-web search.
- **flag `d`** → direct/raw output (bare answer, no formatting).

Flags come right after the backslash(es), before your question, in any order:

| Input | Engine | Search | Direct |
|---|---|---|---|
| `question` | Groq | Wikipedia | — |
| `\question` | Groq | none | — |
| `\\question` | Gemini | none | — |
| `\s question` | Groq | Tavily | — |
| `\d question` | Groq | none | ✅ |
| `\sd question` / `\ds question` | Groq | Tavily | ✅ |
| `\\s question` | Gemini | Tavily | — |
| `\\sd question` | Gemini | Tavily | ✅ |

A leading `?` (or any character that isn't a flag) is treated as part of your question, so normal questions never trigger a modifier by accident.

## Appendix C — How the quota footer works

The line under each answer reports what's left:

- **Groq** → **exact**, pulled from the `x-ratelimit-remaining-*` response headers. `req today` is the daily request budget (~1000/day, refills slowly); `tok/min` is a per-minute token bucket (refills within a minute — so if you hit it, just wait a moment). When the token bucket is low it also shows the reset time.
- **Gemini / Tavily** → **local estimate** (`~`). Those APIs don't report remaining quota, so the tool counts its own calls in `USAGE_FILE` and subtracts from the caps in your config. These are estimates: they reset per period and don't know about calls you made elsewhere. Your true Tavily balance lives on the Tavily dashboard.

## Appendix D — The LaunchAgent, explained

A **LaunchAgent** is a small `.plist` file macOS reads to run something in your login session. The one in [step 10](#10-make-it-launch-at-login):

- `RunAtLoad` → start it at login.
- `KeepAlive { SuccessfulExit: false }` → restart it if it **crashes**, but not if it exits cleanly (so the single-instance duplicate can bow out without a restart loop).
- `StandardOutPath` / `StandardErrorPath` → send all output to `~/Library/Logs/quickask.log`.

Useful commands:
```bash
launchctl kickstart -k gui/$(id -u)/com.quickask.agent    # restart (after editing the script)
launchctl kill TERM gui/$(id -u)/com.quickask.agent       # stop until next login
launchctl bootout gui/$(id -u) "$HOME/Library/LaunchAgents/com.quickask.agent.plist"   # uninstall / disable
```

## Appendix E — Troubleshooting

| Symptom | Likely cause & fix |
|---|---|
| **Double-tap does nothing** | Input Monitoring not granted → System Settings → Privacy & Security → Input Monitoring → enable Python, then re-run/reload. |
| **`[HTTP 404] model … does not exist`** | Your account can't access `GROQ_MODEL`/`GEMINI_MODEL` → change it in CONFIG to one your key has. |
| **`[HTTP 429]` / quota reached** | You hit a rate/day limit. Groq's per-minute token bucket refills in ~a minute; Gemini's daily cap resets tomorrow. The tool falls back to Wikipedia/other engine where it can. |
| **Panel stopped appearing after an edit** | Python error on launch → `tail ~/Library/Logs/quickask.log` for the traceback. |
| **Answers seem doubled / hotkey acts twice** | Two instances running → the single-instance lock should prevent this; make sure you're not running it manually *and* via the LaunchAgent. |

## Appendix F — A tour of the code

It's one file, roughly in this order:

1. **CONFIG block** — keys, models, caps, hotkey (the only part you edit).
2. **`wiki_search` / `tavily_search` / `web_search`** — the search layer.
3. **`stream` (Gemini) / `stream_openai` (Groq) / `llm_stream`** — the LLM layer; `llm_stream` picks the engine.
4. **`Controller` / `Panel`** — the macOS panel: building the UI, parsing modifiers in `submit()`, streaming text in, the copy button, the quota footer.
5. **`main()`** — acquires the single-instance lock and registers the global hotkey + ⌘C monitors.

To change behavior: the prompt lives in `SYSTEM`; the modifier parsing lives in `Controller.submit()`; the answer format for `\d` lives in `DIRECT_INSTRUCTION`.

---

## The honest summary

`quickask` is a single-file macOS utility that trades polish for hackability: it's yours to read and edit. What it asks in return:

- **macOS only**, and it needs **Python 3 + `pyobjc`** — the real setup cost, more than the API key.
- It's an **unsigned script**, not a signed `.app`, so there's no double-click installer — you run it from the terminal (or the LaunchAgent).
- Free tiers are **real limits**: Groq ~1000 requests/day, Gemini's free daily cap, Tavily's monthly search cap. The tool degrades gracefully (falls back to Wikipedia / the other engine) but can't conjure quota.
- **Model availability varies by account** — if a model name 404s, swap it in CONFIG.

For a keyboard-driven "answer my quick question without breaking flow" tool, that trade is the whole point. Have fun. 🔎
