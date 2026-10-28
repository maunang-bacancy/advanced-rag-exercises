# Task 1 - GPO Annual Report: Baseline RAG

Question answering over the GPO 2018 Annual Report (82 pages, two-column narrative text).

## Pipeline

| Step | What it does |
|---|---|
| Ingestion | PyMuPDF text blocks, re-ordered for two-column pages: full-width blocks act as bands; between bands, the left column is read before the right. Words split by line wraps are re-joined (real hyphens like "Vance-Cooks" are kept). |
| Chunking | Whole sentences packed into ~900-character chunks with a 2-sentence overlap; each chunk keeps its page numbers. 362 chunks. |
| Embeddings | `gemini-embedding-001` (`RETRIEVAL_DOCUMENT` for chunks, `RETRIEVAL_QUERY` for questions), stored as normalised vectors in `index/`. |
| Retrieval | Cosine similarity, top 5 chunks. |
| Generation | `gemini-flash-lite-latest`, temperature 0. The prompt allows only facts from the numbered passages, requires `[n]` citations, and a fixed refusal when the answer is not there. `gemini-flash-latest` is the fallback. |
| Reliability | 60 s timeout per call; retries on rate limits (429), overload (503) and network errors. |

## Run

```
..\.venv\Scripts\python rag.py index        # once
..\.venv\Scripts\python rag.py ask "your question"
..\.venv\Scripts\python rag.py eval         # writes results.md
```

## Results (7 questions, checked against the PDF text)

| Question | Answer | Verdict |
|---|---|---|
| Depository libraries visited in FY 2018 *(sample)* | 166 | Correct (not the 1,133 total) |
| System that replaced FDsys, and when fully deployed *(sample)* | govinfo; FDsys retired / replaced in December 2018 | Correct (not December 2017, the beta end) |
| Printing and information products procured in FY 2018 *(sample)* | More than $375 million (also cites $375.7M, the same work on p. 17) | Correct, slightly loose wording ("revenues") |
| Copies and titles distributed | 802,834 copies of 4,211 titles | Correct |
| When govinfo left beta | December 2017 | Correct |
| Passports produced in FY 2018 | ~16 million / 16.1 million | Correct (report also states 15,901,600) |
| Libraries visited in FY 2020 *(out of scope)* | "I could not find this in the document." | Correct refusal |

**7/7 correct, all with page citations.** Full answers and retrieved passages are in `results.md`.

## Observations and next improvements

- Running page headers ("U . S . G O V E R N M E N T P U B L I S H I N G O F F I C E") leak into some chunks. They didn't affect answers here, but they waste context and add noise to embeddings. Removing text repeated in the page margins would fix it.
- Retrieval sometimes ranks the answer passage 3rd rather than 1st (e.g. the libraries question). Hybrid search (BM25 + vectors) and a cross-encoder reranker would improve ranking.
- `gemini-flash-latest` returned frequent 503 "high demand" errors on the free tier, so Flash-Lite is the default.
- The free embedding tier allows about 100 chunks per minute; indexing paused and resumed automatically.
