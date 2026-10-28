# Advanced RAG Hands-on Challenge: 3 Tasks

One RAG system per document, each built around what makes that document hard. See `REQUIREMENTS.md` for the brief.

| Task | Document | Main technique | Result | Details |
|---|---|---|---|---|
| 1. Baseline RAG | GPO 2018 Annual Report (82 pp, narrative) | Two-column reading order, sentence chunks with overlap, Gemini embeddings, grounded answers with citations and refusal | **7/7 correct** (3 sample questions + traps + out-of-scope refusal) | [task1_gpo/README.md](task1_gpo/README.md) |
| 2. Advanced document RAG | World Bank GEP June 2026 (200 pp, structured) | Font-based heading state machine, text repair, section-only chunking, heading-path embeddings, LLM query routing (region / chapter / executive summary), small-to-big retrieval | **7/8 correct, 1 partial** (all 3 samples pass); flat retrieval: 6/8, and it misses the executive-summary question | [task2_gep/README.md](task2_gep/README.md) |
| 3. Multimodal RAG | Apple iMac Environmental Report (12 pp, visual) | Table rebuild from cell geometry, superscript footnote linking, endnote chunks, Gemini vision on page images with number verification | **8/8 correct** in all strategies; retrieval **MRR 0.71 → 0.86 → 1.00** (plain → structured → + vision) | [task3_imac/README.md](task3_imac/README.md) |

## Stack

| Part | Choice |
|---|---|
| PDF parsing | PyMuPDF (fonts, positions, superscript flags, table cells, page rendering) |
| Embeddings | Google `gemini-embedding-001`, normalised vectors in NumPy (`index/` per task) |
| Answers, routing, vision | `gemini-flash-lite-latest`, temperature 0; `gemini-flash-latest` as fallback |
| Reliability | 60 s timeout per call. On a rate limit, wait the time the server asks for. On overload or network errors, retry, then switch to the fallback model |

`shared.py` holds the Gemini helpers used by Tasks 2 and 3. Task 1 keeps its own copy so it stays self-contained.

## Setup

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # then put your Gemini key in .env
cd task1_gpo  &&  ..\.venv\Scripts\python rag.py index  &&  ..\.venv\Scripts\python rag.py eval
```

## Web UI

```
.venv\Scripts\python app\server.py          # then open http://localhost:8000
```

- Pick a report, type a question or click a sample, and choose the retrieval method: structured vs. flat for Task 2, and plain, text + tables, or + vision for Task 3.
- Citation numbers in the answer highlight the passage they refer to.
- Every passage shows its section path and page. Clicking a page number opens the original PDF page.
- Task 2 answers show the filter the router chose (e.g. `region = MNA`).
- Links like `http://localhost:8000/?doc=gep&strategy=flat&q=What%20is%20the%20growth%20outlook%20for%20Pakistan%3F` open a report and ask straight away. They're useful for demos.

The server loads the saved indexes, so run `rag.py index` for each task first.

## Lessons across the three

1. **Look at the PDF before writing the pipeline.** Each fix came from inspecting fonts, spans and drawings: column order (GPO), heading fonts and the dropped "Th" ligature (World Bank), superscript markers and corrupted table decimals (iMac).
2. **Structure beats similarity when the words repeat.** In the World Bank report, the same risks appear in the Executive Summary and in Chapter 1. Only a filter routes "according to the executive summary" to the right place.
3. **Tables need real structure.** Re-reading cells fixed the iMac decimals. The World Bank table with two fiscal-year header rows still misleads the model for Pakistan (years shifted by one). This is the clearest next improvement.
4. **Vision helps for charts, but needs guarding.** It was the only way to put the chart's 445 → −99 → 346 kg relationship into one passage. Most page descriptions were noise and had to be filtered, and its numbers are checked against the text layer.
5. **The free Gemini tier shapes the engineering.** It allows about 100 embeddings and 15 Flash-Lite requests per minute, and `gemini-flash-latest` often returned 503. The retry logic matters as much as the RAG logic.
