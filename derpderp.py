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
    \d \\d    direct: a bare, paste-ready plaintext answer

──────────────────────────────────────────────────────────────────────────
SETUP (3 steps)
  1. Install the dependency:
        pip3 install pyobjc-framework-Cocoa
  2. Paste at least one API key into the CONFIG section just below
     (a free Groq key alone is enough to get started).
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
import fcntl
import json
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

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
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorTransient,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSObject, NSAttributedString

# ============================================================================
#  CONFIG  ───  EDIT THIS SECTION
# ============================================================================

# --- API keys: paste yours between the quotes on lines 90, 91, 92-------------------------------
#   Groq   (required): free key at  https://console.groq.com     → "gsk_..."
#   Tavily (optional): free key at  https://app.tavily.com       → "tvly-..."
#                      (enables the \s live web-search modifier)
#   Gemini (optional): free key at  https://aistudio.google.com  → "AIza..."
#                      (enables the \\ Gemini engine)
#
# You can leave any of these blank. With just a Groq key you still get the
# default engine, Wikipedia context, and every modifier except \s.
GROQ_API_KEY   = ""
TAVILY_API_KEY = ""
GEMINI_API_KEY = ""

# --- Models: change only if your account can't access these -----------------
GROQ_MODEL   = "openai/gpt-oss-120b"     # your available models: console.groq.com
GEMINI_MODEL = "gemini-3.5-flash-lite"   # a free model whose search grounding is off

# --- Free-tier caps: only for the quota readout under each answer ------------
#   (Groq's real remaining count comes live from its response headers; these
#    two are just local estimates for Gemini and Tavily.)
GEMINI_DAILY_LIMIT   = 500     # requests/day
TAVILY_MONTHLY_LIMIT = 1000    # searches/month

# --- Hotkey -----------------------------------------------------------------
RIGHT_CMD_KEYCODE  = 54        # double-tap this key to toggle (54 = Right ⌘)
DOUBLE_TAP_TIMEOUT = 0.4       # max seconds between the two taps

# --- Local files (fine to leave as-is) --------------------------------------
USAGE_FILE = os.path.expanduser("~/.cache/derpderp_usage.json")
LOCK_FILE  = os.path.expanduser("~/.cache/derpderp.lock")

# ============================================================================
#  END OF CONFIG  ───  you shouldn't need to edit below here
# ============================================================================

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
    "%s:streamGenerateContent?alt=sse&key=%s" % (GEMINI_MODEL, GEMINI_API_KEY)
)

PANEL_W = 620
INPUT_H = 54        # height of the question bar
FIELD_H = 28       # natural height of the text field, centered within the bar
ANSWER_H = 380
SIDE = 16          # horizontal inset shared by input, separator and answer text

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
HELP_TEXT = (
    "SEARCH ENGINES\n"
    "␣    Groq response w/ Wikipedia context\n"
    "\\    Groq response, no context or web search\n"
    "\\\\   Gemini response, no context or web search\n"
    "\n"
    "\n"
    "MODIFIERS  (directly to \\ or \\\\)\n"
    "s  add web search via Tavily\n"
    "d  direct plaintext answer, should be pastable\n"
)


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


def _today_key():
    return datetime.now().astimezone().strftime("%Y-%m-%d")


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


def wiki_search(query, max_titles=3, char_cap=1200, timeout=15):
    """Free, keyless web lookup via the Wikipedia API.

    Returns a plain-text context block of intro extracts for the top matching
    articles, or "" on no hit / failure. This sidesteps Gemini's Google Search
    grounding (which is 0-quota on the free tier for Gemini 3.x). It's good for
    factual/encyclopedic questions; it will NOT have live data such as today's
    news, sports scores, or current prices.
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
        return "\n\n".join(blocks)
    except Exception:
        return ""


def tavily_search(query, max_results=5, char_cap=800, timeout=20):
    """Live web search via Tavily (needs TAVILY_API_KEY; 1000/mo free).

    Returns a plain-text context block (answer summary + top results), or ""
    if there's no key or the call fails. Unlike Wikipedia this covers current
    events, prices, weather, etc.
    """
    key = TAVILY_API_KEY
    if not key:
        return ""
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
        bump_usage("tavily", _month_key())  # a credit is spent on any 200
        blocks = []
        if data.get("answer"):
            blocks.append("Summary: " + data["answer"])
        for r in data.get("results", []):
            blocks.append(
                "## %s\n%s\n<%s>"
                % (r.get("title", ""), (r.get("content") or "")[:char_cap], r.get("url", ""))
            )
        return "\n\n".join(blocks)
    except Exception:
        return ""


def web_search(query, live=False):
    """Fetch context for a question. live=True prefers Tavily (current events),
    falling back to Wikipedia; live=False uses Wikipedia only."""
    if live:
        return tavily_search(query) or wiki_search(query)
    return wiki_search(query)


# ---------------------------------------------------------------- LLM
#
# Two backends share one gemini-style message history ([{role, parts:[{text}]}],
# role "user"/"model"). llm_stream() picks Groq when its key is present.


def llm_stream(messages, on_chunk, on_done, stats=None, engine="auto", direct=False):
    """Stream an answer from the chosen engine.

    engine="auto" prefers Groq when its key is present, else Gemini;
    engine="gemini" forces Gemini (the \\\\ lane). direct=True asks for a bare,
    paste-ready answer. `stats` is populated with which engine ran and, for Groq,
    the remaining counts from the headers.
    """
    groq_key = GROQ_API_KEY
    if engine == "auto":
        engine = "groq" if groq_key else "gemini"

    if engine == "groq" and groq_key:
        if stats is not None:
            stats["engine"] = "groq"
        stream_openai(GROQ_URL, groq_key, GROQ_MODEL, messages, on_chunk, on_done,
                      stats=stats, direct=direct)
    else:
        if stats is not None:
            stats["engine"] = "gemini"
        stream(messages, on_chunk, on_done, stats=stats, direct=direct)


def stream_openai(url, key, model, messages, on_chunk, on_done, extra_headers=None,
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

    collected = []
    for attempt in range(retries):
        collected = []
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
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    try:
                        text = event["choices"][0]["delta"].get("content")
                        if text:
                            collected.append(text)
                            on_chunk(text)
                    except (KeyError, IndexError):
                        pass
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                on_chunk("[rate limited, retrying in 3s…]\n")
                time.sleep(3)
                continue
            on_chunk("[HTTP %s] %s" % (e.code, e.read().decode("utf-8", "replace")[:200]))
            break
        except Exception as e:
            on_chunk("[error] %s" % e)
            break

    on_done("".join(collected))

def stream(messages, on_chunk, on_done, retries=2, stats=None, direct=False):
    """Stream an answer from the Gemini API (fallback engine).

    No Google Search grounding is attached — it's 0-quota on the free tier for
    Gemini 3.x, so live info is supplied via web_search() instead (see submit).
    """
    request_body = {
        "systemInstruction": {"parts": [{"text": _system_prompt(direct)}]},
        "contents": messages,
    }
    body = json.dumps(request_body).encode()
    collected = []

    for attempt in range(retries):
        # Discard any partial text from a failed earlier attempt so the answer
        # stored in history only reflects the successful stream.
        collected = []
        req = urllib.request.Request(
            GEMINI_URL, data=body, headers={"content-type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
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

                    try:
                        text = event["candidates"][0]["content"]["parts"][0]["text"]
                        if text:
                            collected.append(text)
                            on_chunk(text)
                    except (KeyError, IndexError):
                        pass
            # Success: break out of the retry loop
            bump_usage("gemini", _today_key())
            break

        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                on_chunk("[rate limited, retrying in 3s…]\n")
                time.sleep(3)
                continue
            body_txt = e.read().decode("utf-8", "replace")
            if e.code == 429:
                on_chunk(
                    "[HTTP 429] Gemini free-tier daily quota reached "
                    "(%d requests/day). Try again tomorrow." % GEMINI_DAILY_LIMIT
                )
            else:
                on_chunk("[HTTP %s] %s" % (e.code, body_txt[:200]))
            break
        except Exception as e:
            on_chunk("[error] %s" % e)
            break

    on_done("".join(collected))


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
        self.last_right_cmd_time = 0.0
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
        legend = "␣ Check Wikipedia    \\ Ask Groq    \\\\ Ask Gemini  …  /help for more"
        self.input.setPlaceholderAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(
                legend,
                {
                    NSFontAttributeName: NSFont.systemFontOfSize_(13),
                    NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
                },
            )
        )
        self.input.setAutoresizingMask_(2 | 8)

        # Set the controller as the delegate to intercept typing commands (Return, Escape)
        self.input.setDelegate_(self)

        blur.addSubview_(self.input)

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
        x = vf.origin.x + (vf.size.width - PANEL_W) / 2
        y = vf.origin.y + vf.size.height * 0.72 - height
        self.panel.setFrame_display_animate_(
            NSMakeRect(x, y, PANEL_W, height), True, False
        )
        # Question bar occupies the top INPUT_H of the panel; the field is
        # centered within it and the answer fills everything below.
        bar_bottom = height - INPUT_H
        self.input.setFrameOrigin_((SIDE, bar_bottom + (INPUT_H - FIELD_H) / 2.0))
        self.separator.setFrameOrigin_((SIDE, bar_bottom))
        self.scroll.setFrame_(NSMakeRect(0, 0, PANEL_W, bar_bottom))
        self.copybtn.setFrameOrigin_((PANEL_W - SIDE - 26, bar_bottom - 28))

    @objc.python_method
    def _grow(self):
        if self.scroll.isHidden():
            self.scroll.setHidden_(False)
            self.separator.setHidden_(False)
            self._place(INPUT_H + ANSWER_H)

    # ---- show / hide
    @objc.python_method
    def show(self):
        self.messages = []
        self.last_answer = ""
        self.answer.setString_("")
        self.scroll.setHidden_(True)
        self.separator.setHidden_(True)
        self.copybtn.setHidden_(True)
        self.copybtn.setTitle_("⧉")
        self.input.setStringValue_("")
        self._place(INPUT_H)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        self.panel.makeFirstResponder_(self.input)

    @objc.python_method
    def hide(self):
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

        # /help — show the modifier cheatsheet in-panel; no API call.
        if question.lower() == "/help":
            self.input.setStringValue_("")
            self._show_help()
            return

        # Parse leading modifiers. A backslash is the "command" marker:
        #   backslashes → engine + skip-Wikipedia (0 Groq+Wiki · 1 Groq · 2 Gemini)
        #   attached flag letters (command mode only): s = Tavily search, d = direct
        #   e.g.  \s  \\s  add Tavily   ·   \d  \\d  direct   ·   \sd  both
        groq_ok = bool(GROQ_API_KEY)
        backslashes = 0
        while question.startswith("\\") and backslashes < 2:
            question = question[1:]
            backslashes += 1

        want_tavily = False
        direct = False
        if backslashes >= 1:
            end = 0
            while end < len(question) and not question[end].isspace():
                end += 1
            flags = question[:end]
            if flags and all(c in "sd" for c in flags):
                want_tavily = "s" in flags
                direct = "d" in flags
                question = question[end:]
        question = question.strip()
        if not question:
            return

        engine = "gemini" if backslashes == 2 else "auto"
        if backslashes == 0:
            search = "wiki"
        elif want_tavily:
            search = "web"
        else:
            search = None

        if search == "wiki":
            indicator = "\ud83d\udd0e Searching Wikipedia\u2026"
        elif search == "web":
            indicator = "\ud83d\udd0e Searching Tavily\u2026"
        elif engine == "gemini" or not groq_ok:
            indicator = "\ud83d\udd0e Asking Gemini\u2026"
        else:
            indicator = "\ud83d\udd0e Asking Groq\u2026"

        self.answering = True
        self.input.setStringValue_("")

        self.messages.append({"role": "user", "parts": [{"text": question}]})

        prefix = "\n\n" if self.answer.string() else ""
        self.appendText_("%s\u203a %s\n\n" % (prefix, question))
        self._grow()

        def chunk(text):
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "appendText:", text, False
            )

        def done(full):
            self.messages.append({"role": "model", "parts": [{"text": full}]})
            self.answering = False
            if full.strip():
                self.last_answer = full.strip()
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "showCopyButton:", None, False
                )

        def worker():
            # Build the request history; when searching, augment only the copy
            # sent to the model so the stored history stays the clean question.
            api_messages = [dict(m) for m in self.messages]
            chunk(indicator + "\n\n")
            if search in ("wiki", "web"):
                context = web_search(question, live=(search == "web"))
                if context:
                    augmented = (
                        "Web results:\n%s\n\n"
                        "Using the results above, answer: %s" % (context, question)
                    )
                    api_messages[-1] = {"role": "user", "parts": [{"text": augmented}]}
                else:
                    chunk("[no web results \u2014 answering from general knowledge]\n\n")

            stats = {}
            llm_stream(api_messages, chunk, done, stats=stats, engine=engine, direct=direct)
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "appendFooter:", self._quota_footer(stats, search), False
            )

        threading.Thread(target=worker, daemon=True).start()

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
            left = max(0, GEMINI_DAILY_LIMIT - usage_count("gemini", _today_key()))
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
        self.appendText_(HELP_TEXT)
        self.answer.scrollRangeToVisible_((0, 0))

    def appendText_(self, text):
        storage = self.answer.textStorage()
        self.answer.setEditable_(True)
        self.answer.setSelectedRange_((storage.length(), 0))
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

    def showCopyButton_(self, _):
        self.copybtn.setHidden_(False)

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
        return False

    # ---- key handling

    @objc.python_method
    def check_double_tap(self, event):
        if event.keyCode() == RIGHT_CMD_KEYCODE:
            if event.modifierFlags() & NSEventModifierFlagCommand:
                now = time.time()
                if now - self.last_right_cmd_time < DOUBLE_TAP_TIMEOUT:
                    self.toggle()
                    self.last_right_cmd_time = 0.0
                else:
                    self.last_right_cmd_time = now

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
