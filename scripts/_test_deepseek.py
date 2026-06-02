"""Test DeepSeek API key."""
import os, httpx
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path("/opt/hkjc2/.env"))
key = os.getenv("DEEPSEEK_API_KEY", "")
resp = httpx.post("https://api.deepseek.com/v1/chat/completions",
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 5},
    timeout=15)
print(f"Status: {resp.status_code}")
print(f"Response: {resp.text[:200]}")
