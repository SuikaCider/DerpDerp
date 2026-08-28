# Troubleshooting 

If you're seeing this page, that probably means something didn't work. Sorry pal. I have no idea what I'm doing and probably couldn't help you if you told me what was going on.

To help you help yourself, I've prepared `ask-llm-for-help.md`. It's a quick technical introduction to DerpDerp to hopefully help your LLM make sense of things. 

> 🙋 This script was built using Claude Opus 4.8 with thinking mode set to High, so I recommend using that. It's great. At least, it got things running for me, so it probably will for you, too.

Anyway, to help yourself:
1. Ensure your LLM has access to `derpderp.py` and `ask-llm-for-help.md`, or just directly upload them
2. Copy the below prompt and paste it into your LLM
3. After the divider, insert your question/problem/whatever
4. If you have multiple questions, you can just type your question; no need to redo 1–3 each time

```
Hi, LLM friend! 

I've downloaded a python script called DerpDerp that serves quick LLM responses to a user by routing queries to a few free/mium APIs through NSPanel. Unfortunately, it's not working. 

Attached are:
- The script itself, `derpderp.py`
- Technical context for you, `ask-llm-for-help.md`
- What I'd like you to do, located just below

[START REQUEST]

yourrequesthereandstuff

```