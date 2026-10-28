"""
Task 1 - Baseline RAG over the GPO 2018 Annual Report.

Pipeline
  index:  PDF -> text in reading order -> sentence chunks (with overlap)
          -> Gemini embeddings -> saved to index/ (chunks.json + vectors.npy)
  ask:    question -> embedding -> top-k chunks by cosine similarity
          -> Gemini answers ONLY from those chunks, citing [n], or refuses

Usage
  python rag.py index
  python rag.py ask "How many depository libraries did GPO staff visit in FY 2018?"
  python rag.py eval            # runs the sample questions, writes results.md
"""

import json
import re
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import pymupdf
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PDF = ROOT.parent / "drive-download-20261025T142645Z-1-001(1)" / "GPO-Anual-report.pdf"
INDEX = HERE / "index"

load_dotenv(ROOT / ".env")
import os  # noqa: E402  (after load_dotenv so the env is populated)

CHAT_MODEL = os.environ.get("CHAT_MODEL", "gemini-flash-lite-latest")
FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL", "gemini-flash-latest")  # used when CHAT_MODEL is overloaded
EMBED_MODEL = os.environ.get("EMBED_MODEL", "gemini-embedding-001")

CHUNK_CHARS = 900     # target chunk size
OVERLAP_SENTENCES = 2  # sentences repeated at the start of the next chunk
TOP_K = 5
REFUSAL = "I could not find this in the document."

# Reads GOOGLE_API_KEY from the environment. The timeout (ms) stops a stalled
# request from hanging forever - the SDK has no timeout by default.
client = genai.Client(http_options=types.HttpOptions(timeout=60_000))


# ---------------------------------------------------------------- 1. extract

def page_text(page) -> str:
    """Text of one page in human reading order.

    The report uses two columns. PyMuPDF's default order can interleave them,
    so we sort text blocks ourselves: full-width blocks act as horizontal
    "bands"; between two bands we read the left column, then the right.
    """
    mid = page.rect.width / 2
    blocks = [b for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
    blocks.sort(key=lambda b: (b[1], b[0]))

    out, left, right = [], [], []

    def flush():
        out.extend(b[4] for b in sorted(left, key=lambda b: b[1]))
        out.extend(b[4] for b in sorted(right, key=lambda b: b[1]))
        left.clear()
        right.clear()

    for b in blocks:
        x0, x1 = b[0], b[2]
        if x1 <= mid + 10:
            left.append(b)
        elif x0 >= mid - 10:
            right.append(b)
        else:  # spans both columns -> a band
            flush()
            out.append(b[4])
    flush()
    return "\n".join(out)


def clean(text: str) -> str:
    text = re.sub(r"(\w)-\n([a-z])", r"\1\2", text)  # re-join words split by line wrap
    text = text.replace("­", "")             # soft hyphens
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- 2. chunk

SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(])")


def chunk_document(pdf_path: Path) -> list[dict]:
    """Whole sentences packed into ~CHUNK_CHARS chunks, with overlap.

    Each sentence remembers its page, so every chunk can cite pages.
    """
    sentences = []  # (page_number, sentence)
    with pymupdf.open(pdf_path) as doc:
        for i, page in enumerate(doc, start=1):
            for s in SENT_SPLIT.split(clean(page_text(page))):
                if len(s) > 2:
                    sentences.append((i, s))

    chunks, cur = [], []
    for sent in sentences:
        if cur and sum(len(s) for _, s in cur) + len(sent[1]) > CHUNK_CHARS:
            chunks.append(cur)
            cur = cur[-OVERLAP_SENTENCES:]
        cur.append(sent)
    if cur:
        chunks.append(cur)

    return [
        {
            "id": n,
            "pages": sorted({p for p, _ in c}),
            "text": " ".join(s for _, s in c),
        }
        for n, c in enumerate(chunks)
    ]


# ---------------------------------------------------------------- 3. embed

def is_temporary(e: Exception) -> bool:
    """Errors worth retrying: rate limit (429), overload (503), network/timeout."""
    if isinstance(e, errors.APIError):
        return e.code in (429, 503)
    return isinstance(e, httpx.TransportError)


def with_retry(call, attempts=6):
    """Run a Gemini call, waiting and retrying on temporary errors -
    all common on the free tier."""
    for attempt in range(attempts):
        try:
            return call()
        except Exception as e:
            if not is_temporary(e) or attempt == attempts - 1:
                raise
            wait = 15 * (attempt + 1)
            if isinstance(e, errors.APIError):
                reason = "rate limited" if e.code == 429 else "model busy"
            else:
                reason = f"network error ({type(e).__name__})"
            print(f"  {reason}, waiting {wait}s ...")
            time.sleep(wait)


def embed(texts: list[str], task: str) -> np.ndarray:
    """Embed texts with Gemini, in small batches.

    task = RETRIEVAL_DOCUMENT for chunks, RETRIEVAL_QUERY for questions;
    the model is trained to make these two kinds line up.
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
    return v / np.linalg.norm(v, axis=1, keepdims=True)  # unit length -> dot = cosine


def build_index():
    print(f"Reading {PDF.name} ...")
    chunks = chunk_document(PDF)
    sizes = [len(c["text"]) for c in chunks]
    print(f"  {len(chunks)} chunks, avg {sum(sizes) // len(sizes)} chars")
    print(f"Embedding with {EMBED_MODEL} ...")
    vecs = embed([c["text"] for c in chunks], "RETRIEVAL_DOCUMENT")
    INDEX.mkdir(exist_ok=True)
    (INDEX / "chunks.json").write_text(json.dumps(chunks, indent=1), encoding="utf-8")
    np.save(INDEX / "vectors.npy", vecs)
    print(f"Saved index to {INDEX}")


# ---------------------------------------------------------------- 4. retrieve

def load_index():
    if not (INDEX / "vectors.npy").exists():
        sys.exit("No index yet - run: python rag.py index")
    chunks = json.loads((INDEX / "chunks.json").read_text(encoding="utf-8"))
    return chunks, np.load(INDEX / "vectors.npy")


def retrieve(question: str, chunks, vecs, k=TOP_K):
    q = embed([question], "RETRIEVAL_QUERY")[0]
    scores = vecs @ q
    best = np.argsort(-scores)[:k]
    return [(chunks[i], float(scores[i])) for i in best]


# ---------------------------------------------------------------- 5. generate

PROMPT = """You answer questions about the U.S. Government Publishing Office (GPO) \
2018 Annual Report, using ONLY the numbered passages below.

Rules:
- Use only facts stated in the passages. Do not add outside knowledge.
- Quote numbers exactly as written in the passages.
- Cite the passages you used like [1] or [2][3].
- If the passages do not contain the answer, reply exactly: "{refusal}"

Passages:
{passages}

Question: {question}
Answer:"""


def answer(question: str, chunks, vecs) -> dict:
    hits = retrieve(question, chunks, vecs)
    passages = "\n\n".join(
        f"[{n}] (page {', '.join(map(str, c['pages']))}) {c['text']}"
        for n, (c, _) in enumerate(hits, start=1)
    )
    prompt = PROMPT.format(refusal=REFUSAL, passages=passages, question=question)

    def generate(model, attempts):
        return with_retry(lambda: client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0),
        ), attempts)

    model = CHAT_MODEL
    try:
        res = generate(CHAT_MODEL, attempts=2)
    except Exception as e:
        if not is_temporary(e):
            raise
        print(f"  {CHAT_MODEL} unavailable, falling back to {FALLBACK_MODEL}")
        model = FALLBACK_MODEL
        res = generate(FALLBACK_MODEL, attempts=6)
    return {"question": question, "answer": (res.text or "").strip(),
            "hits": hits, "model": model}


def print_result(r: dict):
    print(f"\nQ: {r['question']}\n\nA ({r['model']}): {r['answer']}\n\nRetrieved:")
    for n, (c, score) in enumerate(r["hits"], start=1):
        print(f"  [{n}] score={score:.3f} page {c['pages']}: {c['text'][:110]}...")


# ---------------------------------------------------------------- 6. evaluate

# The 3 sample questions from the assignment come first. The extras check
# known traps: near-duplicate numbers (libraries visited vs. total, procured
# vs. revenue), a date that is easy to confuse (govinfo beta end vs. full
# deployment), and a question the report cannot answer (it covers FY 2018
# only), where the system must refuse instead of guessing.
EVAL_QUESTIONS = [
    "How many depository libraries did GPO staff visit in FY 2018?",
    "What new digital system replaced FDsys, and when was it fully deployed?",
    "How much did GPO procure in printing and information products in FY 2018 "
    "on behalf of federal agencies?",
    "How many copies of how many titles did GPO distribute?",
    "When was the govinfo website moved out of beta?",
    "How many passports did GPO produce in FY 2018?",
    "How many depository libraries did GPO staff visit in FY 2020?",
]


def run_eval():
    chunks, vecs = load_index()
    lines = [
        "# Task 1 - GPO Annual Report: Baseline RAG results",
        "",
        f"- Chat model: `{CHAT_MODEL}`  ",
        f"- Embedding model: `{EMBED_MODEL}`  ",
        f"- Chunks: {len(chunks)} (sentence-based, ~{CHUNK_CHARS} chars, "
        f"{OVERLAP_SENTENCES}-sentence overlap), top-k = {TOP_K}",
        "",
    ]
    for q in EVAL_QUESTIONS:
        r = answer(q, chunks, vecs)
        print_result(r)
        lines += [f"## {q}", "", r["answer"], "", f"*Answered by `{r['model']}`*", "",
                  "<details><summary>Retrieved passages</summary>", ""]
        for n, (c, score) in enumerate(r["hits"], start=1):
            lines.append(f"{n}. score {score:.3f}, page {', '.join(map(str, c['pages']))}: "
                         f"{c['text'][:200]}...")
        lines += ["", "</details>", ""]
    (HERE / "results.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {HERE / 'results.md'}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "index":
        build_index()
    elif cmd == "ask" and len(sys.argv) > 2:
        chunks, vecs = load_index()
        print_result(answer(" ".join(sys.argv[2:]), chunks, vecs))
    elif cmd == "eval":
        run_eval()
    else:
        print(__doc__)
