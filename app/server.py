"""
Web UI for the three RAG tasks.

Loads each task's rag.py and its saved index once at startup, then serves:
  GET  /                         the page (static/index.html)
  GET  /api/docs                 documents, strategies, sample questions
  POST /api/ask                  {doc, question, strategy} -> answer + passages
  GET  /api/page/{doc}/{page}    a PDF page as PNG (for checking citations)

Run from the practical folder:
  .venv\\Scripts\\python app\\server.py        ->  http://localhost:8000
"""

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path

import pymupdf
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)


def load_module(name: str, path: Path):
    """Each task's code lives in a file called rag.py, so load them under
    distinct module names."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


print("Loading tasks and indexes ...")
gpo = load_module("rag_gpo", ROOT / "task1_gpo" / "rag.py")
gep = load_module("rag_gep", ROOT / "task2_gep" / "rag.py")
imac = load_module("rag_imac", ROOT / "task3_imac" / "rag.py")

gpo_index = gpo.load_index()      # (chunks, vecs)
gep_index = gep.load_index()      # (children, parents, vecs)
imac_index = imac.load_index()    # {"plain": (...), "structured": (...)}
print("Ready.")

DOCS = {
    "gpo": {
        "task": 1, "title": "GPO 2018 Annual Report", "publisher": "U.S. Government Publishing Office",
        "pages": 82, "shape": "Narrative text",
        "challenge": "Facts sit inside ordinary sentences, on two-column pages.",
        "pdf": gpo.PDF, "samples": gpo.EVAL_QUESTIONS,
        "strategies": [{"id": "baseline", "label": "Baseline",
                        "desc": "Sentence chunks, Gemini embeddings, top 5 passages"}],
    },
    "gep": {
        "task": 2, "title": "Global Economic Prospects, June 2026", "publisher": "World Bank",
        "pages": 200, "shape": "Long and structured",
        "challenge": "Six regions and four chapters use the same words; only the location tells them apart.",
        "pdf": gep.PDF, "samples": gep.EVAL_QUESTIONS,
        "strategies": [
            {"id": "structured", "label": "Structured",
             "desc": "Routes the question to a region or chapter, answers from whole sections"},
            {"id": "flat", "label": "Flat",
             "desc": "No routing; answers from the closest small chunks"},
        ],
    },
    "imac": {
        "task": 3, "title": "iMac Product Environmental Report", "publisher": "Apple, October 2024",
        "pages": 12, "shape": "Short and visual",
        "challenge": "Facts live in a chart, tables, call-outs and footnotes.",
        "pdf": imac.PDF, "samples": imac.EVAL_QUESTIONS,
        "strategies": [
            {"id": "vision", "label": "Text + tables + vision",
             "desc": "Adds chart and call-out descriptions read from the page images"},
            {"id": "structured", "label": "Text + tables",
             "desc": "Rebuilt tables, footnotes linked to their claims"},
            {"id": "plain", "label": "Plain text",
             "desc": "Page text only, as basic RAG would read it"},
        ],
    },
}

app = FastAPI(title="Report Q&A")


class Ask(BaseModel):
    doc: str
    question: str
    strategy: str | None = None


def passage(n, chunk, score, pages, path, kind):
    return {"n": n, "score": round(score, 3), "pages": pages, "path": path,
            "kind": kind, "text": chunk["text"]}


@app.get("/api/docs")
def docs():
    return [{"id": k, **{f: v for f, v in d.items() if f != "pdf"}} for k, d in DOCS.items()]


@app.post("/api/ask")
def ask(req: Ask):
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "Type a question first.")
    if req.doc not in DOCS:
        raise HTTPException(404, f"Unknown document '{req.doc}'.")
    try:
        if req.doc == "gpo":
            r = gpo.answer(question, *gpo_index)
            hits = [passage(n, c, s, c["pages"], [], "text")
                    for n, (c, s) in enumerate(r["hits"], 1)]
            filters = None
        elif req.doc == "gep":
            r = gep.answer(question, *gep_index, structured=req.strategy != "flat")
            hits = [passage(n, c, s, c["pages"], c["path"], c["kind"])
                    for n, (c, s) in enumerate(r["hits"], 1)]
            filters = {k: v for k, v in r["filters"].items() if v} if req.strategy != "flat" else None
        else:
            strategy = req.strategy if req.strategy in imac.STRATEGIES else "vision"
            r = imac.answer(question, imac_index, strategy)
            hits = [passage(n, c, s, [c["page"]], [], c["kind"])
                    for n, (c, s) in enumerate(r["hits"], 1)]
            filters = None
    except Exception as e:  # surface Gemini quota / network problems as a readable message
        msg = str(e)
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
            raise HTTPException(429, "Gemini's free-tier limit was reached. Wait a minute and ask again.")
        raise HTTPException(502, f"The answer could not be generated: {msg[:200]}")
    return {"answer": r["answer"], "model": r["model"], "filters": filters, "passages": hits}


@lru_cache(maxsize=64)
def render(doc: str, page: int) -> bytes:
    with pymupdf.open(DOCS[doc]["pdf"]) as pdf:
        if not 1 <= page <= len(pdf):
            raise HTTPException(404, f"Page {page} does not exist.")
        return pdf[page - 1].get_pixmap(dpi=110).tobytes("png")


@app.get("/api/page/{doc}/{page}")
def page_image(doc: str, page: int):
    if doc not in DOCS:
        raise HTTPException(404, f"Unknown document '{doc}'.")
    return Response(render(doc, page), media_type="image/png",
                    headers={"Cache-Control": "max-age=86400"})


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
