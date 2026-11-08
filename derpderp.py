#!/usr/bin/env python3
r"""
derpderp — a Spotlight-style floating quick-answer panel for macOS.

A background agent with no Dock icon and no app-switcher entry.
Double-tap Right Command (⌘) to open, Escape to dismiss.

Type a question and press Return. Prefix modifiers (see /help in the panel):
    (nothing) Groq answer with Wikipedia context
    \         Groq answer, no search
    \\        Gemini answer, no search
    \s \\s    add a live web search via Tavily
    \w \\w    add a Wiktionary lookup (meanings, IPA, etymology)
    \d \\d    direct: a bare, paste-ready plaintext answer, copied for you
    \c \\c    copy the answer to the clipboard when it's done
    \p \\p    send what you copied (⌘C) along with the question you type
    %         hand the line to your own command (optional: PERCENT_COMMAND in derpderp_config.py);
              with PERCENT_SUGGEST_COMMAND set, a `#` path's predicted name shows greyed, ` takes it;
              PERCENT_SUGGEST_URL asks a running app for the same answer first (faster)
Flags combine in any order: \wc ipa of prendre. ↑ recalls earlier questions,
⌘↩ copies the answer and closes (on a typed line: asks it first), /history
shows the last 10 answers.

──────────────────────────────────────────────────────────────────────────
SETUP (3 steps)
  1. Install the dependency:
        pip3 install pyobjc-framework-Cocoa
  2. Copy derpderp_config.example.py to derpderp_config.py (same folder) and
     paste at least one API key into it (a free Groq key alone is enough to
     get started).
  3. Run it:
        python3 derpderp.py

     macOS will ask for Input Monitoring permission the first time — the
     global hotkey can't work without it. Grant it under
     System Settings > Privacy & Security > Input Monitoring, then re-run.

  (Optional) To launch it automatically at login, see the notes at the very
  bottom of this file.
──────────────────────────────────────────────────────────────────────────
"""

import os
import ctypes
import fcntl
import json
import re
import shlex
import subprocess
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSColor,
    NSEvent,
    NSEventMaskFlagsChanged,
    NSEventModifierFlagCommand,
    NSButton,
    NSFont,
    NSFontAttributeName,
    NSFontManager,
    NSForegroundColorAttributeName,
    NSItalicFontMask,
    NSLinkAttributeName,
    NSBox,
    NSEventMaskKeyDown,
    NSMakeRect,
    NSPanel,
    NSPasteboard,
    NSPasteboardTypeString,
    NSScreen,
    NSScrollView,
    NSTextField,
    NSTextView,
    NSVisualEffectView,
    NSFontWeightSemibold,
    NSBaselineOffsetAttributeName,
    NSMutableParagraphStyle,
    NSParagraphStyle,
    NSParagraphStyleAttributeName,
    NSTextTab,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorTransient,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSObject, NSAttributedString, NSMutableAttributedString, NSURL
from PyObjCTools import AppHelper

# ============================================================================
#  SETTINGS  ───  change them in derpderp_config.py, not here
# ============================================================================
# These are the defaults. Your own values go in derpderp_config.py next to this
# script: copy derpderp_config.example.py (which explains every setting) to that
# name and edit it. Updating derpderp.py then never overwrites your settings.

GROQ_API_KEY   = ""
TAVILY_API_KEY = ""
GEMINI_API_KEY = ""
GROQ_MODEL   = "openai/gpt-oss-120b"
GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_DAILY_LIMIT   = 500
TAVILY_MONTHLY_LIMIT = 1000
RIGHT_CMD_KEYCODE  = 54
DOUBLE_TAP_TIMEOUT = 0.4
USAGE_FILE = os.path.expanduser("~/.cache/derpderp_usage.json")
LOCK_FILE  = os.path.expanduser("~/.cache/derpderp.lock")
HISTORY_FILE = os.path.expanduser("~/.cache/derpderp_history.txt")
KEEP_ANSWER_MINUTES = 5
PERCENT_COMMAND = ""
PERCENT_SUGGEST_COMMAND = ""
PERCENT_SUGGEST_URL = ""
SETTING_NAMES = (
    "GROQ_API_KEY", "TAVILY_API_KEY", "GEMINI_API_KEY", "GROQ_MODEL", "GEMINI_MODEL",
    "GEMINI_DAILY_LIMIT", "TAVILY_MONTHLY_LIMIT", "RIGHT_CMD_KEYCODE", "DOUBLE_TAP_TIMEOUT",
    "USAGE_FILE", "LOCK_FILE", "HISTORY_FILE", "KEEP_ANSWER_MINUTES", "PERCENT_COMMAND",
    "PERCENT_SUGGEST_COMMAND", "PERCENT_SUGGEST_URL",
)

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "derpderp_config.py")


def load_config(path, settings):
    """Overwrite `settings` with the NAME = value lines of the file at `path`, if
    there is one. A typo in the file stops DerpDerp with the line it's on."""
    if not os.path.exists(path):
        return
    import runpy
    for name, value in runpy.run_path(path).items():
        if name in SETTING_NAMES:
            settings[name] = value
        elif name.isupper():
            print("derpderp: %s in %s is not a setting; ignored" % (name, path))


load_config(CONFIG_FILE, globals())
USAGE_FILE = os.path.expanduser(USAGE_FILE)
LOCK_FILE = os.path.expanduser(LOCK_FILE)
HISTORY_FILE = os.path.expanduser(HISTORY_FILE)

# --- Keys kept outside this file (optional) ---------------------------------
# If GROQ_POPUPSEARCH / TAVILY_POPUPSEARCH / GEMINI_POPUPSEARCH is set in your
# environment, or as an `export NAME="..."` line in ~/.secrets, that value wins
# over the one in derpderp_config.py. Otherwise that value is used.

SECRETS_FILE = os.path.expanduser("~/.secrets")
_secrets_cache = None


def load_secret(name):
    """Return a secret by name from the environment, or by parsing ~/.secrets.

    ~/.secrets is a shell file of `export KEY=value` lines (often sourced by
    ~/.zshrc). Parsing the file too means the key is still found when the app
    is launched outside that shell, e.g. by a LaunchAgent.
    """
    value = os.environ.get(name)
    if value:
        return value.strip()

    global _secrets_cache
    if _secrets_cache is None:
        _secrets_cache = {}
        try:
            with open(SECRETS_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("export "):
                        line = line[len("export "):]
                    if line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    _secrets_cache[key.strip()] = val.strip().strip('"').strip("'")
        except OSError:
            pass
    return _secrets_cache.get(name)


GROQ_API_KEY   = load_secret("GROQ_POPUPSEARCH")   or GROQ_API_KEY
TAVILY_API_KEY = load_secret("TAVILY_POPUPSEARCH") or TAVILY_API_KEY
GEMINI_API_KEY = load_secret("GEMINI_POPUPSEARCH") or GEMINI_API_KEY
PERCENT_COMMAND = load_secret("DERPDERP_PERCENT_COMMAND") or PERCENT_COMMAND
PERCENT_SUGGEST_COMMAND = load_secret("DERPDERP_PERCENT_SUGGEST_COMMAND") or PERCENT_SUGGEST_COMMAND
PERCENT_SUGGEST_URL = load_secret("DERPDERP_PERCENT_SUGGEST_URL") or PERCENT_SUGGEST_URL

# ============================================================================
#  NOTES  ───  IF YOU'RE ALSO A CLUELESS VIBE CODER....
# ============================================================================
#
# I personally configured this to start automatically at startup.
# This is accomplished by something called a LaunchAgent.
# Copy/paste-able instructions to make your own are at the BOTTOM of this file.

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "%s:streamGenerateContent?alt=sse" % GEMINI_MODEL
)   # the key travels in the x-goog-api-key header, never in the URL


def _redact(text):
    """Blank out any API key that an error message might echo."""
    for key in (GROQ_API_KEY, TAVILY_API_KEY, GEMINI_API_KEY):
        if key:
            text = text.replace(key, "•••")
    return text


ESCAPE_KEYCODE = 53
RETURN_KEYCODES = (36, 76)   # Return, and Enter on a numeric keypad
# Which bit of an event's modifier flags is THIS physical key (left and right ⌘
# share the plain Command flag, so that flag can't tell them apart).
DEVICE_MASKS = {54: 0x10, 55: 0x08, 56: 0x02, 60: 0x04, 58: 0x20, 61: 0x40, 59: 0x01, 62: 0x2000}

try:   # macOS's running count of key presses, system-wide: no extra install needed
    _cg = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    _cg.CGEventSourceCounterForEventType.restype = ctypes.c_uint32
    _cg.CGEventSourceCounterForEventType.argtypes = [ctypes.c_int32, ctypes.c_uint32]
except OSError:
    _cg = None


def keydown_count():
    """How many keys have been pressed since boot (None if unknown). A tap of the
    hotkey only counts when this hasn't moved, i.e. no shortcut was typed with it."""
    return _cg.CGEventSourceCounterForEventType(1, 10) if _cg else None   # HID state, key-down

EDIT_SHORTCUTS = {"a": "selectAll:", "c": "copy:", "v": "paste:", "x": "cut:"}

PANEL_W = 620
INPUT_H = 54        # height of the question bar
FIELD_H = 28       # natural height of the text field, centered within the bar
ANSWER_H = 380
SIDE = 16          # horizontal inset shared by input, separator and answer text
PLACEHOLDER_DROP = -4.0   # points the placeholder sits lower than its natural spot
TIP_H = 22         # the hint line under the bar, shown only while a `% ... #path` hint is up
SUGGEST_DELAY = 0.25   # seconds of no typing before the suggest command is asked
SUGGEST_URL_TIMEOUT = 1.0   # seconds PERCENT_SUGGEST_URL gets to answer before the command is asked

SYSTEM = (
    "You are a Spotlight-style quick answer overlay. "
    "When asked straightforward factual questions, respond concisely and directly. "
    "Do not use introductory conversational fluff or response tags. Simply answer the "
    "provided question. "
    "If the question includes a 'Web results' block, treat it as fresh source "
    "material and prefer it over your own memory for current facts; if it doesn't "
    "actually answer the question, say what you do know and note the results were thin."
)


DIRECT_INSTRUCTION = (
    " DIRECT MODE: output only the exact answer, as raw plain text meant to be "
    "pasted verbatim elsewhere. No preamble, no explanation, no restating the "
    "question, no surrounding quotation marks, no backticks, no Markdown, and no "
    "trailing punctuation unless it is genuinely part of the answer. If the answer "
    "is a value, string, or snippet, output it and nothing else."
)

# Shown in-panel when the user submits "/help". Purely local — no API call.
# Each section: (header, note beside it, lines). In a line, `backticks` mark a
# modifier or command (drawn grey) and a tab starts the explanation, so every
# explanation begins at HELP_COLUMN.
HELP = (
    ("SEARCH ENGINES", "", (
        "`␣`\tGroq response; yes wikipedia, no search",
        "`\\`\tGroq response; no wikipedia, no search",
        "`\\\\`\tGemini response; no wikipedia, no search",
    )),
    ("MODIFIERS", "(Append directly to `\\` or `\\\\`)", (
        "`s`\tAdd web search via Tavily",
        "`w`\tAdd Wiktionary (meanings, IPA, etymology)",
        "`d`\tDirect plaintext answer; copies itself to your clipboard",
        "`c`\tCopy the answer to your clipboard when it's done",
        "`p`\tSend what you copied (⌘C) along with the question you type",
        "",
        "\tCombine them in any order: `\\wc ipa of prendre`",
    )),
    ("SHORTCUTS", "", (
        "`↑`\tRecall your earlier questions",
        "\t`⌘↩` copies the answer and closes the panel (after a typed question: asks it first)",
        "\t`/history` shows your last 10 questions and answers",
    )),
    ("ESTATE", "(if you're using my other task management tools)", (
        "`%`\tLog an item into To Derp's inbox",
        "\tEnd task with `@weekday` (i.e., `@monday`) → be reminded next Monday",
        "\t`#{Area}/{Project}/{Task}/{Subtask}` → Log directly to a registered To Derp item",
        "\tAfter a `#`, the backtick key takes the greyed name (`↑`/`↓`: the others)",
    )),
)
HELP_COLUMN = 30   # points from a modifier's left edge to where its explanation starts


def body_attributes():
    """The plain answer style: what every typed or streamed line uses."""
    return {
        NSFontAttributeName: NSFont.systemFontOfSize_(14),
        NSForegroundColorAttributeName: NSColor.labelColor(),
        NSParagraphStyleAttributeName: NSParagraphStyle.defaultParagraphStyle(),
    }


def help_attributed():
    """HELP as styled text: bigger headers, grey modifiers, aligned explanations."""
    body = NSFont.systemFontOfSize_(14)
    head = NSFont.systemFontOfSize_weight_(17, NSFontWeightSemibold)
    # Modifiers sit indented under their header by the same space that follows
    # the widest of them, so only headers touch the left margin.
    widest = max(
        NSAttributedString.alloc().initWithString_attributes_(
            line.split("`")[1], {NSFontAttributeName: body}).size().width
        for _, _, lines in HELP for line in lines if line.startswith("`"))
    indent = round(HELP_COLUMN - widest)
    rows = NSMutableParagraphStyle.alloc().init()
    rows.setFirstLineHeadIndent_(indent)
    rows.setTabStops_([NSTextTab.alloc().initWithTextAlignment_location_options_(0, indent + HELP_COLUMN, {})])
    rows.setHeadIndent_(indent + HELP_COLUMN)   # a wrapped explanation stays in its column
    out = NSMutableAttributedString.alloc().init()

    def add(text, font, para, grey=False):
        color = NSColor.secondaryLabelColor() if grey else NSColor.labelColor()
        out.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
            text, {NSFontAttributeName: font, NSForegroundColorAttributeName: color,
                   NSParagraphStyleAttributeName: para}))

    def add_marked(text, font, para):
        for i, part in enumerate(text.split("`")):
            if part:
                add(part, font, para, grey=bool(i % 2))

    for n, (title, note, lines) in enumerate(HELP):
        header = NSMutableParagraphStyle.alloc().init()
        header.setParagraphSpacingBefore_(18 if n else 0)
        header.setParagraphSpacing_(4)
        add(title, head, header)
        if note:
            add_marked("  " + note, body, header)
        add("\n", body, header)
        for line in lines:
            add_marked(line + "\n", body, rows)
    return out



def _system_prompt(direct=False):
    """SYSTEM plus the current local date/time, so temporal questions resolve."""
    now = datetime.now().astimezone()
    prompt = SYSTEM + (
        " The user's current local date and time is %s. "
        "Use it to answer questions about the current time, date, or the time "
        "in other places directly."
        % now.strftime("%A, %B %d, %Y at %I:%M %p %Z (UTC%z)")
    )
    if direct:
        prompt += DIRECT_INSTRUCTION
    return prompt


# ---------------------------------------------------------------- usage tally
#
# Tavily and Gemini don't report remaining quota, so we count calls locally.
# This is an estimate: it only knows about calls this script makes, and resets
# per period. Groq's remaining count is read live from its response headers.


def _gemini_day_key():
    """Gemini's free daily quota resets at midnight Pacific time, not local."""
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def _month_key():
    return datetime.now().astimezone().strftime("%Y-%m")


def _load_usage():
    try:
        with open(USAGE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def bump_usage(name, period_key):
    """Increment (and return) the call count for `name` in the given period."""
    data = _load_usage()
    rec = data.get(name)
    if not rec or rec.get("period") != period_key:
        rec = {"period": period_key, "count": 0}
    rec["count"] += 1
    data[name] = rec
    try:
        os.makedirs(os.path.dirname(USAGE_FILE), exist_ok=True)
        with open(USAGE_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass
    return rec["count"]


def usage_count(name, period_key):
    rec = _load_usage().get(name, {})
    return rec["count"] if rec.get("period") == period_key else 0


# ---------------------------------------------------------------- web search

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_UA = {"User-Agent": "derpderp/1.0 (personal quick-answer overlay)"}


def _page_url(base, title):
    return base + urllib.parse.quote(title.replace(" ", "_"))


def wiki_search(query, max_titles=3, char_cap=1200, timeout=15, sources=None):
    """Free, keyless web lookup via the Wikipedia API.

    Returns a plain-text context block of intro extracts for the top matching
    articles, or "" on no hit / failure. This sidesteps Gemini's Google Search
    grounding (which is 0-quota on the free tier for Gemini 3.x). It's good for
    factual/encyclopedic questions; it will NOT have live data such as today's
    news, sports scores, or current prices. Each article used is added to
    `sources` as (title, url), for the links under the answer.
    """
    try:
        search_url = WIKI_API + "?" + urllib.parse.urlencode(
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": max_titles,
                "format": "json",
            }
        )
        hits = json.load(
            urllib.request.urlopen(
                urllib.request.Request(search_url, headers=WIKI_UA), timeout=timeout
            )
        )
        titles = [h["title"] for h in hits.get("query", {}).get("search", [])]
        if not titles:
            return ""

        extract_url = WIKI_API + "?" + urllib.parse.urlencode(
            {
                "action": "query",
                "prop": "extracts",
                "exintro": 1,
                "explaintext": 1,
                "redirects": 1,
                "titles": "|".join(titles),
                "format": "json",
            }
        )
        pages = json.load(
            urllib.request.urlopen(
                urllib.request.Request(extract_url, headers=WIKI_UA), timeout=timeout
            )
        )["query"]["pages"]

        blocks = []
        for page in pages.values():
            extract = (page.get("extract") or "").strip()
            if extract:
                blocks.append("## %s\n%s" % (page.get("title", ""), extract[:char_cap]))
                if sources is not None:
                    sources.append((page.get("title", ""),
                                    _page_url("https://en.wikipedia.org/wiki/", page.get("title", ""))))
        return "\n\n".join(blocks)
    except Exception:
        return ""


WIKT_API = "https://en.wiktionary.org/w/api.php"
# Question words that are never the word being looked up.
WIKT_SKIP = set("""
a an and are as at be by can could define definition describe difference do does english
etymology explain for from gender give how i in ipa is it its it's me mean meaning means
of on or plural pronounce pronounced pronunciation reading say says spell spelled spelling
tell than that the this to translate translation use used verb noun adjective was what
what's whats when where which who why with word words you your conjugate conjugation
french spanish german italian portuguese japanese chinese mandarin cantonese korean
vietnamese russian catalan latin greek arabic dutch thai hindi turkish polish
""".split())   # a quoted word is looked up even when it's listed here
# Sections that are long and rarely the answer to a quick question.
WIKT_DROP = {"Conjugation", "Declension", "Inflection", "Derived terms", "Related terms",
             "Descendants", "Translations", "Further reading", "References", "Anagrams",
             "See also", "Compounds", "Collocations", "Coordinate terms", "Hyponyms",
             "Hypernyms", "Synonyms", "Antonyms", "Alternative forms", "Mutation"}


def _wikt_candidates(query):
    """The words of `query` that could be the one asked about, in order: quoted
    words first, then every word that isn't a question word or language name."""
    quoted = re.findall(r"[\"“‘「『]([^\"”’」』]+)[\"”’」』]", query)
    words = re.findall(r"\w[\w'’-]*", query)
    out = []
    for w in quoted + [w for w in words if w.lower() not in WIKT_SKIP]:
        if w not in out:
            out.append(w)
    return out or words


def _wikt_clean(extract, query, char_cap):
    """Drop the long reference sections; when the question names a language
    that has its own section (`== French ==`), keep only that language."""
    sections, title, level, lines = [], "", 0, []
    for line in extract.splitlines():
        m = re.match(r"^(=+)\s*(.+?)\s*=+$", line.strip())
        if m:
            sections.append((level, title, lines))
            level, title, lines = len(m.group(1)), m.group(2), []
        else:
            lines.append(line)
    sections.append((level, title, lines))

    asked = {w.lower() for w in re.findall(r"\w+", query)}
    languages = {t for lvl, t, _ in sections if lvl == 2}
    wanted = {t for t in languages if t.lower() in asked}
    out, language = [], ""
    for lvl, t, body in sections:
        if lvl == 2:
            language = t
        if wanted and language not in wanted:
            continue
        if t in WIKT_DROP:
            continue
        if t:
            out.append("%s %s" % ("#" * max(lvl - 1, 1), t))
        out.extend(body)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return text[:char_cap]


def wiktionary_search(query, max_words=2, char_cap=4000, timeout=15, sources=None):
    """Free, keyless dictionary lookup via the English Wiktionary API (the `w`
    flag). Finds which words of the question have an entry, then returns the
    plain-text entry (meanings, pronunciation/IPA, etymology) for up to
    `max_words` of them, or "" on no hit / failure. Wikipedia rarely has these
    for an ordinary word."""
    try:
        candidates = _wikt_candidates(query)[:10]
        titles = []
        for w in candidates:
            titles += [w] if w == w.lower() else [w, w.lower()]
        url = WIKT_API + "?" + urllib.parse.urlencode(
            {"action": "query", "titles": "|".join(titles[:50]), "redirects": 1, "format": "json"})
        data = json.load(urllib.request.urlopen(
            urllib.request.Request(url, headers=WIKI_UA), timeout=timeout))["query"]
        found = {p["title"] for p in data.get("pages", {}).values() if "missing" not in p}
        renamed = {r["from"]: r["to"] for r in data.get("normalized", []) + data.get("redirects", [])}
        picked = []
        for t in titles:
            t = renamed.get(t, t)
            if t in found and t not in picked:
                picked.append(t)
        blocks = []
        for title in picked[:max_words]:
            # A full (not intro-only) extract is one page per request.
            url = WIKT_API + "?" + urllib.parse.urlencode(
                {"action": "query", "prop": "extracts", "explaintext": 1,
                 "titles": title, "format": "json"})
            pages = json.load(urllib.request.urlopen(
                urllib.request.Request(url, headers=WIKI_UA), timeout=timeout))["query"]["pages"]
            for page in pages.values():
                text = _wikt_clean(page.get("extract") or "", query, char_cap)
                if text:
                    blocks.append("## Wiktionary: %s\n%s" % (title, text))
                    if sources is not None:
                        sources.append((title, _page_url("https://en.wiktionary.org/wiki/", title)))
        return "\n\n".join(blocks)
    except Exception:
        return ""


def tavily_search(query, max_results=5, char_cap=800, timeout=20, sources=None):
    """Live web search via Tavily (needs TAVILY_API_KEY; 1000/mo free).

    Returns (context, problem): a plain-text context block (answer summary + top
    results) and "" — or "" and a short reason when there's no key, the call
    fails, or nothing came back. Unlike Wikipedia this covers current events,
    prices, weather, etc.
    """
    key = TAVILY_API_KEY
    if not key:
        return "", "no Tavily key"
    body = json.dumps(
        {
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": True,
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=body,
        headers={"content-type": "application/json", "authorization": "Bearer " + key},
    )
    try:
        data = json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        return "", "Tavily error %s%s" % (e.code, " (monthly quota?)" if e.code in (429, 432) else "")
    except Exception as e:
        return "", _redact("Tavily unreachable: %s" % e)
    bump_usage("tavily", _month_key())  # a credit is spent on any 200
    blocks = []
    if data.get("answer"):
        blocks.append("Summary: " + data["answer"])
    for r in data.get("results", []):
        blocks.append(
            "## %s\n%s\n<%s>"
            % (r.get("title", ""), (r.get("content") or "")[:char_cap], r.get("url", ""))
        )
        if sources is not None and r.get("url"):
            sources.append((r.get("title") or r["url"], r["url"]))
    return ("\n\n".join(blocks), "") if blocks else ("", "Tavily found nothing")


def web_search(query, live=False, sources=None):
    """Fetch context for a question. Returns (context, note): live=True prefers
    Tavily (current events) and falls back to Wikipedia, saying so in `note`;
    live=False uses Wikipedia only. Pages used are added to `sources`."""
    if live:
        context, problem = tavily_search(query, sources=sources)
        if context:
            return context, ""
        return (wiki_search(query, sources=sources),
                "[live search unavailable (%s): used Wikipedia instead]" % problem)
    return wiki_search(query, sources=sources), ""


# ---------------------------------------------------------------- LLM
#
# Two backends share one gemini-style message history ([{role, parts:[{text}]}],
# role "user"/"model"). llm_stream() picks Groq when its key is present.


def llm_stream(messages, on_chunk, stats=None, engine="auto", direct=False):
    """Stream an answer from the chosen engine; return (text, status, note).

    engine="auto" prefers Groq when its key is present, else Gemini;
    engine="gemini" forces Gemini (the \\\\ lane). direct=True asks for a bare,
    paste-ready answer. `stats` is populated with which engine ran and, for Groq,
    the remaining counts from the headers. status is "ok" (a complete answer),
    "interrupted" (cut off part-way), "blocked" (refused by the service) or
    "failed"; `note` says why when it isn't "ok".
    """
    groq_key = GROQ_API_KEY
    if engine == "auto":
        engine = "groq" if groq_key else "gemini"

    if engine == "groq" and groq_key:
        if stats is not None:
            stats["engine"] = "groq"
        text, status, note = stream_openai(GROQ_URL, groq_key, GROQ_MODEL, messages, on_chunk,
                                           stats=stats, direct=direct)
        # Groq still rate limiting after its retry: let Gemini answer instead of a dead end.
        if status == "failed" and note.startswith("[HTTP 429]") and GEMINI_API_KEY:
            on_chunk("[Groq is rate limiting: asking Gemini instead]\n\n")
            if stats is not None:
                stats.clear()
                stats["engine"] = "gemini"
            return stream(messages, on_chunk, stats=stats, direct=direct)
        return text, status, note
    if stats is not None:
        stats["engine"] = "gemini"
    return stream(messages, on_chunk, stats=stats, direct=direct)


def _error_body(e):
    """An HTTP error's body text, or "" if even reading it fails (A1: that read
    used to escape the handler and leave the panel refusing every question)."""
    try:
        return e.read().decode("utf-8", "replace")
    except Exception:
        return ""


def stream_openai(url, key, model, messages, on_chunk, extra_headers=None,
                  retries=2, stats=None, direct=False):
    """Stream from any OpenAI-compatible chat endpoint (Groq, OpenRouter, …)."""
    oai_messages = [{"role": "system", "content": _system_prompt(direct)}]
    for m in messages:
        role = "assistant" if m["role"] == "model" else "user"
        oai_messages.append({"role": role, "content": m["parts"][0]["text"]})
    body = json.dumps({"model": model, "messages": oai_messages, "stream": True}).encode()
    # A real User-Agent is required: Groq sits behind Cloudflare, which rejects
    # urllib's default "Python-urllib" signature with 403 error 1010.
    headers = {
        "content-type": "application/json",
        "authorization": "Bearer " + key,
        "User-Agent": "derpderp/1.0",
    }
    if extra_headers:
        headers.update(extra_headers)

    collected, status, note = [], "failed", ""
    for attempt in range(retries):
        collected, finished = [], False
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                if stats is not None:
                    stats["remaining_requests"] = resp.headers.get(
                        "x-ratelimit-remaining-requests"
                    )
                    stats["limit_requests"] = resp.headers.get(
                        "x-ratelimit-limit-requests"
                    )
                    stats["remaining_tokens"] = resp.headers.get(
                        "x-ratelimit-remaining-tokens"
                    )
                    stats["reset_tokens"] = resp.headers.get(
                        "x-ratelimit-reset-tokens"
                    )
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        finished = True
                        continue
                    if not payload:
                        continue
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if "error" in event:   # the service reports a failure mid-stream
                        err = event["error"]
                        msg = err.get("message", err) if isinstance(err, dict) else err
                        return "".join(collected), "failed", _redact("[error] %s" % msg)
                    try:
                        choice = event["choices"][0]
                        text = choice["delta"].get("content")
                        if text:
                            collected.append(text)
                            on_chunk(text)
                        reason = choice.get("finish_reason")
                        if reason == "stop":
                            finished = True
                        elif reason == "length":
                            note = "[answer hit the length limit]"
                        elif reason:
                            return "".join(collected), "blocked", "[answer stopped: %s]" % reason
                    except (KeyError, IndexError):
                        pass
            if finished and not note:
                return "".join(collected), "ok", ""
            return "".join(collected), "interrupted", note or "[answer cut off]"
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                on_chunk("[rate limited, retrying in 3s…]\n")
                time.sleep(3)
                continue
            return "", "failed", _redact("[HTTP %s] %s" % (e.code, _error_body(e)[:200]))
        except Exception as e:
            return "".join(collected), ("interrupted" if collected else "failed"), _redact("[error] %s" % e)
    return "".join(collected), status, note


def _gemini_429_message(body_txt):
    """A daily-quota 429 says so; a per-minute one says to wait (A10)."""
    daily, delay = False, ""
    try:
        for detail in json.loads(body_txt).get("error", {}).get("details", []):
            for v in detail.get("violations", []):
                if "PerDay" in v.get("quotaId", ""):
                    daily = True
            delay = detail.get("retryDelay", delay)
    except (ValueError, AttributeError):
        pass
    if daily:
        return ("[HTTP 429] Gemini free-tier daily quota reached "
                "(%d requests/day). Try again tomorrow." % GEMINI_DAILY_LIMIT)
    return "[HTTP 429] Gemini is rate limiting; try again in %s." % (delay or "a minute")


def stream(messages, on_chunk, retries=2, stats=None, direct=False):
    """Stream an answer from the Gemini API (fallback engine).

    No Google Search grounding is attached — it's 0-quota on the free tier for
    Gemini 3.x, so live info is supplied via web_search() instead (see submit).
    """
    request_body = {
        "systemInstruction": {"parts": [{"text": _system_prompt(direct)}]},
        "contents": messages,
    }
    body = json.dumps(request_body).encode()
    headers = {"content-type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    collected = []
    for attempt in range(retries):
        # Discard any partial text from a failed earlier attempt so the answer
        # stored in history only reflects the successful stream.
        collected, finished, note = [], False, ""
        req = urllib.request.Request(GEMINI_URL, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                # Counted once Gemini accepts the request, whether or not the
                # answer then finishes downloading (A11).
                bump_usage("gemini", _gemini_day_key())
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if "error" in event:
                        msg = event["error"].get("message", "") if isinstance(event["error"], dict) else event["error"]
                        return "".join(collected), "failed", _redact("[error] %s" % msg)
                    blocked = event.get("promptFeedback", {}).get("blockReason")
                    if blocked:
                        return "", "blocked", "[Gemini blocked this question: %s]" % blocked
                    try:
                        candidate = event["candidates"][0]
                    except (KeyError, IndexError):
                        continue
                    # Every visible text part, in order (A8); thought parts are internal.
                    for part in candidate.get("content", {}).get("parts", []):
                        text = part.get("text")
                        if text and not part.get("thought"):
                            collected.append(text)
                            on_chunk(text)
                    reason = candidate.get("finishReason")
                    if reason == "STOP":
                        finished = True
                    elif reason == "MAX_TOKENS":
                        note = "[answer hit the length limit]"
                    elif reason:
                        return "".join(collected), "blocked", "[Gemini stopped this answer: %s]" % reason
            if finished and not note:
                return "".join(collected), "ok", ""
            return "".join(collected), "interrupted", note or "[answer cut off]"
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                on_chunk("[rate limited, retrying in 3s…]\n")
                time.sleep(3)
                continue
            body_txt = _error_body(e)
            if e.code == 429:
                return "", "failed", _gemini_429_message(body_txt)
            return "", "failed", _redact("[HTTP %s] %s" % (e.code, body_txt[:200]))
        except Exception as e:
            return "".join(collected), ("interrupted" if collected else "failed"), _redact("[error] %s" % e)
    return "".join(collected), "failed", ""


# ---------------------------------------------------------------- panel

class Panel(NSPanel):
    def canBecomeKeyWindow(self):
        return True

    def cancelOperation_(self, sender):
        """Fallback: Catch Escape if the panel itself has focus instead of the text field."""
        if self.delegate() and hasattr(self.delegate(), "hide"):
            self.delegate().hide()


class Controller(NSObject):
    def init(self):
        self = objc.super(Controller, self).init()
        self.messages = []
        self.answering = False
        self.last_answer = ""
        self.session = 0          # bumped on every open; older requests' output is dropped
        self.hotkey_down = False  # is the hotkey itself held right now?
        self.press_time = 0.0     # when it went down, and the key-press count then
        self.press_keys = None
        self.tap_time = 0.0       # the last clean tap (0 = none pending)
        self.tap_keys = None
        self.hidden_at = 0.0      # when the panel was last closed (0 = not since it opened)
        self.close_when_done = False   # ⌘↩ on a typed line: copy the answer and close when it's done
        self.recall = []          # lines asked since launch, for ↑ / ↓
        self.recall_at = None     # which of them the field shows (None = a fresh line)
        self.typed = None         # the suggest command's answer for the `% ` line in the field
        self.choice = 0           # which of its suggested names is offered (↑ / ↓)
        self.suggest_seq = 0      # bumped on every edit: an answer for older text is dropped
        self.tip_h = 0            # TIP_H while the hint line shows, else 0
        self._build()
        return self

    # ---- construction
    @objc.python_method
    def _build(self):
        rect = NSMakeRect(0, 0, PANEL_W, INPUT_H)
        mask = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        self.panel = Panel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, mask, NSBackingStoreBuffered, False
        )
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setHasShadow_(True)
        self.panel.setLevel_(101) 
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setMovableByWindowBackground_(True)
        self.panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorTransient
        )
        self.panel.setDelegate_(self)

        blur = NSVisualEffectView.alloc().initWithFrame_(rect)
        blur.setMaterial_(18)
        blur.setBlendingMode_(0)
        blur.setState_(1)
        blur.setWantsLayer_(True)
        blur.layer().setCornerRadius_(12.0)
        blur.layer().setMasksToBounds_(True)
        blur.setAutoresizingMask_(2 | 16)
        self.panel.setContentView_(blur)

        # Natural-height field centered vertically inside the INPUT_H bar,
        # rather than a full-height field whose text hugs the top edge.
        self.input = NSTextField.alloc().initWithFrame_(
            NSMakeRect(SIDE, (INPUT_H - FIELD_H) / 2.0, PANEL_W - 2 * SIDE, FIELD_H)
        )
        self.input.setFont_(NSFont.systemFontOfSize_(20))
        self.input.setBezeled_(False)
        self.input.setDrawsBackground_(False)
        self.input.setFocusRingType_(1)
        legend = "␣ Wikipedia・\\ Groq・\\\\ Gemini・Or, /help"
        self.input.setPlaceholderAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(
                legend,
                {
                    NSFontAttributeName: NSFont.systemFontOfSize_(13),
                    NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
                    # The 13pt hint draws at the top of a field sized for 20pt
                    # text; lower it to the middle of the bar.
                    NSBaselineOffsetAttributeName: PLACEHOLDER_DROP,
                },
            )
        )
        self.input.setAutoresizingMask_(2 | 8)

        # Set the controller as the delegate to intercept typing commands (Return, Escape)
        self.input.setDelegate_(self)

        blur.addSubview_(self.input)

        # A `% ... #path` line: the rest of the predicted name, greyed after the
        # cursor, and a hint line under the bar (what Enter does, what ` takes).
        self.ghost = NSTextField.labelWithString_("")
        self.ghost.setFont_(NSFont.systemFontOfSize_(20))
        self.ghost.setTextColor_(NSColor.placeholderTextColor())
        self.ghost.setFrame_(NSMakeRect(SIDE, 0, 10, FIELD_H))
        blur.addSubview_(self.ghost)
        self.ghost.setHidden_(True)
        self.tip = NSTextField.labelWithString_("")
        self.tip.setFont_(NSFont.systemFontOfSize_(12))
        self.tip.setTextColor_(NSColor.secondaryLabelColor())
        self.tip.setLineBreakMode_(4)  # NSLineBreakByTruncatingTail
        self.tip.setFrame_(NSMakeRect(SIDE, 0, PANEL_W - 2 * SIDE, 16))
        blur.addSubview_(self.tip)
        self.tip.setHidden_(True)

        # Hairline between the question bar and the answer, shown only when expanded.
        self.separator = NSBox.alloc().initWithFrame_(
            NSMakeRect(SIDE, INPUT_H, PANEL_W - 2 * SIDE, 1)
        )
        self.separator.setBoxType_(2)  # NSBoxSeparator
        self.separator.setAutoresizingMask_(2 | 8)
        blur.addSubview_(self.separator)
        self.separator.setHidden_(True)

        self.scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(0, 0, PANEL_W, ANSWER_H)
        )
        self.scroll.setDrawsBackground_(False)
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setAutoresizingMask_(2 | 16)
        self.answer = NSTextView.alloc().initWithFrame_(
            NSMakeRect(0, 0, PANEL_W, ANSWER_H)
        )
        self.answer.setFont_(NSFont.systemFontOfSize_(14))
        self.answer.setDrawsBackground_(False)
        self.answer.setEditable_(False)
        self.answer.setTextContainerInset_((16, 12))
        self.answer.setTextColor_(NSColor.labelColor())
        self.scroll.setDocumentView_(self.answer)
        blur.addSubview_(self.scroll)
        self.scroll.setHidden_(True)

        # Floating copy button (top-right of the answer). Shown once an answer
        # exists; copies the answer text only (not the question/search chrome).
        self.copybtn = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 26, 22))
        self.copybtn.setBordered_(False)
        self.copybtn.setTitle_("⧉")
        self.copybtn.setFont_(NSFont.systemFontOfSize_(15))
        self.copybtn.setToolTip_("Copy answer  (⌘C)")
        self.copybtn.setTarget_(self)
        self.copybtn.setAction_("copyAnswer:")
        self.copybtn.setAutoresizingMask_(1 | 8)  # pin to top-right
        blur.addSubview_(self.copybtn)
        self.copybtn.setHidden_(True)

    # ---- geometry
    @objc.python_method
    def _place(self, height):
        mouse = NSEvent.mouseLocation()
        screen = NSScreen.mainScreen()
        for s in NSScreen.screens():
            f = s.frame()
            if (
                f.origin.x <= mouse.x <= f.origin.x + f.size.width
                and f.origin.y <= mouse.y <= f.origin.y + f.size.height
            ):
                screen = s
                break
        vf = screen.visibleFrame()
        height += self.tip_h
        x = vf.origin.x + (vf.size.width - PANEL_W) / 2
        y = vf.origin.y + vf.size.height * 0.72 - height
        self.panel.setFrame_display_animate_(
            NSMakeRect(x, y, PANEL_W, height), True, False
        )
        self._layout(height)

    @objc.python_method
    def _layout(self, height):
        # Question bar occupies the top INPUT_H of the panel; the field is
        # centered within it, the hint line (if up) sits under it, and the
        # answer fills everything below.
        bar_bottom = height - INPUT_H
        answer_top = bar_bottom - self.tip_h
        field_y = bar_bottom + (INPUT_H - FIELD_H) / 2.0
        self.input.setFrameOrigin_((SIDE, field_y))
        self.ghost.setFrameOrigin_((self.ghost.frame().origin.x, field_y))
        self.tip.setFrameOrigin_((SIDE, answer_top + 4))
        self.separator.setFrameOrigin_((SIDE, answer_top))
        self.scroll.setFrame_(NSMakeRect(0, 0, PANEL_W, answer_top))
        self.copybtn.setFrameOrigin_((PANEL_W - SIDE - 26, answer_top - 28))

    @objc.python_method
    def _set_tip_h(self, tip_h):
        """Grow or shrink the panel for the hint line, its top edge staying put."""
        if tip_h == self.tip_h:
            return
        f = self.panel.frame()
        height = f.size.height - self.tip_h + tip_h
        self.tip_h = tip_h
        self.panel.setFrame_display_animate_(
            NSMakeRect(f.origin.x, f.origin.y + f.size.height - height, PANEL_W, height), True, False
        )
        self._layout(height)

    @objc.python_method
    def _grow(self):
        if self.scroll.isHidden():
            self.scroll.setHidden_(False)
            self.separator.setHidden_(False)
            self._place(INPUT_H + ANSWER_H)

    # ---- show / hide
    @objc.python_method
    def show(self):
        self.recall_at = None
        self.close_when_done = False
        self.typed = None
        self.tip_h = 0
        self.tip.setHidden_(True)
        self.ghost.setHidden_(True)
        # Reopened soon after closing, with nothing still running: the last answer
        # (and the conversation behind it) is still there, so a stray Esc loses nothing.
        recent = self.hidden_at and time.time() - self.hidden_at < KEEP_ANSWER_MINUTES * 60
        self.hidden_at = 0.0
        if recent and not self.answering and self.answer.string():
            self._place(INPUT_H + ANSWER_H)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            self.panel.makeKeyAndOrderFront_(None)
            self.panel.makeFirstResponder_(self.input)
            self._typed_changed()
            return
        # A request still running from before Escape is abandoned: its output is
        # dropped (see _on_main) and new questions work at once.
        self.session += 1
        self.answering = False
        self.messages = []
        self.last_answer = ""
        self.answer.setString_("")
        self.scroll.setHidden_(True)
        self.separator.setHidden_(True)
        self.copybtn.setHidden_(True)
        self.copybtn.setTitle_("⧉")
        self.input.setStringValue_("")
        self.suggest_seq += 1
        self._place(INPUT_H)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        self.panel.makeFirstResponder_(self.input)

    @objc.python_method
    def hide(self):
        self.hidden_at = time.time()
        self.panel.orderOut_(None)
        NSApplication.sharedApplication().hide_(None)

    @objc.python_method
    def toggle(self):
        if self.panel.isVisible():
            self.hide()
        else:
            self.show()

    # ---- asking
    @objc.python_method
    def submit(self):
        question = self.input.stringValue().strip()
        if not question or self.answering:
            return
        if not self.recall or self.recall[-1] != question:
            self.recall = (self.recall + [question])[-100:]
        self.recall_at = None

        # /help — show the modifier cheatsheet in-panel; no API call.
        if question.lower() == "/help":
            self.input.setStringValue_("")
            self._show_help()
            return

        # /history — the last few saved answers; no API call.
        if question.lower() == "/history":
            self.input.setStringValue_("")
            self._show_history()
            return

        # `% ` — hand the line to PERCENT_COMMAND; no LLM.
        if question.startswith("% "):
            self._run_percent(question[2:].strip())
            return

        # Parse leading modifiers. A backslash is the "command" marker:
        #   backslashes → engine + skip-Wikipedia (0 Groq+Wiki · 1 Groq · 2 Gemini)
        #   attached flag letters (command mode only): s = Tavily search,
        #   w = Wiktionary, d = direct (and copied), c = copy, p = send the clipboard
        #   e.g.  \s  \\s  add Tavily   ·   \d  \\d  direct   ·   \wc  both
        groq_ok = bool(GROQ_API_KEY)
        backslashes = 0
        while question.startswith("\\") and backslashes < 2:
            question = question[1:]
            backslashes += 1

        flags = ""
        if backslashes >= 1:
            end = 0
            while end < len(question) and not question[end].isspace():
                end += 1
            if question[:end] and all(c in "sdwcp" for c in question[:end]):
                flags = question[:end]
                question = question[end:]
        question = question.strip()
        if not question:
            if flags:   # flags typed with no question after them: say so, never silently ignore
                self._hint("Type a query; your clipboard will be pasted alongside it"
                           if "p" in flags else "Type a question after the flags")
            return
        clip = ""
        if "p" in flags:
            clip = (NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString) or "").strip()
            if not clip:
                self._hint("Clipboard has no text — copy something first (⌘C)")
                return
        direct = "d" in flags
        copy_when_done = direct or "c" in flags

        engine = "gemini" if backslashes == 2 else "auto"
        searches = ["wiki"] if backslashes == 0 else [
            s for f, s in (("w", "wikt"), ("s", "web")) if f in flags]
        names = {"wiki": "Wikipedia", "wikt": "Wiktionary", "web": "Tavily"}
        if searches:
            indicator = "🔎 Searching %s…" % " + ".join(names[s] for s in searches)
        elif engine == "gemini" or not groq_ok:
            indicator = "🔎 Asking Gemini…"
        else:
            indicator = "🔎 Asking Groq…"

        # `p`: the clipboard rides along with the question (searches use the question alone).
        ask_text, echo = question, question
        if clip:
            ask_text = "%s\n\nClipboard:\n%s" % (question, clip[:3000])
            echo = "%s  (+ clipboard)" % question

        self._begin()
        session = self.session
        user_msg = {"role": "user", "parts": [{"text": ask_text}]}
        self.messages.append(user_msg)

        prefix = "\n\n" if self.answer.string() else ""
        self.appendText_("%s› %s\n\n" % (prefix, echo))
        self._grow()

        def chunk(text):
            self._on_main(session, self.appendText_, text)

        def worker():
            text, status, note, footer, sources = "", "failed", "", "", []
            try:
                # Build the request history; when searching, augment only the copy
                # sent to the model so the stored history stays the clean question.
                api_messages = [dict(m) for m in self.messages if m is not user_msg] + [user_msg]
                chunk(indicator + "\n\n")
                if searches:
                    contexts = []
                    if "wikt" in searches:
                        contexts.append(wiktionary_search(question, sources=sources))
                        if not contexts[-1]:
                            chunk("[no Wiktionary entry found]\n\n")
                    if "wiki" in searches or "web" in searches:
                        context, search_note = web_search(question, live=("web" in searches),
                                                          sources=sources)
                        contexts.append(context)
                        if search_note:
                            chunk(search_note + "\n\n")
                    context = "\n\n".join(c for c in contexts if c)
                    if context:
                        augmented = (
                            "Web results:\n%s\n\n"
                            "Using the results above, answer: %s" % (context, ask_text)
                        )
                        api_messages[-1] = {"role": "user", "parts": [{"text": augmented}]}
                    else:
                        chunk("[no web results — answering from general knowledge]\n\n")
                stats = {}
                text, status, note = llm_stream(api_messages, chunk, stats=stats,
                                                engine=engine, direct=direct)
                footer = self._quota_footer(stats, "web" if "web" in searches else None)
            except Exception as e:   # whatever happens, the panel is released below
                note = _redact("[error] %s" % e)
            finally:
                self._on_main(session, self._finish_answer, user_msg, text, status, note, footer,
                              question, sources, copy_when_done)

        threading.Thread(target=worker, daemon=True).start()

    @objc.python_method
    def _begin(self):
        """Start an operation: busy, field cleared, and no stale answer to copy (A6)."""
        self.answering = True
        self.input.setStringValue_("")
        self._typed_changed()
        self.last_answer = ""
        self.copybtn.setHidden_(True)

    @objc.python_method
    def _on_main(self, session, func, *args):
        """Run func(*args) on the main thread, unless the panel was reopened since
        `session` began (A2): an abandoned request never writes into a new one."""
        def run():
            if session == self.session:
                func(*args)
        AppHelper.callAfter(run)

    @objc.python_method
    def _finish_answer(self, user_msg, text, status, note, footer,
                       question="", sources=(), copy_when_done=False):
        """Everything that ends a question, in one main-thread step (A4)."""
        if status == "ok" and text.strip():
            self.messages.append({"role": "model", "parts": [{"text": text}]})
            self.last_answer = text.strip()
            self.copybtn.setHidden_(False)
            self._save_history(question, self.last_answer)
            if copy_when_done:
                self._copy_to_clipboard(self.last_answer)
                self.copybtn.setTitle_("✓")
                self.performSelector_withObject_afterDelay_("resetCopyTitle:", None, 1.0)
                footer = (footer + "   ·   copied") if footer else "\n\n— copied"
            self._append_sources(sources)
        else:
            # Not a complete answer (A7): shown, but neither kept for follow-ups nor copyable.
            if self.messages and self.messages[-1] is user_msg:
                self.messages.pop()
            if note:
                self.appendText_(("\n\n" if text else "") + note)
        self.appendFooter_(footer)
        self.answering = False
        close, self.close_when_done = self.close_when_done, False
        if close and self.last_answer:
            self._copy_to_clipboard(self.last_answer)
            self.hide()

    @objc.python_method
    def _save_history(self, question, answer):
        """Append the finished answer to HISTORY_FILE (blank = no history)."""
        if not HISTORY_FILE or not question:
            return
        try:
            os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
            with open(HISTORY_FILE, "a") as f:
                f.write("Q: %s\nA: %s\n\n" % (question, answer))
        except OSError:
            pass   # history is a convenience: never let it break an answer

    @objc.python_method
    def _show_history(self, count=10):
        """Show the last `count` saved answers. No API call."""
        try:
            with open(HISTORY_FILE) as f:
                f.seek(max(0, os.path.getsize(HISTORY_FILE) - 20000))   # the tail is enough
                tail = f.read()
        except (OSError, TypeError, ValueError):
            tail = ""
        entries = re.split(r"(?m)^(?=Q: )", tail)[1:][-count:]
        self.last_answer = ""
        self.copybtn.setHidden_(True)
        self.answer.setString_("")
        self._grow()
        if entries:
            self.appendText_("\n".join(e.strip() + "\n" for e in entries))
        else:
            self.appendText_("No history yet." if HISTORY_FILE else
                             "History is off: set HISTORY_FILE in derpderp_config.py.")

    @objc.python_method
    def _append_sources(self, sources, limit=5):
        """A dim line of clickable links to the pages the answer was built from."""
        seen, links = set(), []
        for title, url in sources:
            if url and url not in seen:
                seen.add(url)
                links.append((title if len(title) <= 40 else title[:39] + "…", url))
        if not links:
            return
        small = NSFont.systemFontOfSize_(12)
        grey = NSColor.secondaryLabelColor()
        line = NSMutableAttributedString.alloc().init()
        def add(text, attrs):
            line.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(text, attrs))
        add("\n\nSources: ", {NSFontAttributeName: small, NSForegroundColorAttributeName: grey})
        for n, (title, url) in enumerate(links[:limit]):
            if n:
                add("  ·  ", {NSFontAttributeName: small, NSForegroundColorAttributeName: grey})
            add(title, {NSFontAttributeName: small, NSLinkAttributeName: NSURL.URLWithString_(url)})
        storage = self.answer.textStorage()
        storage.appendAttributedString_(line)
        self.answer.scrollRangeToVisible_((storage.length(), 0))

    @objc.python_method
    def _run_percent(self, line):
        """Run PERCENT_COMMAND with `line` as its last argument and show what it printed."""
        if not line:
            return
        self._begin()
        session = self.session
        prefix = "\n\n" if self.answer.string() else ""
        self.appendText_("%s\u203a %% %s\n\n" % (prefix, line))
        self._grow()

        def worker():
            out = "% command failed"
            try:
                if not PERCENT_COMMAND:
                    out = "% lines are off: set PERCENT_COMMAND in derpderp_config.py."
                else:
                    r = subprocess.run(shlex.split(PERCENT_COMMAND) + [line],
                                       capture_output=True, text=True, timeout=20)
                    out = (r.stdout if r.returncode == 0 else r.stderr or r.stdout).strip()
                    out = out or ("done" if r.returncode == 0 else "failed (exit %d)" % r.returncode)
            except Exception as e:  # a bad command must never leave the panel stuck
                out = "%% command failed: %s" % e
            finally:
                self._on_main(session, self._finish_percent, out)

        threading.Thread(target=worker, daemon=True).start()

    @objc.python_method
    def _finish_percent(self, out):
        self.appendText_(out + "\n")
        self.answering = False

    # ---- `% ... #path` prediction (PERCENT_SUGGEST_URL, else PERCENT_SUGGEST_COMMAND)
    #
    # While a `% ` line holding a `#` is typed, the suggest command is asked (after
    # SUGGEST_DELAY of no typing) what the line would do. It takes the line (the
    # `% ` removed) as its last argument and prints one JSON line:
    #   {"mode": "add" | "remind" | "file", "day": "Mon 11-09" or null, "title": ...,
    #    "place": null, or for a `#` path {"state": "ok" | "many" | "missing" | "deep" | "empty",
    #      "path": "A › B", "partial": the last path part as typed,
    #      "suggest": [names the partial starts], "close": true when it starts none and
    #      "suggest" holds the closest names instead (a typo)}}
    # The first suggested name shows greyed after the cursor; ` takes it (plus a /),
    # ↑ / ↓ offer the others, and a hint line under the bar says what Enter will do.
    # With PERCENT_SUGGEST_URL set, a GET of that URL with ?q=<the same line> is tried first
    # (a running app answers in well under the command's start-up time); no answer within
    # SUGGEST_URL_TIMEOUT, or no JSON, and the command is asked as before.

    @objc.python_method
    def _typed_changed(self):
        """The field's text changed: drop any answer for older text, ask again after a pause."""
        self.suggest_seq += 1
        seq, line = self.suggest_seq, self.input.stringValue()
        self.ghost.setHidden_(True)
        if not ((PERCENT_SUGGEST_URL or PERCENT_SUGGEST_COMMAND) and line.startswith("% ") and "#" in line):
            self.typed = None
            self._show_hint()
            return
        AppHelper.callLater(SUGGEST_DELAY, self._ask_suggest, seq, line)

    @objc.python_method
    def _ask_suggest(self, seq, line):
        if seq != self.suggest_seq:
            return   # typed on since: a newer ask is pending
        session = self.session

        def worker():
            q, data = line[2:].strip(), None
            if PERCENT_SUGGEST_URL:
                try:
                    url = "%s%s%s" % (PERCENT_SUGGEST_URL, "&" if "?" in PERCENT_SUGGEST_URL else "?",
                                      urllib.parse.urlencode({"q": q}))
                    no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with no_proxy.open(url, timeout=SUGGEST_URL_TIMEOUT) as r:
                        data = json.loads(r.read().decode("utf-8"))
                except Exception:  # not running, too slow, or not JSON: ask the command
                    data = None
            if data is None and PERCENT_SUGGEST_COMMAND:
                try:
                    r = subprocess.run(shlex.split(PERCENT_SUGGEST_COMMAND) + [q],
                                       capture_output=True, text=True, timeout=5)
                    if r.returncode == 0:
                        data = json.loads(r.stdout.strip().splitlines()[0])
                except Exception:  # no hint is better than a stuck or crashed panel
                    data = None
            self._on_main(session, self._got_suggest, seq, line, data)

        threading.Thread(target=worker, daemon=True).start()

    @objc.python_method
    def _got_suggest(self, seq, line, data):
        if seq != self.suggest_seq or self.input.stringValue() != line:
            return   # an answer for text no longer in the field
        place = data.get("place") if isinstance(data, dict) else None
        ok = (data.get("mode") == "file" and isinstance(place, dict)
              and isinstance(place.get("suggest", []), list)) if place else False
        self.typed = dict(data, q=line) if ok else None
        self.choice = 0
        self._show_hint()

    @objc.python_method
    def _current_hint(self):
        """The suggest answer, only while it is for the text now in the field."""
        if self.typed and self.typed["q"] == self.input.stringValue():
            return self.typed
        return None

    @objc.python_method
    def _suggestion(self):
        names = (self.typed or {}).get("place", {}).get("suggest") or []
        return names[self.choice % len(names)] if names else None

    @objc.python_method
    def _show_hint(self):
        """Fill (or clear) the hint line and the greyed rest of the predicted name."""
        t = self._current_hint() if self.typed else None
        if not t:
            self.ghost.setHidden_(True)
            if self.typed is None:   # typed on with a hint up: keep it until the new answer
                self.tip.setHidden_(True)
                self._set_tip_h(0)
            return
        p, day, name = t["place"], t.get("day"), self._suggestion()
        names = p.get("suggest") or []
        more = ""
        if name and not day:
            more = (" · did you mean “%s”? ` takes it" % name if p.get("close")
                    else " · ` takes “%s”" % name)
            if len(names) > 1:
                more += " (%d of %d, ↑/↓)" % (self.choice % len(names) + 1, len(names))
        state, path = p.get("state"), p.get("path") or ""
        if state == "ok":
            tip = "→ %s · Enter files it there, on today%s" % (path, " · ⏰ " + day if day else "")
        elif state == "missing":
            tip = "%s is not made yet · Enter adds the line to Adhoc as typed" % path
        elif state == "many":
            tip = "“%s” starts more than one name: type more" % p.get("partial", "")
        elif state == "deep":
            tip = "%s goes past 6 levels: file it higher up" % path
        else:
            tip = "Type an Area’s name after the #"
        self.tip.setStringValue_(tip + more)
        self.tip.setHidden_(False)
        self._set_tip_h(TIP_H)

        # The greyed rest: only for a name the typed part starts, with the cursor at the end.
        line, partial = t["q"], p.get("partial") or ""
        editor = self.input.currentEditor()
        at_end = editor is None or (
            editor.selectedRange().length == 0
            and editor.selectedRange().location == len(line.encode("utf-16-le")) // 2)
        rest = (name[len(partial):] if name and not p.get("close") and not day
                and line.endswith(partial) and at_end else "")
        if not rest:
            self.ghost.setHidden_(True)
            return
        font = {NSFontAttributeName: self.input.font()}
        typed_w = NSAttributedString.alloc().initWithString_attributes_(line, font).size().width
        rest_w = NSAttributedString.alloc().initWithString_attributes_(rest, font).size().width + 8
        if typed_w + rest_w > self.input.frame().size.width:
            self.ghost.setHidden_(True)   # the line has scrolled: nowhere true to draw it
            return
        f = self.input.frame()
        self.ghost.setStringValue_(rest)
        self.ghost.setFrame_(NSMakeRect(f.origin.x + typed_w, f.origin.y, rest_w, f.size.height))
        self.ghost.setHidden_(False)

    @objc.python_method
    def _take_suggestion(self):
        """`: the offered name replaces the path's last part, then a /. False = nothing to take."""
        t, name = self._current_hint(), self._suggestion()
        if not t or not name or t.get("day"):
            return False
        line = t["q"]
        at = line.rfind("#")
        path = line[at + 1:]
        self.input.setStringValue_(line[:at + 1] + path[:path.rfind("/") + 1] + name + "/")
        editor = self.input.currentEditor()
        if editor is not None:
            editor.setSelectedRange_((len(self.input.stringValue().encode("utf-16-le")) // 2, 0))
        self._typed_changed()
        return True

    def controlTextDidChange_(self, notification):
        self._typed_changed()

    @objc.python_method
    def _quota_footer(self, stats, search):
        """A one-line quota readout appended (in italic) after each answer.

        Groq's numbers are exact (from response headers); Tavily and Gemini are
        local estimates (this script's own call count for the period), marked ~.
        """
        bits = []
        if stats.get("engine") == "groq":
            rr = stats.get("remaining_requests")
            lr = stats.get("limit_requests")
            rt = stats.get("remaining_tokens")
            if rr is not None:
                bits.append("Groq %s/%s req today" % (rr, lr or "?"))
            if rt is not None:
                seg = "%s tok/min left" % rt
                try:  # when the per-minute token bucket is low, note the refill
                    if int(float(rt)) < 2000 and stats.get("reset_tokens"):
                        seg += " (refills in %s)" % stats["reset_tokens"]
                except ValueError:
                    pass
                bits.append(seg)
        elif stats.get("engine") == "gemini":
            left = max(0, GEMINI_DAILY_LIMIT - usage_count("gemini", _gemini_day_key()))
            bits.append("Gemini ~%d/%d req today" % (left, GEMINI_DAILY_LIMIT))

        if search == "web" and TAVILY_API_KEY:
            left = max(0, TAVILY_MONTHLY_LIMIT - usage_count("tavily", _month_key()))
            bits.append("Tavily ~%d/%d this month" % (left, TAVILY_MONTHLY_LIMIT))

        return "\n\n\u2014 " + "   \u00b7   ".join(bits) if bits else ""

    @objc.python_method
    def _show_help(self):
        """Expand the panel and display the modifier cheatsheet. No API call."""
        self.last_answer = ""
        self.copybtn.setHidden_(True)
        self.answer.setString_("")
        self._grow()
        self.answer.textStorage().setAttributedString_(help_attributed())
        self.answer.scrollRangeToVisible_((0, 0))

    @objc.python_method
    def _hint(self, text):
        """A one-line note in the panel; the typed line stays in the field to finish."""
        prefix = "\n\n" if self.answer.string() else ""
        self._grow()
        self.appendText_(prefix + text)

    def appendText_(self, text):
        storage = self.answer.textStorage()
        self.answer.setEditable_(True)
        self.answer.setSelectedRange_((storage.length(), 0))
        self.answer.setTypingAttributes_(body_attributes())  # not the help's or footer's style
        self.answer.insertText_(text)
        self.answer.setEditable_(False)
        self.answer.scrollRangeToVisible_((storage.length(), 0))

    def appendFooter_(self, text):
        """Append the quota readout as dim italic text (its own attributes, so
        it doesn't affect the normal typing style of later answers)."""
        if not text:
            return
        italic = NSFontManager.sharedFontManager().convertFont_toHaveTrait_(
            NSFont.systemFontOfSize_(12), NSItalicFontMask
        )
        attrs = {
            NSFontAttributeName: italic,
            NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
        }
        note = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        storage = self.answer.textStorage()
        storage.appendAttributedString_(note)
        self.answer.scrollRangeToVisible_((storage.length(), 0))

    # ---- copy answer

    def copyAnswer_(self, sender):
        """Copy the last answer and briefly flash the button to confirm."""
        if not self.last_answer:
            return
        self._copy_to_clipboard(self.last_answer)
        self.copybtn.setTitle_("✓")
        self.performSelector_withObject_afterDelay_("resetCopyTitle:", None, 1.0)

    def resetCopyTitle_(self, _):
        self.copybtn.setTitle_("⧉")

    @objc.python_method
    def _copy_to_clipboard(self, text):
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)

    @objc.python_method
    def _has_selection(self):
        """True if the user has text selected (so ⌘C should do a normal copy)."""
        if self.answer.selectedRange().length > 0:
            return True
        editor = self.input.currentEditor()
        return editor is not None and editor.selectedRange().length > 0

    @objc.python_method
    def local_keydown(self, event):
        if not (self.panel.isVisible() and self.panel.isKeyWindow()):
            return event
        # Escape closes the panel from anywhere in it (the field, or the answer after a click).
        if event.keyCode() == ESCAPE_KEYCODE:
            self.hide()
            return None
        # ` takes the predicted name of a `% ... #path` line (else it types a `).
        if (
            event.characters() == "`"
            and not (event.modifierFlags() & NSEventModifierFlagCommand)
            and self._take_suggestion()
        ):
            return None
        # ⌘↩ on a typed line asks it, then copies the answer and closes once it's done.
        if (
            event.keyCode() in RETURN_KEYCODES
            and (event.modifierFlags() & NSEventModifierFlagCommand)
            and self.input.stringValue().strip()
            and not self.answering
        ):
            self.submit()
            self.close_when_done = self.answering   # a hint or /help asks nothing: stay open
            return None
        # ⌘↩ on an empty field copies the last answer and closes.
        if (
            event.keyCode() in RETURN_KEYCODES
            and (event.modifierFlags() & NSEventModifierFlagCommand)
            and self.last_answer
            and not self.input.stringValue().strip()
        ):
            self._copy_to_clipboard(self.last_answer)
            self.hide()
            return None
        # ⌘C with nothing selected copies the whole last answer.
        if (
            self.panel.isVisible()
            and (event.modifierFlags() & NSEventModifierFlagCommand)
            and event.charactersIgnoringModifiers() == "c"
            and self.last_answer
            and not self._has_selection()
        ):
            self._copy_to_clipboard(self.last_answer)
            self.copybtn.setTitle_("✓")
            self.performSelector_withObject_afterDelay_("resetCopyTitle:", None, 1.0)
            return None  # consume the event
        # ⌘A / ⌘C / ⌘V / ⌘X: a menu-less background app gets no Edit menu to route these,
        # so send them to whatever has focus (the question field or the answer text).
        if event.modifierFlags() & NSEventModifierFlagCommand:
            action = EDIT_SHORTCUTS.get(event.charactersIgnoringModifiers())
            if action and NSApplication.sharedApplication().sendAction_to_from_(action, None, self):
                return None
        return event

    # ---- text field delegate (handles Return & Escape directly)

    def control_textView_doCommandBySelector_(self, control, textView, commandSelector):
        sel = str(commandSelector)
        if sel == "cancelOperation:":
            self.hide()
            return True
        if sel == "insertNewline:":
            self.submit()
            return True
        t = self._current_hint()
        if sel in ("moveUp:", "moveDown:") and t and len(t["place"].get("suggest") or []) > 1:
            # a `#` hint with several names: ↑ / ↓ offer the others, ahead of recall
            self.choice += -1 if sel == "moveUp:" else 1
            self._show_hint()
            return True
        if sel in ("moveUp:", "moveDown:") and self.recall:
            # ↑ / ↓ step through the lines asked since launch; past the newest, a blank field.
            at = len(self.recall) if self.recall_at is None else self.recall_at
            at = max(0, at - 1) if sel == "moveUp:" else at + 1
            if at >= len(self.recall):
                self.recall_at = None
                self.input.setStringValue_("")
            else:
                self.recall_at = at
                self.input.setStringValue_(self.recall[at])
            editor = self.input.currentEditor()
            if editor is not None:
                editor.setSelectedRange_((len(self.input.stringValue()), 0))
            self._typed_changed()
            return True
        return False

    # ---- key handling

    @objc.python_method
    def check_double_tap(self, event):
        """Toggle on two quick, clean taps of the hotkey (A5). A tap is the hotkey
        itself going down and up with no other key typed meanwhile, so ⌘C / ⌘V
        typed with it, or the other ⌘ held down, never count."""
        if event.keyCode() != RIGHT_CMD_KEYCODE:
            self.tap_time = 0.0   # another modifier moved: not a clean double tap
            return
        mask = DEVICE_MASKS.get(RIGHT_CMD_KEYCODE)
        down = bool(event.modifierFlags() & mask) if mask else not self.hotkey_down
        if down == self.hotkey_down:
            return
        self.hotkey_down = down
        now, keys = time.time(), keydown_count()
        if down:
            if self.tap_time and now - self.tap_time < DOUBLE_TAP_TIMEOUT and keys == self.tap_keys:
                self.tap_time = 0.0
                self.toggle()
            else:
                self.tap_time = 0.0
                self.press_time, self.press_keys = now, keys
        elif keys == self.press_keys:   # released with nothing typed: one clean tap
            self.tap_time, self.tap_keys = self.press_time, keys
        else:
            self.tap_time = 0.0

    @objc.python_method
    def global_flags(self, event):
        self.check_double_tap(event)

    @objc.python_method
    def local_flags(self, event):
        self.check_double_tap(event)
        return event


# ---------------------------------------------------------------- main

_instance_lock = None  # kept open for the process lifetime to hold the lock


def _acquire_single_instance():
    """Return True if we got the lock; False if another instance already holds
    it. Prevents duplicate global hotkey listeners (e.g. login agent + manual)."""
    global _instance_lock
    try:
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        _instance_lock = open(LOCK_FILE, "w")
        fcntl.flock(_instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def main():
    if not _acquire_single_instance():
        print("derpderp is already running; exiting this instance.")
        return

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    controller = Controller.alloc().init()

    # Track modifier flag changes (Right Cmd) globally and locally
    NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
        NSEventMaskFlagsChanged, controller.global_flags
    )
    NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
        NSEventMaskFlagsChanged, controller.local_flags
    )
    # ⌘C (with nothing selected) copies the whole last answer.
    NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
        NSEventMaskKeyDown, controller.local_keydown
    )

    app.run()


if __name__ == "__main__":
    main()


# ============================================================================
#  OPTIONAL: launch automatically at login
# ============================================================================
#
# Create a LaunchAgent so the panel is always running after you log in. In a
# terminal, run these (they fill in your own username/paths automatically):
#
#   PLIST="$HOME/Library/LaunchAgents/com.derpderp.agent.plist"
#   PY="$(command -v python3)"
#   SCRIPT="$HOME/path/to/derpderp.py"   # <- adjust to where you saved it
#   cat > "$PLIST" <<EOF
#   <?xml version="1.0" encoding="UTF-8"?>
#   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
#   <plist version="1.0"><dict>
#     <key>Label</key><string>com.derpderp.agent</string>
#     <key>ProgramArguments</key><array><string>$PY</string><string>$SCRIPT</string></array>
#     <key>RunAtLoad</key><true/>
#     <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
#     <key>ProcessType</key><string>Interactive</string>
#     <key>StandardOutPath</key><string>$HOME/Library/Logs/derpderp.log</string>
#     <key>StandardErrorPath</key><string>$HOME/Library/Logs/derpderp.log</string>
#   </dict></plist>
#   EOF
#   launchctl bootstrap gui/$(id -u) "$PLIST"
#
# To reload after editing this file:
#   launchctl kickstart -k gui/$(id -u)/com.derpderp.agent
# To turn it off:
#   launchctl bootout gui/$(id -u) "$HOME/Library/LaunchAgents/com.derpderp.agent.plist"
