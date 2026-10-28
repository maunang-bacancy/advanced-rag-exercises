# RAG Hands-On: 3 Practicals - Requirements

## Official brief (Advanced RAG Hands-on Challenge)

**Deadline: 25 October 2026.** Tasks are assigned on the **POD platform**; update progress there regularly as you work.

| Task | Title | Focus (from the brief) |
|---|---|---|
| 1 | GPO Annual Report: **Baseline RAG** | Ingestion, chunking, embeddings, retrieval, response generation |
| 2 | World Bank Economic Report: **Advanced Document RAG** | Long, structured document: hierarchical / section-aware chunking, metadata, retrieval, context management |
| 3 | Apple iMac Environmental Report: **Multimodal / Complex Document RAG** | Information in tables, charts and footnotes; explore parsing and extraction techniques to make it searchable |

Validate each task with the provided sample questions (listed below). The stated goal is hands-on experience with each kind of RAG problem, not only finishing.

---

Source material (local copy): `drive-download-20261025T142645Z-1-001(1)/`

| File | Role |
|---|---|
| `RAG-Hands-On-Use-Cases.docx` | The assignment: 3 use cases, test questions, pass/fail bar |
| `Advanced RAG Field Guide.html` | Reference solution: how basic RAG fails, and which techniques fix it |
| `GPO-Anual-report.pdf` | Document for Practical 1 |
| `GEP-Jun-2026.pdf` | Document for Practical 2 |
| `iMac_PER_Oct2024.pdf` | Document for Practical 3 |

Build one question-answering (RAG) system per document. The documents get harder in order: plain text, then long and structured, then visual.

---

## Practical 1 (Task 1): Plain-text report Q&A, baseline RAG

> The brief only asks for a working **baseline** here: a clean ingest, chunk, embed, retrieve and generate pipeline. The field guide's extras below (reading order, hybrid search, reranking) are optional improvements.

**Document:** GPO 2018 Annual Report (82 pages). Ordinary paragraphs in a two-column layout, with numbers written into sentences.

### Must answer
- How many depository libraries did GPO staff visit in FY 2018?
- What new digital system replaced FDsys, and when was it fully deployed?
- How much did GPO procure in printing and information products in FY 2018 on behalf of federal agencies?

### Pass bar
- The system finds the exact fact in the paragraph.
- It adds nothing that isn't in the document.
- For a question the report can't answer (e.g. about FY 2020), it replies *"I could not find this in the document."* A refusal counts as a correct answer.

### Techniques (from the field guide)
| Problem | Fix |
|---|---|
| Columns come out interleaved | Read the left column, then the right |
| Chunks cut mid-sentence | Sentence-based chunks with overlap |
| Right passage found but ranked low | Hybrid search (dense + BM25, merged with RRF), then a cross-encoder rerank of the top 20 |
| Model fills gaps from memory | Cite numbered passages, or refuse |

Field guide result: sentence chunking gave the biggest jump. Hybrid search alone didn't help on this document; the reranker did.

---

## Practical 2 (Task 2): Long, structured report, advanced document RAG

**Document:** World Bank Global Economic Prospects, June 2026 (200 pages). Four chapters (Global Outlook, Regional Outlooks, Sovereign Debt, Fiscal Policy), sections and sub-sections, boxed articles, and a statistics section at the end.

### Must answer
- What is the growth outlook for South Asia in this report?
- What does Chapter 3 say about sovereign debt in developing economies?
- According to the executive summary, what are the main risks to the global outlook?

### Pass bar
- The system retrieves from the right chapter and section.
- It never mixes up facts between regions or chapters.

### Techniques
| Problem | Fix |
|---|---|
| Broken extracted text ("Te conflict", "sustainabil- ity") | Repair ligatures and hyphenation |
| No idea which chapter or region a chunk is from | Rebuild headings from fonts (body text is 11pt Garamond, headings are Helvetica) and store chapter, region and heading path as metadata |
| Passage says "SAR", not "South Asia" | Embed the heading path in front of the chunk text |
| Question names a place | Turn it into a metadata filter, using regex rules or an LLM JSON call |
| Small chunks lack context | Search small child chunks (~280 tokens), answer from the parent section (~900 tokens) |

Traps:
- The basic version answered "about 4 percent" for South Asia, which is wrong. The report says growth softens to **6.3 percent in 2026** (page 96).
- **Pakistan** is grouped with Middle East & North Africa in this report, not South Asia (answer: 1.6 percent).
- Allow a chapter filter only when the question names the chapter. A wrong filter hides the answer.

---

## Practical 3 (Task 3): Short, visual document, multimodal RAG

> The brief says to *explore* parsing and extraction techniques, so write down what you tried (e.g. plain text vs. table rebuild vs. vision model) and what each one changed.

**Document:** Apple iMac Product Environmental Report (11-12 pages). Laid out like a brochure: most facts sit in charts, small tables, icon call-outs and numbered footnotes.

### Must answer
- How much less energy does iMac use compared to the ENERGY STAR requirement?
- What is the total carbon footprint of the iMac, and how much comes from product use versus manufacturing?
- What material is the iMac stand made from, and what percentage of it is recycled?

### Pass bar
- The system pulls the right number from the chart or table, not a general sentence nearby.

### Techniques
| Problem | Fix |
|---|---|
| Table values separated from their column | Find the dark header bars in the page drawings and rebuild rows from word positions |
| A claim's caveat is on a different page | Link footnote markers to their endnotes and index them together |
| Tables cut by character count | One chunk per element (a table, call-out, etc.) |
| A chart says something the text doesn't | A vision model describes each visual page (cached) |

Example: the carbon chart on page 2 shows a 445 kg baseline minus 99 kg of reductions = **346 kg**. Text extraction gets the labels, not the arithmetic.

Note: in the field guide the reranker made results *worse* here, because it was trained on prose and under-rates table chunks. Measure before keeping it.

---

## Applies to all three

### Evaluation
- Write 10-14 hand-checked questions per document, each with the page and passage that holds the answer.
- Build a **basic RAG baseline** first, then add one technique at a time.
- Measure **MRR** (mean reciprocal rank): 1.0 means the answer passage always ranked first; 0.5 means second on average.
- Also check whether each final answer is correct.
- Treat differences under about 0.1 as noise.

### Proposed stack
| Part | Choice | Notes |
|---|---|---|
| LLM (answers, query routing) | Gemini `gemini-flash-latest` | Reuses `GOOGLE_API_KEY` from `POD Chatbox/.env` |
| Vision (Practical 3 charts) | Gemini Flash | Same key |
| Embeddings | Local `BAAI/bge-small-en-v1.5` | Avoids free-tier rate limits on the 200-page PDF. `models/gemini-embedding-001` is an alternative |
| Vector DB + BM25 | Qdrant (local mode, no Docker) | |
| Reranker | Local MiniLM cross-encoder | |
| PDF parsing | PyMuPDF | Gives the fonts, positions and drawings that Practicals 2 and 3 need |

Reference stack used in the field guide: bge-small embeddings, Qdrant BM25, MiniLM cross-encoder, Groq gpt-oss-120b for answers, Gemini Flash-Lite for vision.

### Checklist
- [ ] Practical 1: baseline, then sentence chunks, hybrid search, rerank and grounded answers. Score it.
- [ ] Practical 2: text repair, font-based headings and metadata, heading-prefixed embeddings, query filters, parent-child chunks. Score it.
- [ ] Practical 3: table rebuild, footnote linking, element chunks, vision descriptions. Score it.
- [ ] Final scorecard comparing basic and advanced versions on all three documents.
- [ ] Update progress on the POD platform after each task.
