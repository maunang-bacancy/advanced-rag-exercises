"""
Task 3 - Multimodal / complex document RAG over the Apple iMac Product
Environmental Report (12 pages; facts in charts, call-outs, tables, footnotes).

Three extraction strategies are compared on the same questions:
  plain       page text only, cut into chunks (what basic RAG does)
  structured  + tables rebuilt cell by cell, footnote markers linked to their
              endnotes, endnotes as their own chunks
  vision      structured + a Gemini description of every visual page (charts,
              call-outs, icons); numbers it reports are checked against the
              PDF's text layer and unconfirmed ones are flagged

Usage
  python rag.py parse                 # extraction only, prints what was found (no API calls)
  python rag.py index                 # vision captions (cached) + embeddings for all strategies
  python rag.py ask "question" [plain|structured|vision]
  python rag.py eval                  # all questions x 3 strategies, writes results.md
"""

import json
import re
import sys
from pathlib import Path

import numpy as np
import pymupdf
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared import CHAT_MODEL, EMBED_MODEL, PDF_DIR, embed, generate  # noqa: E402

HERE = Path(__file__).resolve().parent
PDF = PDF_DIR / "iMac_PER_Oct2024.pdf"
INDEX = HERE / "index"
CACHE = HERE / "cache"
ENDNOTE_PAGES = (11, 12)
CHUNK_CHARS = 900
TOP_K = 6
STRATEGIES = ("plain", "structured", "vision")
REFUSAL = "I could not find this in the document."
HEADER = re.compile(r"^iMac\s+\|\s+Product Environmental Report(\s+\d+\b)?")  # running header + page no.


# ---------------------------------------------------------------- shared bits

def pack(pieces, limit=CHUNK_CHARS):
    """Pack text pieces (blocks or lines) into chunks of ~limit chars."""
    chunks, cur = [], ""
    for p in pieces:
        if cur and len(cur) + len(p) > limit:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def is_marker(span, prev) -> bool:
    """A footnote marker: a superscript number right after other text.
    (The '2' in CO2 is a subscript, not superscript, so it is not matched.)"""
    return bool(span["flags"] & 1) and span["text"].strip().isdigit() and prev is not None


# ---------------------------------------------------------------- 1. plain

def extract_plain(doc):
    chunks = []
    for pno, page in enumerate(doc, start=1):
        lines = [ln for ln in page.get_text().splitlines() if ln.strip()]
        for text in pack(lines):
            chunks.append({"kind": "text", "page": pno, "text": text})
    return chunks


# ---------------------------------------------------------------- 2. structured

def parse_endnotes(doc) -> dict[int, str]:
    """Endnotes look like '<tab> 18 <tab> Testing conducted by Apple ...'."""
    text = "\n".join(doc[p - 1].get_text() for p in ENDNOTE_PAGES)
    parts = re.split(r"(?m)^\s*(\d{1,2})\t", text)
    notes = {}
    for num, body in zip(parts[1::2], parts[2::2]):
        body = re.sub(r"\s+", " ", body.split("© 20")[0]).strip()
        notes[int(num)] = body
    return notes


def table_chunks(page, pno):
    """Tables: PyMuPDF finds the cell grid, but its own cell text corrupts
    decimals ('0.27W' -> '027W'), so each cell's words are re-read from the
    page. Output: a grid plus one sentence per row, e.g.
    'Power consumption for iMac - Sleep: 100V = 0.56W; 115V = 0.57W; ...'
    so a single value is still findable next to its row and column names."""
    out, boxes = [], []
    for t in page.find_tables().tables:
        grid = []
        for row in t.rows:
            cells = []
            for c in row.cells:
                words = page.get_text("words", clip=pymupdf.Rect(c)) if c else []
                words.sort(key=lambda w: (round(w[1]), w[0]))
                cells.append(" ".join(w[4] for w in words))
            grid.append(cells)
        title = ""
        if sum(bool(c) for c in grid[0]) == 1:          # merged title row
            title = next(c for c in grid[0] if c)
            grid = grid[1:]
        keep = [j for j in range(len(grid[0])) if any(r[j] for r in grid)]   # drop empty columns
        grid = [[r[j] for j in keep] for r in grid]
        header, rows = grid[0], grid[1:]
        name = title or header[0]
        facts = [f"{name} - {r[0]}: " + "; ".join(f"{h} = {v}" for h, v in zip(header[1:], r[1:]) if v)
                 for r in rows if r[0]]
        table = "\n".join(" | ".join(r) for r in grid)
        out.append({"kind": "table", "page": pno,
                    "text": f"Table: {name}\n{table}\n\n" + "\n".join(facts)})
        boxes.append(pymupdf.Rect(t.bbox))
    return out, boxes


def extract_structured(doc):
    notes = parse_endnotes(doc)
    chunks = [{"kind": "footnote", "page": 11 if n < 17 else 12, "text": f"Endnote {n}: {t}"}
              for n, t in notes.items()]

    for pno, page in enumerate(doc, start=1):
        tables, table_boxes = table_chunks(page, pno)
        chunks += tables
        if pno in ENDNOTE_PAGES:      # their text is already in the endnote chunks
            continue

        blocks = []                                    # (text, footnote numbers)
        for b in page.get_text("dict")["blocks"]:
            if not b.get("lines") or any(pymupdf.Rect(b["bbox"]).intersects(r) for r in table_boxes):
                continue                               # table text is already a table chunk
            parts, refs, prev = [], [], None
            for line in b["lines"]:
                for s in line["spans"]:
                    if is_marker(s, prev):
                        n = int(s["text"])
                        refs.append(n)
                        parts.append(f"[^{n}]")
                    else:
                        parts.append(s["text"])
                    prev = s
                parts.append(" ")
            text = re.sub(r"\s+", " ", "".join(parts)).strip()
            text = HEADER.sub("", text).strip()
            if text and not text.isdigit():
                blocks.append((text, refs))

        # pack blocks into chunks, then attach the endnotes each chunk cites,
        # so a claim and its caveat are retrieved (and read) together
        cur, cur_refs = [], []
        for text, refs in blocks + [(None, [])]:
            if text is None or (cur and sum(len(t) for t in cur) + len(text) > CHUNK_CHARS):
                if cur:
                    body = " ".join(cur)
                    cited = [f"[^{n}] {notes[n]}" for n in dict.fromkeys(cur_refs) if n in notes]
                    if cited:
                        body += "\nFootnotes:\n" + "\n".join(cited)
                    chunks.append({"kind": "text", "page": pno, "text": body})
                cur, cur_refs = [], []
            if text:
                cur.append(text)
                cur_refs += refs
    return chunks


# ---------------------------------------------------------------- 3. vision

VISION_PROMPT = """This is page {page} of Apple's iMac Product Environmental Report.

Describe the information on this page that is shown VISUALLY - charts, graphs, large
call-out statistics, icons with labels, and diagrams - as plain factual sentences.

- For a bar or waterfall chart: name the chart, then give every bar's label and value,
  and any totals, reductions or baselines it shows, and how they relate.
- For a call-out statistic: give the number and exactly what it refers to.
- Copy numbers exactly as printed. Do not compute new numbers.
- Skip ordinary body paragraphs; they are extracted separately.
- If the page has no visual information, reply exactly: NONE"""


def visual_pages(doc):
    """Pages with images or many vector drawings (charts, icons)."""
    return [pno for pno, page in enumerate(doc, start=1)
            if pno not in ENDNOTE_PAGES and (page.get_images() or len(page.get_drawings()) > 25)]


def numbers(text):
    return set(re.findall(r"\d+(?:[.,]\d+)?", text))


def describe_page(doc, pno):
    """Gemini reads the rendered page. Cached, so re-indexing is free."""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"vision_p{pno}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    png = doc[pno - 1].get_pixmap(dpi=150).tobytes("png")
    text, model = generate([types.Part.from_bytes(data=png, mime_type="image/png"),
                            VISION_PROMPT.format(page=pno)])
    result = {"page": pno, "model": model, "text": text}
    path.write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


def extract_vision(doc):
    """Vision descriptions, with every number checked against the PDF text.

    Vision models can misread or invent numbers, so the chunk says which
    numbers were confirmed by the page's own text and which were not."""
    chunks = []
    for pno in visual_pages(doc):
        d = describe_page(doc, pno)
        # Most pages repeat the same decorative lifecycle diagram; those
        # descriptions only add noise. Keep pages whose visuals carry data.
        if len(numbers(d["text"]) - {str(pno)}) < 2:
            continue
        page_nums = numbers(doc[pno - 1].get_text())
        unconfirmed = sorted(numbers(d["text"]) - page_nums - {str(pno)})
        note = ("\n(Machine-read from the page image. Numbers not found in the page text: "
                + ", ".join(unconfirmed) + ")") if unconfirmed else \
               "\n(Machine-read from the page image; all numbers confirmed in the page text.)"
        chunks.append({"kind": "vision", "page": pno, "text": d["text"] + note,
                       "unconfirmed": unconfirmed})
    return chunks


# ---------------------------------------------------------------- 4. index

def extract_all(with_vision):
    with pymupdf.open(PDF) as doc:
        plain = extract_plain(doc)
        structured = extract_structured(doc)
        vision = extract_vision(doc) if with_vision else []
    return {"plain": plain, "structured": structured + vision}


def cmd_parse():
    with pymupdf.open(PDF) as doc:
        plain, structured = extract_plain(doc), extract_structured(doc)
        print(f"plain: {len(plain)} chunks | structured: {len(structured)} chunks "
              f"| visual pages for vision: {visual_pages(doc)}")
    for c in structured:
        if c["kind"] == "table" or "ENERGY STAR" in c["text"] or c["text"].startswith("Endnote 18"):
            print(f"\n--- {c['kind']} p{c['page']}\n{c['text'][:900]}")


def build_index():
    sets = extract_all(with_vision=True)
    INDEX.mkdir(exist_ok=True)
    for name, chunks in sets.items():
        print(f"Embedding {len(chunks)} {name} chunks with {EMBED_MODEL} ...")
        vecs = embed([f"[{c['kind']} | page {c['page']}] {c['text']}" for c in chunks],
                     "RETRIEVAL_DOCUMENT")
        (INDEX / f"{name}.json").write_text(json.dumps(chunks, indent=1), encoding="utf-8")
        np.save(INDEX / f"{name}.npy", vecs)
    print(f"Saved index to {INDEX}")


def load_index():
    if not (INDEX / "structured.npy").exists():
        sys.exit("No index yet - run: python rag.py index")
    out = {}
    for name in ("plain", "structured"):
        out[name] = (json.loads((INDEX / f"{name}.json").read_text(encoding="utf-8")),
                     np.load(INDEX / f"{name}.npy"))
    return out


# ---------------------------------------------------------------- 5. answer

PROMPT = """You answer questions about Apple's iMac Product Environmental Report \
(October 2024), using ONLY the numbered passages below. Passages can be body text, \
tables, endnotes, or descriptions of charts read from the page image.

Rules:
- Use only facts stated in the passages. Do not add outside knowledge.
- Quote numbers and units exactly as written. Do not calculate new numbers.
- Prefer text and table passages over machine-read chart descriptions when they disagree.
- If a footnote limits or explains a claim, mention it briefly.
- Cite the passages you used like [1] or [2][3].
- If the passages do not contain the answer, reply exactly: "{refusal}"

Passages:
{passages}

Question: {question}
Answer:"""


def answer(question, index, strategy="vision"):
    """strategy: plain | structured (no vision chunks) | vision (everything)."""
    chunks, vecs = index["plain" if strategy == "plain" else "structured"]
    q = embed([question], "RETRIEVAL_QUERY")[0]
    scores = vecs @ q
    if strategy == "structured":
        scores = np.where([c["kind"] != "vision" for c in chunks], scores, -1)
    hits = [(chunks[i], float(scores[i])) for i in np.argsort(-scores)[:TOP_K]]
    passages = "\n\n".join(f"[{n}] ({c['kind']}, page {c['page']})\n{c['text']}"
                           for n, (c, _) in enumerate(hits, start=1))
    text, model = generate(PROMPT.format(refusal=REFUSAL, passages=passages, question=question))
    return {"question": question, "strategy": strategy, "answer": text, "hits": hits, "model": model}


def print_result(r):
    print(f"\n[{r['strategy']}] Q: {r['question']}\nA ({r['model']}): {r['answer']}\nRetrieved:")
    for n, (c, s) in enumerate(r["hits"], start=1):
        print(f"  [{n}] {s:.3f} {c['kind']:8} p{c['page']}: {c['text'][:90]!r}")


# ---------------------------------------------------------------- 6. evaluate

# The 3 sample questions from the assignment, then one question per hard
# element: a table cell, the chart, a footnote, a call-out; and one
# out-of-scope question that must be refused.
EVAL_QUESTIONS = [
    "How much less energy does iMac use compared to the ENERGY STAR requirement?",
    "What is the total carbon footprint of the iMac, and how much of that comes from "
    "product use versus manufacturing?",
    "What material is the iMac stand made from, and what percentage of it is recycled?",
    "What is the power consumption of iMac in sleep mode at 115V?",
    "In the carbon emissions chart, what were the baseline emissions and how many "
    "kilograms did the reductions remove?",
    "Which power adapter was used when testing iMac against the ENERGY STAR requirement?",
    "What percentage of the fiber packaging is recycled content?",
    "How many iMac units did Apple sell worldwide in 2024?",
]


def run_eval():
    index = load_index()
    lines = ["# Task 3 - iMac Environmental Report: Multimodal RAG results", "",
             f"- Chat model: `{CHAT_MODEL}` | Embedding model: `{EMBED_MODEL}` | top-k = {TOP_K}  ",
             "- **plain** = page text only | **structured** = + tables, footnotes, endnotes "
             "| **vision** = structured + chart/call-out descriptions from page images", ""]
    for q in EVAL_QUESTIONS:
        lines += [f"## {q}", ""]
        for strategy in STRATEGIES:
            r = answer(q, index, strategy)
            print_result(r)
            lines += [f"**{strategy}** - *{r['model']}*", "", r["answer"], "",
                      "<details><summary>Retrieved</summary>", ""]
            lines += [f"{n}. {s:.3f} - {c['kind']}, page {c['page']}: {c['text'][:150]!r}"
                      for n, (c, s) in enumerate(r["hits"], start=1)]
            lines += ["", "</details>", ""]
    (HERE / "results.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {HERE / 'results.md'}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "parse":
        cmd_parse()
    elif cmd == "index":
        build_index()
    elif cmd == "ask" and len(sys.argv) > 2:
        strategy = sys.argv[-1] if sys.argv[-1] in STRATEGIES else "vision"
        words = sys.argv[2:-1] if sys.argv[-1] in STRATEGIES else sys.argv[2:]
        print_result(answer(" ".join(words), load_index(), strategy))
    elif cmd == "eval":
        run_eval()
    else:
        print(__doc__)
