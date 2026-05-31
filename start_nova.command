#!/bin/bash
# Double-click this to chat with Nova.
cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
  echo "→ first run: setting up venv…"
  python3 -m venv venv
  ./venv/bin/pip install -q -r requirements.txt
fi

if [ ! -f ".env" ]; then
  echo "→ no .env found — copying from .env.example"
  cp .env.example .env
  echo
  echo "  Open .env and add at least one API key, then run me again."
  echo "  ($(pwd)/.env)"
  read -p "Press enter to close…"
  exit 0
fi

./venv/bin/python chat.py
