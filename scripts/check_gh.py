"""Ad-hoc GitHub reachability check (reads token from .env, never inlines it)."""
import os
import httpx
from dotenv import dotenv_values

env = dotenv_values(".env")
tok = env.get("CLAMS__GITHUB__TOKEN", "")
print("token present:", bool(tok), "len:", len(tok))
r = httpx.get("https://api.github.com/user",
              headers={"Authorization": f"Bearer {tok}"}, timeout=15)
print("user:", r.status_code, r.json().get("login") if r.status_code == 200 else r.text[:200])
for repo in ("claudio", "clams"):
    r2 = httpx.get(f"https://api.github.com/repos/massimorofi/{repo}/issues",
                   params={"state": "open", "labels": "status:Ready"},
                   headers={"Authorization": f"Bearer {tok}"}, timeout=15)
    n = len(r2.json()) if r2.status_code == 200 else r2.text[:120]
    print(f"ready issues {repo}:", r2.status_code, n)
