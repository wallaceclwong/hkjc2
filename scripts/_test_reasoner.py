"""Test if DeepSeek key has reasoner access."""
import os, httpx
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path("/opt/hkjc2/.env"))
key = os.getenv("DEEPSEEK_API_KEY", "")
resp = httpx.post("https://api.deepseek.com/v1/chat/completions",
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    json={"model": "deepseek-reasoner", "messages": [{"role": "user", "content": "1+1=?"}], "max_tokens": 10},
    timeout=30)
print(f"Status: {resp.status_code}")
print(f"Response: {resp.text[:300]}")
