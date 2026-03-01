# Guardian Breaking Bot Prototype

This repository now includes a Python prototype that watches Guardian RSS feeds,
filters for high-impact stories, generates dramatic social captions, and can
optionally publish through Instagram Graph API.

## Files

- `guardian_breaking_bot.py` — autonomous polling loop.
- `requirements.txt` — Python dependencies.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python guardian_breaking_bot.py --poll-interval 45
```

Default behavior is **dry-run** (it will log caption previews but not publish).

## Enable AI ranking

```bash
export OPENAI_API_KEY="your_key"
python guardian_breaking_bot.py --use-ai-scoring --ai-score-threshold 8
```

## Enable Instagram publishing

```bash
export INSTAGRAM_ACCESS_TOKEN="..."
export INSTAGRAM_IG_USER_ID="..."
export INSTAGRAM_IMAGE_URL="https://your-cdn.com/breaking-template.jpg"
python guardian_breaking_bot.py --enable-posting --live
```

> Keep `--live` off while testing. Without `--live`, the bot remains in dry-run mode.

## Core pipeline

1. Poll Guardian RSS every N seconds.
2. Skip stories already seen (SQLite state).
3. Keyword-score headlines/summaries.
4. Optional AI importance rank (1–10).
5. Generate BREAKING-style caption.
6. Optionally publish with Meta Graph API.

## Notes for production

- Use a process supervisor (systemd / pm2 / Docker restart policy).
- Add feed/backoff metrics and alerting.
- Add a delay/recheck pass for evolving incidents to reduce false early numbers.
