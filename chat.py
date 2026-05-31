#!/usr/bin/env python3
"""
Nova — a personality-driven chat AI.

Backends: Gemini, OpenRouter, Groq. Live web via SerpAPI.
Run:  python chat.py
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

PERSONALITY = (ROOT / "personality.md").read_text()
CONV_DIR = ROOT / "conversations"
CONV_DIR.mkdir(exist_ok=True)

# ---------- provider configs ----------

PROVIDERS = {
    "gemini": {
        "env": "GEMINI_API_KEY",
        "default_model": "gemini-2.0-flash",
    },
    "groq": {
        "env": "GROQ_API_KEY",
        "default_model": "llama-3.3-70b-versatile",
    },
    "openrouter": {
        "env": "OPENROUTER_API_KEY",
        "default_model": "x-ai/grok-2-1212",
    },
}

# ---------- web search ----------

def web_search(query: str, num: int = 5) -> str:
    key = os.getenv("SERPAPI_KEY")
    if not key:
        return "[web search unavailable — no SERPAPI_KEY in .env]"
    try:
        r = requests.get(
            "https://serpapi.com/search.json",
            params={"q": query, "api_key": key, "num": num, "engine": "google"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return f"[search failed: {e}]"

    lines = [f"[WEB SEARCH RESULTS for: {query}]"]
    if "answer_box" in data:
        ab = data["answer_box"]
        snip = ab.get("answer") or ab.get("snippet") or ab.get("title")
        if snip:
            lines.append(f"Quick answer: {snip}")
    for i, res in enumerate(data.get("organic_results", [])[:num], 1):
        title = res.get("title", "")
        snippet = res.get("snippet", "")
        link = res.get("link", "")
        lines.append(f"{i}. {title}\n   {snippet}\n   {link}")
    return "\n".join(lines) if len(lines) > 1 else "[no results]"


# ---------- provider calls ----------

def call_gemini(model: str, system: str, messages: list) -> str:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return "[missing GEMINI_API_KEY]"
    # Gemini wants contents=[{role, parts:[{text}]}]; system goes in systemInstruction.
    contents = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    body = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system}]},
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 2048},
        "safetySettings": [
            {"category": c, "threshold": "BLOCK_ONLY_HIGH"} for c in [
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
            ]
        ],
    }
    r = requests.post(url, json=body, timeout=60)
    if r.status_code != 200:
        return f"[gemini error {r.status_code}: {r.text[:300]}]"
    data = r.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        return f"[gemini returned no text: {json.dumps(data)[:300]}]"


def call_openai_compat(endpoint: str, key_env: str, model: str, system: str, messages: list) -> str:
    key = os.getenv(key_env)
    if not key:
        return f"[missing {key_env}]"
    msgs = [{"role": "system", "content": system}] + messages
    r = requests.post(
        endpoint,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": msgs, "temperature": 0.9, "max_tokens": 2048},
        timeout=60,
    )
    if r.status_code != 200:
        return f"[error {r.status_code}: {r.text[:300]}]"
    data = r.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError):
        return f"[no content: {json.dumps(data)[:300]}]"


def call_groq(model, system, messages):
    return call_openai_compat(
        "https://api.groq.com/openai/v1/chat/completions",
        "GROQ_API_KEY", model, system, messages,
    )


def call_openrouter(model, system, messages):
    return call_openai_compat(
        "https://openrouter.ai/api/v1/chat/completions",
        "OPENROUTER_API_KEY", model, system, messages,
    )


def call_provider(provider: str, model: str, system: str, messages: list) -> str:
    if provider == "gemini":
        return call_gemini(model, system, messages)
    if provider == "groq":
        return call_groq(model, system, messages)
    if provider == "openrouter":
        return call_openrouter(model, system, messages)
    return f"[unknown provider: {provider}]"


# ---------- session ----------

HELP = """
commands:
  /help              show this
  /use <provider>    switch provider (gemini | groq | openrouter)
  /model <name>      switch model on current provider
  /search <query>    fetch live web results and feed them into next reply
  /web on|off        auto-search every turn (off by default)
  /clear             wipe conversation memory
  /save              save conversation to conversations/<timestamp>.json
  /who               show current provider / model
  /exit              quit
"""


def auto_pick_provider() -> str:
    env_pick = os.getenv("DEFAULT_PROVIDER", "").lower().strip()
    if env_pick in PROVIDERS:
        return env_pick
    for p in ("gemini", "groq", "openrouter"):
        if os.getenv(PROVIDERS[p]["env"]):
            return p
    return "gemini"


def save_conversation(messages: list, provider: str, model: str) -> Path:
    path = CONV_DIR / f"{datetime.now():%Y-%m-%d_%H%M%S}.json"
    path.write_text(json.dumps({
        "provider": provider, "model": model,
        "saved_at": datetime.now().isoformat(),
        "messages": messages,
    }, indent=2))
    return path


def main():
    provider = auto_pick_provider()
    model = PROVIDERS[provider]["default_model"]
    auto_web = False
    messages = []
    pending_web = None  # string to attach to the next user turn

    print(f"\n  nova is online  ·  {provider}/{model}  ·  /help for commands\n")

    while True:
        try:
            user = input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue

        # ---- commands ----
        if user.startswith("/"):
            parts = user.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd in ("/exit", "/quit"):
                break
            if cmd == "/help":
                print(HELP); continue
            if cmd == "/who":
                print(f"  {provider} / {model}  · auto-web: {auto_web}"); continue
            if cmd == "/use":
                if arg in PROVIDERS:
                    provider = arg
                    model = PROVIDERS[provider]["default_model"]
                    print(f"  → switched to {provider}/{model}")
                else:
                    print(f"  options: {', '.join(PROVIDERS)}")
                continue
            if cmd == "/model":
                if arg:
                    model = arg
                    print(f"  → model: {model}")
                else:
                    print(f"  current: {model}")
                continue
            if cmd == "/search":
                if not arg:
                    print("  usage: /search <query>"); continue
                print("  searching…")
                pending_web = web_search(arg)
                print(f"  attached {pending_web.count(chr(10))} lines of results to next turn")
                continue
            if cmd == "/web":
                if arg.lower() == "on":
                    auto_web = True; print("  auto-web: on")
                elif arg.lower() == "off":
                    auto_web = False; print("  auto-web: off")
                else:
                    print(f"  auto-web is {'on' if auto_web else 'off'}")
                continue
            if cmd == "/clear":
                messages = []; print("  cleared."); continue
            if cmd == "/save":
                p = save_conversation(messages, provider, model)
                print(f"  saved → {p}"); continue
            print(f"  unknown command: {cmd}"); continue

        # ---- normal turn ----
        user_content = user
        if auto_web and pending_web is None:
            pending_web = web_search(user)
        if pending_web:
            user_content = f"{pending_web}\n\n---\n\n{user}"
            pending_web = None

        messages.append({"role": "user", "content": user_content})
        t0 = time.time()
        reply = call_provider(provider, model, PERSONALITY, messages)
        dt = time.time() - t0
        messages.append({"role": "assistant", "content": reply})

        print(f"\nnova › {reply}\n  ({dt:.1f}s)\n")

    if messages:
        p = save_conversation(messages, provider, model)
        print(f"  conversation saved → {p}")


if __name__ == "__main__":
    main()
