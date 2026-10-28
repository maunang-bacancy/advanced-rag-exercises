"""
Task 2 - Advanced document RAG over the World Bank Global Economic Prospects,
June 2026 (200 pages, chapters / regions / boxes / sub-sections).

Pipeline
  parse:  PDF -> lines classified by font (heading level, body, box body,
          figure text, table cell, noise) -> heading state machine tracks
          chapter > region > section > sub-section > box for every line
  repair: "Te"/"Tis" -> "The"/"This" (the PDF drops the Th ligature),
          fi/fl ligature characters -> plain letters
  chunk:  text is packed inside ONE leaf section only (never across headings);
          small child chunks (~250 tokens) for search, grouped into parent
          passages (~900 tokens) for answering; figures/tables get own chunks
  embed:  heading path + text, e.g. "[Chapter 2: Regional Outlooks > South Asia
          > Outlook] Growth in SAR is expected to ..."
  ask:    LLM router turns the question into filters {region, chapter,
          executive summary} -> search children inside the filter -> answer
          from their parents, citing [n]

Usage
  python rag.py parse                 # parse only, print structure stats (no API calls)
  python rag.py index
  python rag.py ask "What is the growth outlook for South Asia?"
  python rag.py eval                  # structured vs flat retrieval, writes results.md
"""

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared import CHAT_MODEL, EMBED_MODEL, PDF_DIR, embed, generate  # noqa: E402

HERE = Path(__file__).resolve().parent
PDF = PDF_DIR / "GEP-Jun-2026.pdf"
INDEX = HERE / "index"

FIRST_PAGE = 17          # pages 1-16: cover, copyright, tables of contents
LAST_PAGE = 191          # pages 192-200: lists of other publications
CHILD_CHARS = 1000       # ~250 tokens: what we search
PARENT_CHARS = 3600      # ~900 tokens: what the model reads
TOP_CHILDREN = 10
TOP_PARENTS = 4
REFUSAL = "I could not find this in the document."

REGIONS = {
    "East Asia and Pacific": "EAP",
    "Europe and Central Asia": "ECA",
    "Latin America and the Caribbean": "LAC",
    "Middle East, North Africa, Afghanistan, and Pakistan": "MNA",
    "South Asia": "SAR",
    "Sub-Saharan Africa": "SSA",
}
SKIP_SECTIONS = {"References", "Abbreviations", "Contents", "Summary of Contents"}


# ---------------------------------------------------------------- 1. parse

def classify(span) -> str:
    """Role of a line, decided by the font of its first span.

    Found by surveying the PDF's fonts: body text is Garamond 11 (10.6 inside
    boxed articles); headings are Helvetica / larger Garamond.
    """
    font, size = span["font"], round(span["size"], 1)
    if font == "HelveticaLTStd-Cond" and size >= 20:
        return "front"          # Foreword, Executive Summary, ...
    if font == "AGaramondPro-Regular" and size >= 25:
        return "chapter"        # CHAPTER 1 / GLOBAL OUTLOOK / chapter titles
    if font == "HelveticaLTStd-Bold" and size == 16.0:
        return "h1"             # sections; in Chapter 2 these are the regions
    if font == "HelveticaLTStd-Roman" and size == 11.0:
        return "h2"
    if font == "AGaramondPro-BoldItalic" and size >= 10.5:
        return "h3"
    if font == "HelveticaLTStd-Bold" and size == 10.0:
        return "caption"        # BOX 1.1 / FIGURE 2.3 / TABLE 1.1 titles
    if font.startswith("AGaramondPro") and 10.5 <= size <= 11.1:
        return "box" if size < 10.9 else "body"
    if font == "HelveticaLTStd-Obl" and size == 8.0:
        return "figure"         # the explanatory text printed beside figures
    if font == "HelveticaLTStd-Bold" and size == 7.0:
        return "figure"         # figure panel titles, "A. Share of economies"
    if font.startswith("HelveticaLTStd") and size in (7.6, 8.0):
        return "table"
    return "noise"              # axis labels, running headers, source notes


def reading_order(page):
    """Text blocks of a two-column page in reading order (same idea as Task 1:
    full-width blocks are bands; between bands, left column then right)."""
    mid = page.rect.width / 2
    blocks = [b for b in page.get_text("dict")["blocks"] if b.get("lines")]
    blocks.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))
    out, left, right = [], [], []

    def flush():
        out.extend(sorted(left, key=lambda b: b["bbox"][1]))
        out.extend(sorted(right, key=lambda b: b["bbox"][1]))
        left.clear()
        right.clear()

    for b in blocks:
        x0, _, x1, _ = b["bbox"]
        if x1 <= mid + 10:
            left.append(b)
        elif x0 >= mid - 10:
            right.append(b)
        else:
            flush()
            out.append(b)
    flush()
    return out


class State:
    """Where we are in the document while reading it top to bottom."""

    def __init__(self):
        self.chapter, self.chapter_no, self.region = None, None, None
        self.h1 = self.h2 = self.h3 = self.box = None
        self.saved = None       # state to restore when a boxed article ends

    def snapshot(self):
        return dict(self.__dict__, saved=None)


def section_path(st: dict) -> list[str]:
    """Heading path of a state snapshot. In Chapter 2, h1 is the region name."""
    return [p for p in (st["chapter"], st["h1"], st["box"], st["h2"], st["h3"]) if p]


def parse(pdf_path: Path):
    """Return a list of text pieces, each tagged with its section and page.

    Pieces: {"kind": text|figure|table, "page", "state": snapshot, "text"}
    """
    st = State()
    pieces = []
    last_role = None
    box_title_open = False

    def add(kind, page, text):
        if st.h1 in SKIP_SECTIONS or st.chapter in SKIP_SECTIONS:
            return
        pieces.append({"kind": kind, "page": page, "state": st.snapshot(), "text": text})

    with pymupdf.open(pdf_path) as doc:
        for pno in range(FIRST_PAGE, LAST_PAGE + 1):
            page = doc[pno - 1]
            table_cells = []      # (y, x, text) - rebuilt into rows at page end
            last_role = None      # headings never continue across pages
            for block in reading_order(page):
                for line in block["lines"]:
                    spans = [s for s in line["spans"] if s["text"].strip()]
                    if not spans:
                        continue
                    text = "".join(s["text"] for s in spans).strip()
                    role = classify(spans[0])
                    cont = role == last_role  # multi-line heading continues
                    if role != "caption":
                        box_title_open = False

                    if role == "front":
                        title = f"{st.chapter} {text}" if cont else text
                        st.__init__()
                        st.chapter = title
                    elif role == "chapter":
                        m = re.match(r"CHAPTER (\d+)", text)
                        if m:
                            st.__init__()
                            st.chapter_no = int(m.group(1))
                            st.chapter = f"Chapter {m.group(1)}"
                        elif text.isupper() and "APPENDIX" in text:
                            st.__init__()
                            st.chapter = "Statistical Appendix"
                        elif st.chapter_no:
                            title = text.title() if text.isupper() else text
                            sep = " " if ":" in st.chapter else ": "
                            st.chapter = f"{st.chapter}{sep}{title}"
                    elif role == "h1":
                        h1 = f"{st.h1} {text}" if cont else text
                        if st.saved:  # a section heading always ends a box
                            st.__dict__.update(st.saved)
                        st.h1, st.h2, st.h3, st.box = h1, None, None, None
                        if st.chapter_no == 2:
                            st.region = REGIONS.get(h1)
                    elif role == "h2":
                        st.h2 = f"{st.h2} {text}" if cont else text
                        st.h3 = None
                    elif role == "h3":
                        st.h3 = f"{st.h3} {text}" if cont else text
                    elif role == "caption":
                        m = re.match(r"BOX (\d+\.\d+)\s*(.*)", text)
                        if m and st.box and st.box.startswith(f"Box {m.group(1)} "):
                            pass      # "BOX 1.1 ... (continued)" on a later page
                        elif m:
                            if not st.saved:
                                st.saved = st.snapshot()
                            title = m.group(2).replace("(continued)", "").strip()
                            st.box = f"Box {m.group(1)} {title}".strip()
                            st.h2 = st.h3 = None
                            box_title_open = True
                        elif (cont and box_title_open
                              and not re.match(r"(FIGURE|TABLE)", text)):
                            st.box = f"{st.box} {text}"     # title wraps to 2nd line
                        else:
                            box_title_open = False
                            add("caption", pno, text)
                        last_role = role
                        continue
                    elif role == "box":
                        add("text", pno, text)
                    elif role == "body":
                        if st.box and st.saved:      # main text again: box is over
                            st.__dict__.update(st.saved)
                        add("text", pno, text)
                    elif role == "figure":
                        add("figure", pno, text)
                    elif role == "table":
                        x0, y0 = line["bbox"][0], line["bbox"][1]
                        table_cells.append((y0, x0, text))
                    last_role = role
            if table_cells:
                add("table", pno, rows_from_cells(table_cells))
    return pieces


def rows_from_cells(cells):
    """Group table cells into rows by their y position, cells left to right."""
    rows, current, y_prev = [], [], None
    for y, x, t in sorted(cells):
        if y_prev is not None and abs(y - y_prev) > 2.5:
            rows.append(current)
            current = []
        current.append((x, t))
        y_prev = y
    rows.append(current)
    return "\n".join(" | ".join(t for _, t in sorted(r)) for r in rows if r)


# ---------------------------------------------------------------- 2. repair

LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"}


def make_repairer(all_text: str):
    """The PDF's font drops the 'h' of the 'Th' ligature: 'Te', 'Tis', 'Tese'.

    Fix a capitalised word T+rest only when the document never uses 't'+rest
    in lower case but does use 'th'+rest - so 'Te' -> 'The', but real words
    like 'Trough' or 'To' are left alone. No hand-made word list needed.
    """
    lower = Counter(w for w in re.findall(r"\b[a-z]+\b", all_text))

    def fix_word(m):
        rest = m.group(1)
        if ("t" + rest) not in lower and ("th" + rest) in lower:
            return "Th" + rest
        return m.group(0)

    def repair(text):
        for lig, plain in LIGATURES.items():
            text = text.replace(lig, plain)
        return re.sub(r"\bT([a-z]+)\b", fix_word, text)

    return repair


def clean(text: str) -> str:
    text = re.sub(r"(\w)-\n([a-z])", r"\1\2", text)   # words split by line wrap
    text = text.replace("­", "")
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- 3. chunk

SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(])")


def pack(sentences, limit):
    """Pack sentences into chunks of at most ~limit chars, 1-sentence overlap."""
    chunks, cur = [], []
    for s in sentences:
        if cur and sum(len(x) for x in cur) + len(s) > limit:
            chunks.append(cur)
            cur = cur[-1:]
        cur.append(s)
    if cur:
        chunks.append(cur)
    return [" ".join(c) for c in chunks]


def group_by_size(items, limit):
    """Consecutive items grouped so each group stays under ~limit chars."""
    groups, cur, size = [], [], 0
    for it in items:
        if cur and size + len(it) > limit:
            groups.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += len(it)
    if cur:
        groups.append(cur)
    return groups


def build_chunks(pieces):
    """Turn parsed pieces into (children, parents).

    Consecutive pieces of the same kind in the same leaf section form one group;
    figures and tables additionally group per page. A group is cut into
    children, and consecutive children are bundled into parents - so neither
    ever spans two sections.
    """
    raw = "\n".join(p["text"] for p in pieces)
    for lig, plain in LIGATURES.items():
        raw = raw.replace(lig, plain)
    repair = make_repairer(raw)

    groups = []
    for p in pieces:
        st = p["state"]
        kind = "text" if p["kind"] == "text" else "visual"
        key = (kind, st["chapter"], st["h1"], st["box"], st["h2"], st["h3"])
        if kind == "visual":
            key += (p["page"],)
        if groups and groups[-1][0] == key:
            groups[-1][1].append(p)
        else:
            groups.append((key, [p]))

    children, parents = [], []
    for key, ps in groups:
        st = ps[0]["state"]
        pages = sorted({p["page"] for p in ps})
        if key[0] == "text":
            kind = "text"
            text = repair(clean("\n".join(p["text"] for p in ps)))
            kids = pack([s for s in SENT_SPLIT.split(text) if len(s) > 2], CHILD_CHARS)
            joiner = " "
        else:
            kind = "table" if any(p["kind"] == "table" for p in ps) else "figure"
            captions = [repair(p["text"]) for p in ps if p["kind"] == "caption"]
            rest = [repair(p["text"]) for p in ps if p["kind"] != "caption"]
            text = "\n".join(captions + rest)
            if len(text) < 80:
                continue          # stray labels, nothing worth searching
            kids = [text[i:i + 2000] for i in range(0, len(text), 2000)]
            joiner = "\n"
        meta = {"kind": kind, "pages": pages, "chapter": st["chapter"],
                "chapter_no": st["chapter_no"], "region": st["region"],
                "box": repair(st["box"]) if st["box"] else None,
                "path": [repair(p) for p in section_path(st)]}

        for bundle in group_by_size(kids, PARENT_CHARS):
            pid = f"p{len(parents)}"
            parents.append({"id": pid, "text": joiner.join(bundle), **meta})
            for k in bundle:
                children.append({"id": f"c{len(children)}", "parent": pid, "text": k, **meta})
    return children, parents


def embed_text(c):
    head = " > ".join(c["path"])
    label = f" | {c['kind']}" if c["kind"] != "text" else ""
    return f"[{head}{label}] {c['text']}"


# ---------------------------------------------------------------- 4. index

def cmd_parse():
    pieces = parse(PDF)
    children, parents = build_chunks(pieces)
    print(f"{len(pieces)} pieces -> {len(children)} child chunks, {len(parents)} parents")
    print("by kind:", Counter(c["kind"] for c in children))
    print("by region:", Counter(c["region"] for c in children))
    print("by chapter:", Counter(c["chapter"] for c in children))
    sections = Counter(" > ".join(c["path"]) for c in children)
    print(f"\n{len(sections)} distinct section paths, e.g.:")
    for s, n in list(sections.items())[:: max(1, len(sections) // 40)]:
        print(f"  {n:3d}  {s}")
    return children, parents


def build_index():
    children, parents = cmd_parse()
    print(f"\nEmbedding {len(children)} child chunks with {EMBED_MODEL} ...")
    vecs = embed([embed_text(c) for c in children], "RETRIEVAL_DOCUMENT")
    INDEX.mkdir(exist_ok=True)
    (INDEX / "children.json").write_text(json.dumps(children, indent=1), encoding="utf-8")
    (INDEX / "parents.json").write_text(json.dumps(parents, indent=1), encoding="utf-8")
    np.save(INDEX / "vectors.npy", vecs)
    print(f"Saved index to {INDEX}")


def load_index():
    if not (INDEX / "vectors.npy").exists():
        sys.exit("No index yet - run: python rag.py index")
    children = json.loads((INDEX / "children.json").read_text(encoding="utf-8"))
    parents = {p["id"]: p for p in json.loads((INDEX / "parents.json").read_text(encoding="utf-8"))}
    return children, parents, np.load(INDEX / "vectors.npy")


# ---------------------------------------------------------------- 5. route

ROUTER_PROMPT = """You route questions about the World Bank "Global Economic Prospects, June 2026" report.

The report's structure:
- Executive Summary
- Chapter 1: Global Outlook
- Chapter 2: Regional Outlooks, with one section per region:
  EAP = East Asia and Pacific
  ECA = Europe and Central Asia
  LAC = Latin America and the Caribbean
  MNA = Middle East, North Africa, Afghanistan, and Pakistan
  SAR = South Asia (Bangladesh, Bhutan, India, Maldives, Nepal, Sri Lanka - NOT Pakistan or Afghanistan)
  SSA = Sub-Saharan Africa
- Chapter 3: Sovereign Debt Levels and Interest Rates in EMDEs
- Chapter 4: Fiscal Policy and Commodity Price Swings
- Statistical Appendix

Return JSON: {{"region": one of EAP|ECA|LAC|MNA|SAR|SSA or null,
               "chapter": 1|2|3|4 or null,
               "executive_summary": true|false}}

Rules:
- region: set it when the question names a region or a country in one (map the country
  using the World Bank grouping above). Otherwise null.
- chapter: set it ONLY when the question explicitly names a chapter ("Chapter 3"). Do not
  guess a chapter from the topic - a wrong filter hides the answer.
- executive_summary: true ONLY when the question explicitly mentions the executive summary.

Question: {question}"""


def route(question: str) -> dict:
    try:
        text, _ = generate(ROUTER_PROMPT.format(question=question), json_output=True)
        r = json.loads(text)
        return {"region": r.get("region") if r.get("region") in REGIONS.values() else None,
                "chapter": r.get("chapter") if r.get("chapter") in (1, 2, 3, 4) else None,
                "executive_summary": bool(r.get("executive_summary"))}
    except Exception as e:  # routing is an optimisation; never fail the question
        print(f"  router failed ({e}); searching without filters")
        return {"region": None, "chapter": None, "executive_summary": False}


def matches(c, f):
    if f["region"] and c["region"] != f["region"]:
        return False
    if f["chapter"] and c["chapter_no"] != f["chapter"]:
        return False
    if f["executive_summary"] and c["chapter"] != "Executive Summary":
        return False
    return True


# ---------------------------------------------------------------- 6. retrieve + answer

def retrieve(question, children, parents, vecs, structured=True):
    """structured: route -> filter -> search children -> answer from parents.
    flat (for comparison): no filter, answer from the top child chunks."""
    q = embed([question], "RETRIEVAL_QUERY")[0]
    scores = vecs @ q
    f = route(question) if structured else {"region": None, "chapter": None,
                                            "executive_summary": False}
    mask = np.array([matches(c, f) for c in children])
    if not mask.any():
        mask[:] = True
    scores = np.where(mask, scores, -1)
    order = np.argsort(-scores)

    if not structured:
        return f, [(children[i], float(scores[i])) for i in order[:TOP_PARENTS + 1]]

    seen, hits = set(), []
    for i in order[:TOP_CHILDREN]:
        if not mask[i]:
            break             # filter matched fewer chunks than we asked for
        pid = children[i]["parent"]
        if pid not in seen:
            seen.add(pid)
            hits.append((parents[pid], float(scores[i])))
        if len(hits) == TOP_PARENTS:
            break
    return f, hits


PROMPT = """You answer questions about the World Bank "Global Economic Prospects, June 2026" \
report, using ONLY the numbered passages below. Each passage shows where it comes from \
in the report (chapter > region > section).

Rules:
- Use only facts stated in the passages. Do not add outside knowledge.
- Keep regions and chapters apart: if the question is about one region or chapter, use only
  passages from it and never borrow a number from another region.
- Quote numbers exactly as written. Mention the year each forecast refers to.
- Cite the passages you used like [1] or [2][3].
- If the passages do not contain the answer, reply exactly: "{refusal}"

Passages:
{passages}

Question: {question}
Answer:"""


def answer(question, children, parents, vecs, structured=True):
    f, hits = retrieve(question, children, parents, vecs, structured)
    passages = "\n\n".join(
        f"[{n}] ({' > '.join(c['path'])} | page {', '.join(map(str, c['pages']))}"
        f"{' | ' + c['kind'] if c['kind'] != 'text' else ''})\n{c['text']}"
        for n, (c, _) in enumerate(hits, start=1)
    )
    text, model = generate(PROMPT.format(refusal=REFUSAL, passages=passages, question=question))
    return {"question": question, "filters": f, "answer": text, "hits": hits, "model": model}


def describe(c):
    return f"{' > '.join(c['path'])} (p. {', '.join(map(str, c['pages']))})"


def print_result(r):
    active = {k: v for k, v in r["filters"].items() if v}
    print(f"\nQ: {r['question']}\nFilters: {active or 'none'}\n\nA ({r['model']}): {r['answer']}\n\nRetrieved:")
    for n, (c, score) in enumerate(r["hits"], start=1):
        print(f"  [{n}] {score:.3f} {describe(c)}")


# ---------------------------------------------------------------- 7. evaluate

# The 3 sample questions from the assignment, then checks for the structure
# problems: a country whose region is not obvious (Pakistan is in MNA here),
# two other regions with similar wording, a named chapter, and an
# out-of-scope question that must be refused.
EVAL_QUESTIONS = [
    "What is the growth outlook for South Asia in this report?",
    "What does Chapter 3 say about sovereign debt in developing economies?",
    "According to the executive summary, what are the main risks to the global outlook?",
    "What is the growth outlook for Pakistan?",
    "What is the growth outlook for East Asia and Pacific in 2026?",
    "How fast did growth in Sub-Saharan Africa firm in 2025?",
    "What does Chapter 4 say about fiscal rules in commodity exporters?",
    "What is the projected GDP growth of Brazil in the year 2031?",
]


def run_eval():
    children, parents, vecs = load_index()
    lines = [
        "# Task 2 - World Bank GEP June 2026: Advanced Document RAG results", "",
        f"- Chat model: `{CHAT_MODEL}` | Embedding model: `{EMBED_MODEL}`  ",
        f"- {len(children)} child chunks (~{CHILD_CHARS} chars) in {len(parents)} parents "
        f"(~{PARENT_CHARS} chars)  ",
        "- **Structured** = router filters + search children + answer from parents.  ",
        "- **Flat** = same index, no filters, answer from the top child chunks "
        "(what basic RAG would do).", "",
    ]
    for q in EVAL_QUESTIONS:
        lines += [f"## {q}", ""]
        for structured in (True, False):
            r = answer(q, children, parents, vecs, structured)
            label = "Structured" if structured else "Flat"
            print(f"\n=== {label}")
            print_result(r)
            active = {k: v for k, v in r["filters"].items() if v}
            lines += [f"**{label}**" + (f" (filters: `{active}`)" if structured else "")
                      + f" - *{r['model']}*", "", r["answer"], "",
                      "<details><summary>Retrieved</summary>", ""]
            lines += [f"{n}. {s:.3f} - {describe(c)}" for n, (c, s) in enumerate(r["hits"], 1)]
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
        print_result(answer(" ".join(sys.argv[2:]), *load_index()))
    elif cmd == "eval":
        run_eval()
    else:
        print(__doc__)
