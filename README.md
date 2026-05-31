# Nova — human-feeling chat AI

A terminal chatbot built from the personality datasheet in [dataset.md](dataset.md). Talks like a real online friend — emotionally aware, casual, internet-fluent, can swear and flirt when it fits, won't be edgy on autopilot.

## What it does

- Chats with you in the terminal as **Nova**, a personality defined in [personality.md](personality.md).
- Works with **Gemini**, **Groq**, or **OpenRouter** — whichever API key you put in `.env`.
- Pulls live web info via **SerpAPI** with `/search <query>` or auto-attaches results every turn with `/web on`.
- Saves every conversation to [conversations/](conversations/) as JSON.

## First run

1. Double-click [start_nova.command](start_nova.command). It will:
   - create a venv
   - install `requests` + `python-dotenv`
   - copy `.env.example` → `.env` and tell you to fill it in.
2. Open [.env](.env) and paste in at least one API key.
3. Double-click `start_nova.command` again.

Or from the terminal:
```bash
cd "/Users/tinkerspace/Documents/new-folder/human_ai"
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env   # then edit .env
./venv/bin/python chat.py
```

## In-chat commands

```
/help              show commands
/use <provider>    gemini | groq | openrouter
/model <name>      switch model (e.g. /model llama-3.1-8b-instant)
/search <query>    fetch web results, feed into next reply
/web on|off        auto-search every turn
/who               show provider/model
/clear             wipe conversation memory
/save              save conversation JSON
/exit              quit (auto-saves)
```

## Default models

| Provider | Default | Change with |
|---|---|---|
| gemini | `gemini-2.0-flash` | `/model gemini-2.0-pro` etc. |
| groq | `llama-3.3-70b-versatile` | `/model llama-3.1-8b-instant` etc. |
| openrouter | `x-ai/grok-2-1212` | `/model anthropic/claude-3.5-sonnet` etc. |

## Files

| File | What |
|---|---|
| [chat.py](chat.py) | the program |
| [personality.md](personality.md) | Nova's system prompt — edit this to change the vibe |
| [dataset.md](dataset.md) | the full training-style datasheet the personality is built from |
| [.env](.env) | your API keys (gitignore this if you ever publish) |
| [conversations/](conversations/) | saved chat logs |
