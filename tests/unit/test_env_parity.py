"""CI check: .env.example must equal the code defaults exactly."""
from __future__ import annotations

from pathlib import Path

from clams.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def _parse_env_example() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def test_env_example_parity():
    s = Settings(_env_file=None)
    env = _parse_env_example()
    expected = {f"CLAMS_{f.upper()}": str(getattr(s, f))
                for f in s.model_fields}
    assert set(env) == set(expected), (
        f"missing={set(expected) - set(env)} extra={set(env) - set(expected)}")
    for k, v in expected.items():
        assert env[k] == v, f"{k}: example={env[k]!r} default={v!r}"
