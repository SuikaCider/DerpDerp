# DerpDerp settings.
#
# Copy this file to derpderp_config.py, in the same folder as derpderp.py, and edit
# that copy. Your copy stays yours: git ignores it, and updating derpderp.py never
# touches it. Delete any line you don't change; derpderp.py has the same defaults
# built in. Without a derpderp_config.py, DerpDerp runs on those defaults.

# --- API keys: paste yours between the quotes ----------------------------------
#   Groq   (required): free key at  https://console.groq.com     → "gsk_..."
#   Tavily (optional): free key at  https://app.tavily.com       → "tvly-..."
#                      (enables the \s live web-search modifier)
#   Gemini (optional): free key at  https://aistudio.google.com  → "AIza..."
#                      (enables the \\ Gemini engine)
#
# You can leave any of these blank. With just a Groq key you still get the
# default engine, Wikipedia context, and every modifier except \s.
# Rather keep keys out of files? Set GROQ_POPUPSEARCH, TAVILY_POPUPSEARCH or
# GEMINI_POPUPSEARCH in your environment, or as `export NAME="..."` lines in
# ~/.secrets; those win over the lines below.
GROQ_API_KEY   = ""
TAVILY_API_KEY = ""
GEMINI_API_KEY = ""

# --- Models: change only if your account can't access these -----------------
GROQ_MODEL   = "openai/gpt-oss-120b"     # your available models: console.groq.com
# (a 404 error means your account can't use this one; pick another at console.groq.com)
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
USAGE_FILE = "~/.cache/derpderp_usage.json"
LOCK_FILE  = "~/.cache/derpderp.lock"
# Every finished answer is appended here as "Q: ..." / "A: ..." (blank = no history).
HISTORY_FILE = "~/.cache/derpderp_history.txt"

# --- Panel ------------------------------------------------------------------
KEEP_ANSWER_MINUTES = 5        # reopen within this long and the last answer is still there

# --- `% ` lines (optional; blank = off) --------------------------------------
# A line typed as `% buy milk` runs this command with "buy milk" added as its
# last argument, and shows the command's printed output in the panel: a hook
# for your own note-taking or to-do tool. Example:
#   PERCENT_COMMAND = "python3 /path/to/add_note.py"
# A DERPDERP_PERCENT_COMMAND environment variable (or ~/.secrets line) wins.
PERCENT_COMMAND = ""
