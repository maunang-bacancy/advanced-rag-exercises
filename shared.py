"""
Gemini helpers shared by Tasks 2 and 3 (Task 1 keeps its own copy so it stays
self-contained): client with timeout, retries, embeddings, and text generation
with a fallback model.
"""

import os
import re
import sys
import time
from pathlib import Path

import httpx
import numpy as np
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

ROOT = Path(__file__).resolve().parent
PDF_DIR = ROOT.parent / "drive-download-20261025T142645Z-1-001(1)"

load_dotenv(ROOT / ".env")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "gemini-flash-lite-latest")
FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL", "gemini-flash-latest")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "gemini-embedding-001")

# Reads GOOGLE_API_KEY from the environment. The timeout (ms) stops a stalled
# request from hanging forever - the SDK has no timeout by default.
client = genai.Client(http_options=types.HttpOptions(timeout=60_000))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)


def is_rate_limit(e: Exception) -> bool:
    return isinstance(e, errors.APIError) and e.code == 429


def is_temporary(e: Exception) -> bool:
    """Errors worth retrying: rate limit (429), overload/timeout (503, 504), network."""
    if isinstance(e, errors.APIError):
        return e.code in (429, 503, 504)
    return isinstance(e, httpx.TransportError)


def with_retry(call, attempts=6, overload_attempts=None):
    """Run a Gemini call, retrying on temporary errors.

    Rate limits (429): wait as long as the server asks ("retry in 45s"), then
    try the same model again - switching models would not help.
    Overload / network: back off; after `overload_attempts` such failures,
    give up so the caller can switch to a fallback model.
    """
    overloads = 0
    for attempt in range(attempts):
        try:
            return call()
        except Exception as e:
            if not is_temporary(e) or attempt == attempts - 1:
                raise
            if is_rate_limit(e):
                m = re.search(r"retry in ([\d.]+)s", str(e))
                wait = float(m.group(1)) + 2 if m else 30
                reason = "rate limited"
            else:
                overloads += 1
                if overload_attempts and overloads >= overload_attempts:
                    raise
                wait = 15 * overloads
                reason = (f"model busy ({e.code})" if isinstance(e, errors.APIError)
                          else f"network error ({type(e).__name__})")
            print(f"  {reason}, waiting {wait:.0f}s ...")
            time.sleep(wait)


def embed(texts: list[str], task: str) -> np.ndarray:
    """Unit-length Gemini embeddings, in batches of 20.

    task = RETRIEVAL_DOCUMENT for chunks, RETRIEVAL_QUERY for questions.
    """
    vectors, batch = [], 20
    for start in range(0, len(texts), batch):
        part = texts[start:start + batch]
        res = with_retry(lambda: client.models.embed_content(
            model=EMBED_MODEL,
            contents=part,
            config=types.EmbedContentConfig(task_type=task),
        ))
        vectors.extend(e.values for e in res.embeddings)
        if len(texts) > batch:
            print(f"  embedded {min(start + batch, len(texts))}/{len(texts)}")
    v = np.array(vectors, dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def generate(contents, json_output=False) -> tuple[str, str]:
    """Generate with CHAT_MODEL, falling back to FALLBACK_MODEL if it is
    unavailable. Returns (text, model_used). `contents` may include images."""
    config = types.GenerateContentConfig(
        temperature=0,
        response_mime_type="application/json" if json_output else None,
    )

    def call(model, overload_attempts=None):
        return with_retry(lambda: client.models.generate_content(
            model=model, contents=contents, config=config),
            attempts=8, overload_attempts=overload_attempts)

    try:
        return (call(CHAT_MODEL, overload_attempts=2).text or "").strip(), CHAT_MODEL
    except Exception as e:
        if not is_temporary(e) or is_rate_limit(e):
            raise
        print(f"  {CHAT_MODEL} unavailable, falling back to {FALLBACK_MODEL}")
        return (call(FALLBACK_MODEL).text or "").strip(), FALLBACK_MODEL
