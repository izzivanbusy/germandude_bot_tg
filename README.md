# @germandude_bot — Deployment Guide

## Railway Setup

### 1. Environment Variables
Set these in Railway → your service → Variables:
```
TELEGRAM_TOKEN=dein_token_hier
OPENAI_API_KEY=dein_key_hier
```

### 2. Persistent Volume (wichtig!)
Damit `users.json` Redeploys überlebt:
- Railway → dein Projekt → Add Volume
- Mount Path: `/data`
- Fertig. Der Bot schreibt automatisch nach `/data/users.json`.

### 3. Deploy
Railway erkennt Python automatisch und installiert `requirements.txt`.
Startbefehl: `python bot.py`

## Local Development
```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN=...
export OPENAI_API_KEY=...
export USER_FILE=./users.json   # lokaler Pfad statt /data
python bot.py
```
