#!/usr/bin/env python3
"""
Vokk AI — full-stack Flask web application
by Nibra Cyber, a technological branch of Nibra Ecos

Features:
  - Multi-provider LLM routing: Gemini, Groq, OpenRouter with fallback chain
  - Auto-routing by intent: NSFW → uncensored model, thinking → deepseek, image → gemini
  - Cinematic content moderation: BLOCKED markers, censor bar UI
  - Real auth: email+password, OTP (email or dev-mode display)
  - Persistent conversation history per user (SQLite)
  - Tools: web search (SerpAPI), scrape (ScrapeGraphAI), song (AudD),
            crypto (CoinGecko), image gen (Gemini multimodal)
  - Bignice AI autonomous agent integration
  - VokkScript: custom language support in personality
  - Conversation export, search, soft rate limiting
  - All safety settings at BLOCK_NONE for Gemini — personality handles rules
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import (
    Flask, Response, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

PERSONALITY    = (ROOT / "personality.md").read_text()
IMG_DIR        = ROOT / "generated_images"
DB_PATH        = ROOT / "nova.db"
CONV_DIR       = ROOT / "conversations"     # legacy, kept for compat

IMG_DIR.mkdir(exist_ok=True)
CONV_DIR.mkdir(exist_ok=True)

PORT        = int(os.getenv("PORT", "5555"))
BIGNICE_PATH = (
    Path(os.getenv("BIGNICE_PATH", "")).expanduser()
    if os.getenv("BIGNICE_PATH") else None
)

# ── DB ────────────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create all tables on first run. Safe to call multiple times."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT    UNIQUE NOT NULL,
                password_hash TEXT,
                display_name  TEXT,
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_seen     DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS otps (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                email      TEXT    NOT NULL,
                code       TEXT    NOT NULL,
                expires_at TEXT    NOT NULL,
                used       INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
                title      TEXT    DEFAULT 'New chat',
                provider   TEXT    DEFAULT 'auto',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
                role            TEXT    NOT NULL,
                content         TEXT    NOT NULL,
                provider_used   TEXT,
                elapsed_ms      INTEGER,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS rate_limits (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
                endpoint   TEXT    NOT NULL,
                hit_at     DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS api_keys (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
                key_hash    TEXT NOT NULL UNIQUE,
                key_prefix  TEXT NOT NULL,
                name        TEXT DEFAULT 'Default Key',
                is_active   INTEGER DEFAULT 1,
                calls_total INTEGER DEFAULT 0,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_used   DATETIME
            );

            CREATE INDEX IF NOT EXISTS idx_msgs_conv   ON chat_messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_convs_user  ON conversations(user_id);
            CREATE INDEX IF NOT EXISTS idx_rl_user     ON rate_limits(user_id, endpoint, hit_at);
            CREATE INDEX IF NOT EXISTS idx_apikeys_hash ON api_keys(key_hash);
        """)
        conn.commit()


# ── auth helpers ──────────────────────────────────────────────────────────────

def login_required_api(f):
    """Decorator: returns 401 JSON if no session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "login required", "code": "AUTH"}), 401
        return f(*args, **kwargs)
    return decorated


def touch_last_seen(user_id: int) -> None:
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET last_seen=? WHERE id=?",
                (datetime.now().isoformat(), user_id)
            )
            conn.commit()
    except Exception:
        pass


def send_otp_email(to_email: str, code: str) -> bool:
    smtp_host = os.getenv("SMTP_HOST")
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    if not smtp_host or not smtp_user:
        return False
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"]    = smtp_user
    msg["To"]      = to_email
    msg["Subject"] = "Vokk AI — your sign-in code"
    msg.set_content(
        f"Your Vokk sign-in code:\n\n  {code}\n\nExpires in 10 minutes.\n\n"
        f"— Nibra Cyber / Vokk AI"
    )
    try:
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        return True
    except Exception:
        return False


def soft_rate_limit(user_id: int, endpoint: str, max_per_minute: int = 30) -> bool:
    """Returns True if the user is within rate limits, False if exceeded."""
    window = (datetime.now() - timedelta(minutes=1)).isoformat()
    with get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM rate_limits WHERE user_id=? AND endpoint=? AND hit_at > ?",
            (user_id, endpoint, window)
        ).fetchone()[0]
        if count >= max_per_minute:
            return False
        conn.execute(
            "INSERT INTO rate_limits (user_id, endpoint) VALUES (?,?)",
            (user_id, endpoint)
        )
        # clean old records (keep DB tidy)
        conn.execute(
            "DELETE FROM rate_limits WHERE user_id=? AND endpoint=? AND hit_at < ?",
            (user_id, endpoint, (datetime.now() - timedelta(hours=1)).isoformat())
        )
        conn.commit()
    return True


# ── providers ─────────────────────────────────────────────────────────────────

PROVIDERS = {
    "gemini":     {"env": "GEMINI_API_KEY",     "default_model": "gemini-2.5-flash"},
    "groq":       {"env": "GROQ_API_KEY",       "default_model": "llama-3.3-70b-versatile"},
    "openrouter": {"env": "OPENROUTER_API_KEY", "default_model": "x-ai/grok-2-1212"},
}

# Uncensored OpenRouter model — used for NSFW / creative routing
OPENROUTER_UNCENSORED = "nousresearch/hermes-3-llama-3.1-405b"

# ── model rotation lists (tried in order when primary hits rate limits) ───────
# Groq: many free models available — rotate through all of them
GROQ_MODELS_ROTATION = [
    "llama-3.3-70b-versatile",
    "llama3-70b-8192",
    "llama-3.1-70b-versatile",
    "mixtral-8x7b-32768",
    "gemma2-9b-it",
    "llama-3.1-8b-instant",
    "llama3-8b-8192",
    "deepseek-r1-distill-llama-70b",
]

# Gemini: multiple flash variants
GEMINI_MODELS_ROTATION = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
    "gemini-1.0-pro",
]

# OpenRouter: diverse model pool including free tiers
OPENROUTER_MODELS_ROTATION = [
    "x-ai/grok-2-1212",
    "nousresearch/hermes-3-llama-3.1-405b",
    "qwen/qwen-2.5-72b-instruct",
    "mistralai/mistral-7b-instruct",
    "mistralai/mixtral-8x7b-instruct",
    "meta-llama/llama-3.3-70b-instruct",
    "meta-llama/llama-3.1-8b-instruct:free",
    "google/gemma-2-9b-it:free",
    "microsoft/phi-3-mini-128k-instruct:free",
]

OPENROUTER_UNCENSORED_ROTATION = [
    "nousresearch/hermes-3-llama-3.1-405b",
    "nousresearch/nous-hermes-2-mixtral-8x7b-dpo",
    "mistralai/mistral-7b-instruct",
    "meta-llama/llama-3.1-8b-instruct:free",
]

_ROTATION_MAP = {
    "groq":       GROQ_MODELS_ROTATION,
    "gemini":     GEMINI_MODELS_ROTATION,
    "openrouter": OPENROUTER_MODELS_ROTATION,
}

# ── developer API key helpers ─────────────────────────────────────────────────

def create_api_key(user_id: int, name: str = "Default Key") -> str:
    """Create a new Vokk API key. Returns the full key (shown only once)."""
    raw_key    = f"vk-{secrets.token_urlsafe(32)}"
    key_hash   = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:12] + "..."
    with get_db() as conn:
        conn.execute(
            "INSERT INTO api_keys (user_id, key_hash, key_prefix, name) VALUES (?,?,?,?)",
            (user_id, key_hash, key_prefix, name),
        )
        conn.commit()
    return raw_key


def verify_api_key(raw_key: str) -> int | None:
    """Returns user_id if the key is valid and active, else None."""
    if not raw_key or not raw_key.startswith("vk-"):
        return None
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    with get_db() as conn:
        row = conn.execute(
            "SELECT user_id FROM api_keys WHERE key_hash=? AND is_active=1",
            (key_hash,),
        ).fetchone()
    return row["user_id"] if row else None


def bump_api_key_usage(raw_key: str) -> None:
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    with get_db() as conn:
        conn.execute(
            "UPDATE api_keys SET calls_total=calls_total+1, last_used=? WHERE key_hash=?",
            (datetime.now().isoformat(), key_hash),
        )
        conn.commit()

REFUSAL_PATTERNS = (
    "i can't help with that",  "i cannot help with that",
    "i'm sorry, but i can",    "i am sorry, but i can",
    "i cannot generate",       "i can't generate",
    "i cannot provide",        "i can't provide",
    "i cannot create",         "i can't create",
    "i can't assist",          "i cannot assist",
    "i'm unable to",           "i am unable to",
    "as an ai",                "as a language model",
    "this content cannot",     "against my guidelines",
    "violates my",             "content policy",
    "i'm not able to",         "i am not able to",
    "i won't",                 "i will not",
    "not appropriate",         "not comfortable",
    "harmful content",         "harmful or offensive",
    "i'd rather not",          "i would rather not",
    "that's not something",    "that is not something",
    "safety reasons",          "ethical",
    "i have to decline",       "must decline",
    "goes against",            "designed to",
)


def looks_like_refusal(text: str) -> bool:
    if not text:
        return False
    head = text.lower().strip()[:500]
    return any(p in head for p in REFUSAL_PATTERNS)


def wrap_as_blocked(text: str = "") -> str:
    """Wrap content in BLOCKED markers for cinematic UI redaction."""
    return f"⟪BLOCKED⟫{(text or '').strip()}⟪/BLOCKED⟫"


def strip_thinking_tags(text: str) -> str:
    """Remove <think>...</think> blocks from deepseek-r1 responses."""
    return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()


# Sentences/fragments that constitute refusals AFTER a BLOCKED section.
# The model sometimes generates a bar correctly but then appends an explanation
# of why it won't help — this strips those fragments out.
_POST_BLOCK_REFUSAL_PHRASES = (
    "i'm not gonna help", "i won't help", "i can't help with that",
    "not something i can", "not able to help", "i don't provide",
    "i won't do", "i refuse to", "this isn't something",
    "won't help you", "can't assist", "not going to help",
    "happy to assist with something", "here for it. what else",
    "less likely to get you arrested",
)


def clean_post_block_refusals(text: str) -> str:
    """
    After every ⟪BLOCKED⟫...⟪/BLOCKED⟫ section, remove any refusal sentences
    that appear in the surrounding text. The bar is the refusal — nothing else.
    """
    if "⟪BLOCKED⟫" not in text:
        return text

    # Split into BLOCKED and plain-text parts
    parts = re.split(r"(⟪BLOCKED⟫[\s\S]*?⟪/BLOCKED⟫)", text)
    result = []
    after_block = False

    for part in parts:
        if part.startswith("⟪BLOCKED⟫"):
            result.append(part)
            after_block = True
        else:
            if after_block:
                # Strip sentences/clauses containing refusal language
                sentences = re.split(r"(?<=[.!?])\s+", part)
                filtered  = []
                for sent in sentences:
                    lo = sent.lower()
                    if any(p in lo for p in _POST_BLOCK_REFUSAL_PHRASES):
                        continue
                    # Also strip lines starting with refusal keywords
                    if any(lo.strip().startswith(p) for p in REFUSAL_PATTERNS):
                        continue
                    filtered.append(sent)
                part = " ".join(filtered)
            result.append(part)

    return "".join(result)


# ── Gemini API ────────────────────────────────────────────────────────────────

def call_gemini(
    model: str,
    system: str,
    messages: list,
    image_mode: bool = False,
    temperature: float = 0.95,
    max_tokens: int = 4096,
) -> dict:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return {"text": "[missing GEMINI_API_KEY]"}

    contents = [
        {
            "role": "user" if m["role"] == "user" else "model",
            "parts": [{"text": m["content"]}],
        }
        for m in messages
    ]
    body: dict = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system}]},
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "candidateCount": 1,
        },
        # All safety filters off — personality handles the rules
        "safetySettings": [
            {"category": cat, "threshold": "BLOCK_NONE"}
            for cat in [
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
            ]
        ],
    }
    if image_mode:
        body["generationConfig"]["responseModalities"] = ["TEXT", "IMAGE"]

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={key}"
    )
    try:
        resp = requests.post(url, json=body, timeout=90)
    except requests.RequestException as e:
        return {"text": f"[gemini network error: {e}]"}

    if resp.status_code != 200:
        return {"text": f"[gemini {resp.status_code}: {resp.text[:400]}]"}

    data = resp.json()
    out: dict = {"text": "", "image_b64": None, "mime": None}

    try:
        cand   = data["candidates"][0]
        finish = cand.get("finishReason", "")
        for part in cand.get("content", {}).get("parts", []):
            if "text" in part:
                out["text"] += part["text"]
            if "inlineData" in part:
                out["image_b64"] = part["inlineData"]["data"]
                out["mime"]      = part["inlineData"].get("mimeType", "image/png")
        out["text"] = out["text"].strip()

        if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST") or (
            not out["text"] and not out["image_b64"]
        ):
            out["text"] = wrap_as_blocked()

    except (KeyError, IndexError):
        if "promptFeedback" in data and data["promptFeedback"].get("blockReason"):
            out["text"] = wrap_as_blocked()
        else:
            out["text"] = f"[gemini parse error: {json.dumps(data)[:400]}]"

    return out


# ── OpenAI-compatible (Groq + OpenRouter) ────────────────────────────────────

def call_openai_compat(
    endpoint: str,
    key_env: str,
    model: str,
    system: str,
    messages: list,
    temperature: float = 0.95,
    max_tokens: int = 4096,
    extra_headers: dict | None = None,
) -> dict:
    key = os.getenv(key_env)
    if not key:
        return {"text": f"[missing {key_env}]"}

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    msgs = [{"role": "system", "content": system}] + messages

    try:
        resp = requests.post(
            endpoint,
            headers=headers,
            json={
                "model":       model,
                "messages":    msgs,
                "temperature": temperature,
                "max_tokens":  max_tokens,
            },
            timeout=90,
        )
    except requests.RequestException as e:
        return {"text": f"[network error: {e}]"}

    if resp.status_code != 200:
        return {"text": f"[error {resp.status_code}: {resp.text[:400]}]"}

    data = resp.json()
    try:
        text = data["choices"][0]["message"]["content"].strip()
        # Deepseek R1 sometimes wraps reasoning in <think> tags — strip them
        text = strip_thinking_tags(text)
        return {"text": text}
    except (KeyError, IndexError):
        return {"text": f"[parse error: {json.dumps(data)[:400]}]"}


def call_groq(model: str, system: str, messages: list) -> dict:
    return call_openai_compat(
        "https://api.groq.com/openai/v1/chat/completions",
        "GROQ_API_KEY", model, system, messages,
    )


def call_openrouter(model: str, system: str, messages: list) -> dict:
    return call_openai_compat(
        "https://openrouter.ai/api/v1/chat/completions",
        "OPENROUTER_API_KEY", model, system, messages,
        extra_headers={
            "HTTP-Referer": "https://vokkai.nibra.eco",
            "X-Title":      "Vokk AI by Nibra Cyber",
        },
    )


def call_provider(
    provider: str,
    model: str,
    system: str,
    messages: list,
    image_mode: bool = False,
) -> dict:
    if provider == "gemini":
        return call_gemini(model, system, messages, image_mode=image_mode)
    if provider == "groq":
        return call_groq(model, system, messages)
    if provider == "openrouter":
        return call_openrouter(model, system, messages)
    return {"text": f"[unknown provider: {provider}]"}


# ── tools ─────────────────────────────────────────────────────────────────────

def tool_web_search(query: str, num: int = 6) -> str:
    key = os.getenv("SERPAPI_KEY")
    if not key:
        return "[web search unavailable — no SERPAPI_KEY configured]"
    try:
        resp = requests.get(
            "https://serpapi.com/search.json",
            params={"q": query, "api_key": key, "num": num, "engine": "google"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return f"[search failed: {e}]"

    lines = [f"[TOOL RESULT — web search: {query!r}]"]

    # Answer box (quick facts)
    if "answer_box" in data:
        ab   = data["answer_box"]
        snip = ab.get("answer") or ab.get("snippet") or ab.get("title")
        if snip:
            lines.append(f"Quick answer: {snip}")

    # Knowledge graph
    if "knowledge_graph" in data:
        kg = data["knowledge_graph"]
        if kg.get("description"):
            lines.append(f"Knowledge: {kg['description']}")

    # Organic results
    for i, res in enumerate(data.get("organic_results", [])[:num], 1):
        title   = res.get("title", "")
        snippet = res.get("snippet", "")
        link    = res.get("link", "")
        lines.append(f"{i}. {title}\n   {snippet}\n   {link}")

    return "\n".join(lines)


def tool_scrape(url: str, prompt: str) -> str:
    key = os.getenv("SCRAPEGRAPH_API_KEY")
    if not key:
        return "[scrape unavailable — no SCRAPEGRAPH_API_KEY configured]"
    try:
        resp = requests.post(
            "https://api.scrapegraphai.com/v1/smartscraper",
            headers={"SGAI-APIKEY": key, "Content-Type": "application/json"},
            json={"website_url": url, "user_prompt": prompt},
            timeout=120,
        )
        if resp.status_code != 200:
            return f"[scrape error {resp.status_code}: {resp.text[:400]}]"
        data = resp.json()
        return f"[TOOL RESULT — scraped {url}]\n{json.dumps(data, indent=2)[:4000]}"
    except Exception as e:
        return f"[scrape failed: {e}]"


def tool_song_recognize(file_storage) -> str:
    token = os.getenv("AUDD_API_TOKEN")
    if not token:
        return "[song recognition unavailable — no AUDD_API_TOKEN configured]"
    try:
        resp = requests.post(
            "https://api.audd.io/",
            data={"api_token": token, "return": "apple_music,spotify,deezer"},
            files={"file": (
                file_storage.filename,
                file_storage.stream,
                file_storage.mimetype,
            )},
            timeout=60,
        )
        data = resp.json()
    except Exception as e:
        return f"[recognition failed: {e}]"

    if data.get("status") != "success" or not data.get("result"):
        return "[no match found — try a clearer or longer audio clip]"

    res   = data["result"]
    parts = [
        f"Title:    {res.get('title', '?')}",
        f"Artist:   {res.get('artist', '?')}",
        f"Album:    {res.get('album', '?')}",
        f"Released: {res.get('release_date', '?')}",
    ]
    if res.get("song_link"):
        parts.append(f"Link:     {res['song_link']}")

    # Spotify link if available
    if res.get("spotify") and isinstance(res["spotify"], dict):
        ext = res["spotify"].get("external_urls", {}).get("spotify")
        if ext:
            parts.append(f"Spotify:  {ext}")

    return "[TOOL RESULT — song identified]\n" + "\n".join(parts)


def tool_crypto(ids: str = "bitcoin", vs: str = "usd") -> str:
    key    = os.getenv("COINGECKO_API_KEY")
    params = {"ids": ids, "vs_currencies": vs, "include_24hr_change": "true"}
    if key:
        params["x_cg_demo_api_key"] = key
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return f"[crypto fetch failed: {e}]"

    lines = [f"[TOOL RESULT — live crypto prices ({vs.upper()}) at {datetime.now().strftime('%H:%M UTC')}]"]
    for coin, info in data.items():
        price  = info.get(vs.lower(), "?")
        change = info.get(f"{vs.lower()}_24h_change")
        change_str = f"  {change:+.2f}% (24h)" if change is not None else ""
        lines.append(f"{coin:12s}  {price:>12} {vs.upper()}{change_str}")
    return "\n".join(lines) if len(lines) > 1 else "[no data returned]"


def tool_image_generate(prompt: str) -> dict:
    out = call_gemini(
        "gemini-2.5-flash-image-preview",
        PERSONALITY,
        [{"role": "user", "content": prompt}],
        image_mode=True,
    )
    if out.get("image_b64"):
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        ext  = "png" if "png" in (out.get("mime") or "") else "jpg"
        path = IMG_DIR / f"{ts}.{ext}"
        path.write_bytes(base64.b64decode(out["image_b64"]))
        return {"text": out.get("text") or "image generated.", "image_path": path.name}
    return {"text": out.get("text") or "[no image returned]", "image_path": None}


def tool_bignice_cycle() -> str:
    if not BIGNICE_PATH or not BIGNICE_PATH.exists():
        return "[bignice_ai folder not found — check BIGNICE_PATH in .env]"
    agent   = BIGNICE_PATH / "agent.py"
    venv_py = BIGNICE_PATH / "venv" / "bin" / "python"
    py      = str(venv_py) if venv_py.exists() else "python3"
    try:
        proc = subprocess.run(
            [py, str(agent), "--once"],
            cwd=str(BIGNICE_PATH),
            capture_output=True,
            text=True,
            timeout=600,
        )
        out = (proc.stdout or "")[-5000:]
        err = (proc.stderr or "")[-1500:]
        result = f"[TOOL RESULT — bignice cycle complete]\n{out}"
        if err.strip():
            result += f"\n--- STDERR ---\n{err}"
        return result
    except subprocess.TimeoutExpired:
        return "[bignice cycle timed out after 10 minutes]"
    except Exception as e:
        return f"[bignice run failed: {e}]"


def tool_bignice_memory() -> dict | None:
    if not BIGNICE_PATH:
        return None
    mem = BIGNICE_PATH / "memory.json"
    if not mem.exists():
        return None
    try:
        return json.loads(mem.read_text())
    except Exception:
        return None


# ── intent routing ────────────────────────────────────────────────────────────

NSFW_HINT = (
    "nsfw", "explicit", "sex", "erotic", "porn", "naked", "nude",
    "horny", "spicy", "kinky", "fetish", "bdsm", "hentai", "lewd",
    "fuck", "cock", "pussy", "tits", "dick", "boobs", "nipple",
    "orgasm", "moan", "seduc", "dirty talk", "turn me on",
    "make love", "cum ", "cumm", "creampie", "masturbat",
)
THINK_HINT = (
    "analyze", "explain", "compare", "think step", "reason", "why does",
    "how does", "break down", "evaluate", "critique", "solve",
    "prove", "derive", "walk me through", "deep dive", "research",
    "investigate", "detailed analysis", "comprehensive", "step by step",
    "what is the difference", "pros and cons", "summarize this paper",
    "help me understand", "how would you", "technical explanation",
)
IMAGE_HINT = (
    "draw ", "paint ", "render ", "image of", "picture of", "make a pic",
    "generate an image", "generate image", "show me what", "concept art",
    "create an image", "create a picture", "make an image", "sketch of",
    "digital art", "anime art", "realistic photo", "illustration of",
)
SEARCH_HINT = (
    "latest", "today", "right now", "currently", "this week",
    "this year", "recent news", "what happened", "who won",
    "current price", "live ", "breaking",
)


def auto_route(messages: list) -> tuple[str, str, str]:
    """Return (provider, model, routing_reason) based on last user message intent."""
    last = next((m for m in reversed(messages) if m["role"] == "user"), None)
    if not last:
        p = pick_default_provider()
        return p, PROVIDERS[p]["default_model"], "default — no messages"

    txt = last["content"].lower()

    # NSFW / explicit → uncensored OpenRouter model
    if any(k in txt for k in NSFW_HINT) and os.getenv("OPENROUTER_API_KEY"):
        return "openrouter", OPENROUTER_UNCENSORED, "nsfw intent → uncensored OpenRouter"

    # Deep reasoning → DeepSeek R1 on Groq
    if any(k in txt for k in THINK_HINT) and len(txt) > 55 and os.getenv("GROQ_API_KEY"):
        return "groq", "deepseek-r1-distill-llama-70b", "reasoning intent → DeepSeek R1"

    # Image generation → Gemini image model
    if any(k in txt for k in IMAGE_HINT):
        return "gemini", "gemini-2.5-flash-image-preview", "image intent → Gemini image"

    # Live search hints → Gemini (fast, will use tool result already attached)
    if any(k in txt for k in SEARCH_HINT):
        p = pick_default_provider()
        return p, PROVIDERS[p]["default_model"], "search hint → default (tool attached)"

    p = pick_default_provider()
    return p, PROVIDERS[p]["default_model"], "default"


def pick_default_provider() -> str:
    pref = (os.getenv("DEFAULT_PROVIDER") or "").lower().strip()
    if pref in PROVIDERS and os.getenv(PROVIDERS[pref]["env"]):
        return pref
    for p in ("gemini", "groq", "openrouter"):
        if os.getenv(PROVIDERS[p]["env"]):
            return p
    return "gemini"


def is_error_text(text: str) -> bool:
    if not text:
        return True
    t = text.strip()
    return t.startswith("[") and any(
        w in t.lower()
        for w in ("error", "missing", "unavailable", "network", "503", "429", "500", "rate_limit", "exceeded")
    )


def build_fallback_chain(provider: str, model: str) -> list[tuple[str, str]]:
    """
    Build an exhaustive ordered list of (provider, model) to try.
    Tries all models in the primary provider first, then all models in
    all other providers — so we never fail due to a single rate limit.
    """
    chain: list[tuple[str, str]] = []
    seen:  set[tuple[str, str]] = set()

    def add(p: str, m: str) -> None:
        key = p, m
        if key in seen:
            return
        if not os.getenv(PROVIDERS.get(p, {}).get("env", "__NOPE__") or "__NOPE__"):
            return
        chain.append(key)
        seen.add(key)

    # Primary model first
    add(provider, model)

    # All other models in the same provider
    for m in _ROTATION_MAP.get(provider, []):
        add(provider, m)

    # Then every model in every other provider
    for p in ("gemini", "openrouter", "groq"):
        if p == provider:
            continue
        for m in _ROTATION_MAP.get(p, []):
            add(p, m)

    return chain


def call_with_fallback(
    provider: str,
    model: str,
    system: str,
    messages: list,
    image_mode: bool = False,
) -> tuple[dict, str, str]:
    """Try every available model across all providers until one succeeds."""
    chain    = build_fallback_chain(provider, model)
    last_err = ""

    for p, m in chain:
        out = call_provider(p, m, system, messages, image_mode=image_mode)
        text = out.get("text", "")
        if not is_error_text(text):
            return out, p, m
        last_err = text

    return {"text": last_err or "[all providers and models exhausted]"}, provider, model


# ── conversation DB helpers ────────────────────────────────────────────────────

def save_exchange(
    conv_id: int,
    user_msg: str,
    ai_msg: str,
    provider_used: str = "",
    elapsed_ms: int = 0,
) -> None:
    now = datetime.now().isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, provider_used, elapsed_ms) VALUES (?,?,?,?,?)",
            (conv_id, "user", user_msg, "", 0),
        )
        conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, provider_used, elapsed_ms) VALUES (?,?,?,?,?)",
            (conv_id, "assistant", ai_msg, provider_used, elapsed_ms),
        )
        conv = conn.execute(
            "SELECT title FROM conversations WHERE id=?", (conv_id,)
        ).fetchone()
        if conv and conv["title"] in ("New chat", ""):
            title = (user_msg[:65] + "…") if len(user_msg) > 65 else user_msg
            conn.execute(
                "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                (title, now, conv_id),
            )
        else:
            conn.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id)
            )
        conn.commit()


def get_conversation_messages(conv_id: int, limit: int = 200) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT role, content, provider_used, elapsed_ms, created_at
               FROM chat_messages
               WHERE conversation_id=?
               ORDER BY id
               LIMIT ?""",
            (conv_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def search_conversations(user_id: int, query: str, limit: int = 20) -> list[dict]:
    """Full-text search across user's conversation messages."""
    pattern = f"%{query}%"
    with get_db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT c.id, c.title, c.updated_at,
                      cm.content as match_snippet
               FROM conversations c
               JOIN chat_messages cm ON cm.conversation_id = c.id
               WHERE c.user_id = ?
                 AND cm.content LIKE ?
               ORDER BY c.updated_at DESC
               LIMIT ?""",
            (user_id, pattern, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def export_conversation(conv_id: int, user_id: int, fmt: str = "text") -> str | None:
    """Export a conversation to text or JSON format."""
    with get_db() as conn:
        conv = conn.execute(
            "SELECT * FROM conversations WHERE id=? AND user_id=?",
            (conv_id, user_id)
        ).fetchone()
        if not conv:
            return None
        msgs = conn.execute(
            "SELECT role, content, created_at FROM chat_messages WHERE conversation_id=? ORDER BY id",
            (conv_id,)
        ).fetchall()

    if fmt == "json":
        return json.dumps({
            "conversation": dict(conv),
            "messages": [dict(m) for m in msgs],
            "exported_at": datetime.now().isoformat(),
        }, indent=2)

    # Plain text format
    lines = [
        f"=== Vokk AI — Conversation Export ===",
        f"Title: {conv['title']}",
        f"Date:  {conv['created_at']}",
        f"Exported: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "=" * 50,
        "",
    ]
    for m in msgs:
        role   = "You" if m["role"] == "user" else "Vokk"
        ts     = m["created_at"][:16] if m["created_at"] else ""
        header = f"[{role}] {ts}"
        lines.extend([header, m["content"], ""])
    return "\n".join(lines)


# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask(
    __name__,
    template_folder=str(ROOT / "templates"),
    static_folder=str(ROOT / "static"),
)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)


# ── page routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/login")
def login_page():
    if session.get("user_id"):
        return redirect(url_for("index"))
    return render_template("login.html")


# ── auth API ──────────────────────────────────────────────────────────────────

@app.post("/api/auth/register")
def api_register():
    body     = request.get_json(force=True)
    email    = (body.get("email") or "").strip().lower()
    password = (body.get("password") or "").strip()
    name     = (body.get("display_name") or "").strip()

    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"error": "valid email required"}), 400
    if len(password) < 8:
        return jsonify({"error": "password must be at least 8 characters"}), 400

    with get_db() as conn:
        if conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            return jsonify({"error": "email already registered"}), 400
        pw_hash = generate_password_hash(password)
        conn.execute(
            "INSERT INTO users (email, password_hash, display_name) VALUES (?,?,?)",
            (email, pw_hash, name or email.split("@")[0]),
        )
        conn.commit()
        user = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        session.permanent = True
        session["user_id"]  = user["id"]
        session["email"]    = email

    return jsonify({"ok": True, "email": email})


@app.post("/api/auth/login")
def api_login():
    body     = request.get_json(force=True)
    email    = (body.get("email") or "").strip().lower()
    password = (body.get("password") or "").strip()

    if not email or not password:
        return jsonify({"error": "email and password required"}), 400

    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

    if not user or not user["password_hash"] or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "incorrect email or password"}), 401

    session.permanent  = True
    session["user_id"] = user["id"]
    session["email"]   = email
    touch_last_seen(user["id"])
    return jsonify({"ok": True, "email": email, "display_name": user["display_name"]})


@app.post("/api/auth/otp/request")
def api_otp_request():
    body  = request.get_json(force=True)
    email = (body.get("email") or "").strip().lower()

    if not email or "@" not in email:
        return jsonify({"error": "valid email required"}), 400

    code       = str(secrets.randbelow(1_000_000)).zfill(6)
    expires_at = (datetime.now() + timedelta(minutes=10)).isoformat()

    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (email) VALUES (?)", (email,))
        conn.execute("UPDATE otps SET used=1 WHERE email=?", (email,))
        conn.execute(
            "INSERT INTO otps (email, code, expires_at) VALUES (?,?,?)",
            (email, code, expires_at),
        )
        conn.commit()

    sent = send_otp_email(email, code)
    if sent:
        return jsonify({"ok": True, "sent": True})
    return jsonify({
        "ok": True, "sent": False, "dev_code": code,
        "note": "SMTP not configured — code shown for development",
    })


@app.post("/api/auth/otp/verify")
def api_otp_verify():
    body  = request.get_json(force=True)
    email = (body.get("email") or "").strip().lower()
    code  = (body.get("code") or "").strip()

    if not email or not code:
        return jsonify({"error": "email and code required"}), 400

    with get_db() as conn:
        otp = conn.execute(
            """SELECT * FROM otps
               WHERE email=? AND code=? AND used=0 AND expires_at > ?
               ORDER BY id DESC LIMIT 1""",
            (email, code, datetime.now().isoformat()),
        ).fetchone()
        if not otp:
            return jsonify({"error": "invalid or expired code"}), 401

        conn.execute("UPDATE otps SET used=1 WHERE id=?", (otp["id"],))
        user = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        conn.commit()
        session.permanent  = True
        session["user_id"] = user["id"]
        session["email"]   = email

    touch_last_seen(user["id"])
    return jsonify({"ok": True, "email": email})


@app.post("/api/auth/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/auth/me")
def api_me():
    uid = session.get("user_id")
    if uid:
        with get_db() as conn:
            user = conn.execute(
                "SELECT email, display_name FROM users WHERE id=?", (uid,)
            ).fetchone()
        if user:
            touch_last_seen(uid)
            return jsonify({
                "logged_in":    True,
                "user_id":      uid,
                "email":        user["email"],
                "display_name": user["display_name"],
            })
    return jsonify({"logged_in": False})


# ── conversation API ──────────────────────────────────────────────────────────

@app.get("/api/conversations")
def api_conversations():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"conversations": []})
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, title, provider, created_at, updated_at
               FROM conversations WHERE user_id=?
               ORDER BY updated_at DESC LIMIT 80""",
            (uid,),
        ).fetchall()
    return jsonify({"conversations": [dict(r) for r in rows]})


@app.post("/api/conversations")
def api_new_conversation():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "login required"}), 401
    provider = (request.get_json(force=True) or {}).get("provider", "auto")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (user_id, provider) VALUES (?,?)", (uid, provider)
        )
        conv_id = cur.lastrowid
        conn.commit()
    return jsonify({"id": conv_id})


@app.get("/api/conversations/<int:conv_id>")
def api_get_conversation(conv_id: int):
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "login required"}), 401
    with get_db() as conn:
        conv = conn.execute(
            "SELECT * FROM conversations WHERE id=? AND user_id=?", (conv_id, uid)
        ).fetchone()
        if not conv:
            return jsonify({"error": "not found"}), 404
        msgs = get_conversation_messages(conv_id)
    return jsonify({"conversation": dict(conv), "messages": msgs})


@app.delete("/api/conversations/<int:conv_id>")
def api_delete_conversation(conv_id: int):
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "login required"}), 401
    with get_db() as conn:
        conn.execute("DELETE FROM chat_messages WHERE conversation_id=?", (conv_id,))
        conn.execute(
            "DELETE FROM conversations WHERE id=? AND user_id=?", (conv_id, uid)
        )
        conn.commit()
    return jsonify({"ok": True})


@app.get("/api/conversations/<int:conv_id>/export")
def api_export_conversation(conv_id: int):
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "login required"}), 401
    fmt     = request.args.get("format", "text")
    content = export_conversation(conv_id, uid, fmt=fmt)
    if content is None:
        return jsonify({"error": "not found"}), 404
    mime     = "application/json" if fmt == "json" else "text/plain"
    filename = f"vokk-conversation-{conv_id}.{'json' if fmt == 'json' else 'txt'}"
    return Response(
        content,
        mimetype=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/conversations/search")
def api_search_conversations():
    uid   = session.get("user_id")
    if not uid:
        return jsonify({"results": []})
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"results": []})
    results = search_conversations(uid, query)
    return jsonify({"results": results})


@app.patch("/api/conversations/<int:conv_id>")
def api_rename_conversation(conv_id: int):
    uid   = session.get("user_id")
    if not uid:
        return jsonify({"error": "login required"}), 401
    title = (request.get_json(force=True) or {}).get("title", "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    with get_db() as conn:
        conn.execute(
            "UPDATE conversations SET title=? WHERE id=? AND user_id=?",
            (title, conv_id, uid),
        )
        conn.commit()
    return jsonify({"ok": True})


# ── chat ──────────────────────────────────────────────────────────────────────

@app.post("/api/chat")
def api_chat():
    body              = request.get_json(force=True)
    requested_provider = (body.get("provider") or "").strip().lower()
    messages           = body.get("messages", [])
    attach             = body.get("attach_tool_result")
    conv_id            = body.get("conversation_id")

    # Attach tool result to last user message
    if attach and messages:
        last = messages[-1]
        if last.get("role") == "user":
            last["content"] = f"{attach}\n\n---\n\n{last['content']}"

    # Provider / model selection
    mode_map = {
        "thinking": ("groq",       "deepseek-r1-distill-llama-70b", "thinking mode → DeepSeek R1"),
        "creative": ("openrouter", OPENROUTER_UNCENSORED,            "creative/spicy → uncensored OR"),
        "fast":     ("gemini",     "gemini-2.5-flash",               "fast mode → Gemini 2.5 Flash"),
    }
    if requested_provider in mode_map:
        provider, model, routing_reason = mode_map[requested_provider]
    elif requested_provider in ("auto", ""):
        provider, model, routing_reason = auto_route(messages)
    elif requested_provider in PROVIDERS:
        provider       = requested_provider
        model          = PROVIDERS[provider]["default_model"]
        routing_reason = f"user chose {provider}"
    else:
        provider, model, routing_reason = auto_route(messages)

    # ── auto-search for live/news queries ─────────────────────────────────────
    # If the message asks about current events and no tool result is already attached,
    # automatically run a web search and prepend the result.
    SEARCH_AUTO_HINT = (
        "latest", "today", "right now", "currently", "this week",
        "recent news", "breaking", "live ", "news about",
        "current price", "what's happening", "happened today",
        "update on", "trending",
    )
    if os.getenv("SERPAPI_KEY") and messages:
        last_user_content = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        should_auto_search = (
            any(k in last_user_content.lower() for k in SEARCH_AUTO_HINT)
            and "[TOOL RESULT" not in last_user_content
        )
        if should_auto_search:
            search_result = tool_web_search(last_user_content[:200], num=4)
            if search_result and not search_result.startswith("["):
                for m in reversed(messages):
                    if m["role"] == "user":
                        m["content"] = f"{search_result}\n\n---\n\n{m['content']}"
                        break

    # Rate limiting (soft) for logged-in users
    uid = session.get("user_id")
    if uid and not soft_rate_limit(uid, "chat", max_per_minute=40):
        return jsonify({"reply": "slow down a bit — you're sending messages really fast.", "fellback": False, "elapsed": 0})

    t0           = time.time()
    out, used_p, used_m = call_with_fallback(provider, model, PERSONALITY, messages)
    elapsed      = round(time.time() - t0, 2)
    elapsed_ms   = int(elapsed * 1000)

    reply = out.get("text", "")

    # Convert upstream refusal → cinematic BLOCKED bar (when no bar already present)
    if looks_like_refusal(reply) and "⟪BLOCKED⟫" not in reply:
        reply = wrap_as_blocked()
    else:
        # The model might have added a BLOCKED bar correctly but then appended
        # refusal text after it ("I won't help you with that..."). Strip it.
        reply = clean_post_block_refusals(reply)

    # Save to conversation history
    if uid and conv_id:
        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), None
        )
        if last_user:
            try:
                save_exchange(int(conv_id), last_user, reply, used_p, elapsed_ms)
            except Exception:
                pass

    response: dict = {
        "reply":    reply,
        "elapsed":  elapsed,
        "fellback": used_p != provider,
    }
    if os.getenv("NOVA_DEBUG") == "1":
        response["debug"] = {
            "asked":   f"{provider}/{model}",
            "used":    f"{used_p}/{used_m}",
            "routing": routing_reason,
        }
    return jsonify(response)


# ── tool routes ───────────────────────────────────────────────────────────────

@app.post("/api/tool/search")
def api_search():
    body = request.get_json(force=True)
    q    = (body.get("query") or "").strip()
    if not q:
        return jsonify({"result": "[empty query]"})
    num = int(body.get("num", 6))
    return jsonify({"result": tool_web_search(q, num=min(num, 10))})


@app.post("/api/tool/scrape")
def api_scrape():
    body = request.get_json(force=True)
    return jsonify({
        "result": tool_scrape(
            body.get("url", ""),
            body.get("prompt", "extract the main content as structured data"),
        )
    })


@app.post("/api/tool/song")
def api_song():
    f = request.files.get("file")
    if not f:
        return jsonify({"result": "[no audio file uploaded]"})
    return jsonify({"result": tool_song_recognize(f)})


@app.get("/api/tool/crypto")
def api_crypto():
    ids = request.args.get("ids", "bitcoin")
    vs  = request.args.get("vs",  "usd")
    return jsonify({"result": tool_crypto(ids, vs)})


@app.post("/api/tool/image")
@login_required_api
def api_image():
    body   = request.get_json(force=True)
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"result": "[empty prompt]", "image_url": None})

    uid = session.get("user_id")
    if uid and not soft_rate_limit(uid, "image_gen", max_per_minute=5):
        return jsonify({"result": "image generation rate limit — wait a moment.", "image_url": None}), 429

    out = tool_image_generate(prompt)
    return jsonify({
        "result":    out["text"],
        "image_url": f"/generated/{out['image_path']}" if out["image_path"] else None,
    })


@app.get("/generated/<name>")
def serve_image(name: str):
    # Sanitize: no path traversal
    safe = re.sub(r"[^a-zA-Z0-9._-]", "", name)
    path = IMG_DIR / safe
    if not path.exists():
        return "not found", 404
    return send_file(str(path))


# ── bignice routes ────────────────────────────────────────────────────────────

@app.post("/api/bignice/cycle")
def api_bignice_cycle():
    return jsonify({"result": tool_bignice_cycle()})


@app.get("/api/bignice/memory")
def api_bignice_memory():
    mem = tool_bignice_memory()
    if mem is None:
        return jsonify({"result": "[no bignice memory found — check BIGNICE_PATH]"})
    return jsonify({"memory": mem})


# ── status / health ───────────────────────────────────────────────────────────

@app.get("/api/status")
def api_status():
    return jsonify({
        "app":     "Vokk AI by Nibra Cyber",
        "version": "2.0.0",
        "providers_configured": {
            p: bool(os.getenv(c["env"])) for p, c in PROVIDERS.items()
        },
        "tools": {
            "search":  bool(os.getenv("SERPAPI_KEY")),
            "scrape":  bool(os.getenv("SCRAPEGRAPH_API_KEY")),
            "song":    bool(os.getenv("AUDD_API_TOKEN")),
            "crypto":  True,
            "image":   bool(os.getenv("GEMINI_API_KEY")),
            "bignice": bool(BIGNICE_PATH and BIGNICE_PATH.exists()),
        },
        "auth_enabled": True,
        "db_path": str(DB_PATH),
        "uptime_ts": datetime.now().isoformat(),
    })


@app.get("/api/health")
def api_health():
    return jsonify({"status": "ok", "ts": datetime.now().isoformat()})


# ── developer API ─────────────────────────────────────────────────────────────
#
# Vokk Developer API — OpenAI-compatible endpoint.
# Authenticate with: Authorization: Bearer vk-<your-key>
# Available model aliases:
#   vokk-auto     → auto-routed (default)
#   vokk-fast     → Gemini 2.5 Flash
#   vokk-think    → DeepSeek R1 (deep reasoning)
#   vokk-creative → uncensored OpenRouter model
#   vokk-image    → Gemini image generation

@app.get("/api/developer/keys")
@login_required_api
def api_dev_list_keys():
    uid = session["user_id"]
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, key_prefix, name, is_active, calls_total, created_at, last_used
               FROM api_keys WHERE user_id=? ORDER BY created_at DESC""",
            (uid,),
        ).fetchall()
    return jsonify({"keys": [dict(r) for r in rows]})


@app.post("/api/developer/keys")
@login_required_api
def api_dev_create_key():
    uid  = session["user_id"]
    name = (request.get_json(force=True) or {}).get("name", "Default Key").strip()[:60]
    # Limit to 5 keys per user
    with get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM api_keys WHERE user_id=? AND is_active=1", (uid,)
        ).fetchone()[0]
    if count >= 5:
        return jsonify({"error": "max 5 active API keys per account"}), 400
    raw_key = create_api_key(uid, name)
    return jsonify({"ok": True, "key": raw_key, "note": "store this key safely — shown only once"})


@app.delete("/api/developer/keys/<int:key_id>")
@login_required_api
def api_dev_revoke_key(key_id: int):
    uid = session["user_id"]
    with get_db() as conn:
        conn.execute(
            "UPDATE api_keys SET is_active=0 WHERE id=? AND user_id=?",
            (key_id, uid),
        )
        conn.commit()
    return jsonify({"ok": True})


@app.get("/v1/models")
def v1_models():
    """OpenAI-compatible models list endpoint."""
    model_list = [
        {"id": "vokk-auto",     "object": "model", "description": "auto-routed, best for general use"},
        {"id": "vokk-fast",     "object": "model", "description": "fast responses via Gemini"},
        {"id": "vokk-think",    "object": "model", "description": "deep reasoning via DeepSeek R1"},
        {"id": "vokk-creative", "object": "model", "description": "uncensored creative responses"},
        {"id": "vokk-image",    "object": "model", "description": "image generation via Gemini"},
    ]
    return jsonify({"object": "list", "data": model_list})


@app.post("/v1/chat/completions")
def v1_chat_completions():
    """
    Vokk Developer API — OpenAI-compatible chat completions.
    Accepts Authorization: Bearer vk-<your_api_key>
    """
    auth    = request.headers.get("Authorization", "")
    raw_key = auth[7:].strip() if auth.startswith("Bearer ") else ""

    if not raw_key:
        return jsonify({"error": {"message": "API key required. Set Authorization: Bearer vk-<key>",
                                  "type": "authentication_error"}}), 401

    dev_user_id = verify_api_key(raw_key)
    if not dev_user_id:
        return jsonify({"error": {"message": "Invalid or revoked API key",
                                  "type": "authentication_error"}}), 401

    body        = request.get_json(force=True) or {}
    model_req   = (body.get("model") or "vokk-auto").lower()
    msgs        = body.get("messages", [])
    temperature = float(body.get("temperature", 0.95))
    max_tokens  = int(body.get("max_tokens", 4096))

    # Map model aliases to provider/model
    model_map = {
        "vokk-auto":     lambda: auto_route(msgs)[:2],
        "vokk-fast":     lambda: ("gemini",     "gemini-2.5-flash"),
        "vokk-think":    lambda: ("groq",        "deepseek-r1-distill-llama-70b"),
        "vokk-creative": lambda: ("openrouter",  OPENROUTER_UNCENSORED),
        "vokk-image":    lambda: ("gemini",      "gemini-2.5-flash-image-preview"),
    }
    resolver = model_map.get(model_req, model_map["vokk-auto"])
    result = resolver()
    provider, model = result if len(result) == 2 else result[:2]

    t0           = time.time()
    out, used_p, used_m = call_with_fallback(provider, model, PERSONALITY, msgs)
    elapsed      = round(time.time() - t0, 2)
    reply        = out.get("text", "")

    if looks_like_refusal(reply) and "⟪BLOCKED⟫" not in reply:
        reply = wrap_as_blocked()

    bump_api_key_usage(raw_key)

    # Count tokens (approximate)
    prompt_tokens = sum(len((m.get("content") or "").split()) * 4 // 3 for m in msgs)
    comp_tokens   = len(reply.split()) * 4 // 3

    return jsonify({
        "id":      f"vokk-cmpl-{secrets.token_hex(8)}",
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   f"vokk/{used_m}",
        "choices": [{
            "index":         0,
            "message":       {"role": "assistant", "content": reply},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens":     prompt_tokens,
            "completion_tokens": comp_tokens,
            "total_tokens":      prompt_tokens + comp_tokens,
        },
        "system": "Vokk AI by Nibra Cyber — nibra.eco",
        "elapsed_seconds": elapsed,
    })


# ── error handlers ────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "endpoint not found"}), 404
    return render_template("index.html"), 404


@app.errorhandler(500)
def server_error(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "internal server error"}), 500
    return render_template("index.html"), 500


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print(f"\n  ╔═══════════════════════════════════╗")
    print(f"  ║   Vokk AI by Nibra Cyber          ║")
    print(f"  ║   → http://127.0.0.1:{PORT}/       ║")
    print(f"  ╚═══════════════════════════════════╝\n")
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
