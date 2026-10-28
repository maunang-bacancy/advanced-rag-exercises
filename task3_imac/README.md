# Task 3 - iMac Environmental Report: Multimodal / Complex Document RAG

A 12-page report laid out like a brochure. Most facts sit outside normal paragraphs: in a waterfall chart, two tables, large call-out numbers, and 19 numbered endnotes. The brief asks to *explore* extraction techniques, so three strategies are built and compared on the same questions.

## What I found in the PDF first

| Element | Problem with plain text extraction | Evidence |
|---|---|---|
| Power consumption table (p. 12) | PyMuPDF's table extractor corrupts decimals: `0.27W` → `027W` | Its output: `['Off', '027W\n.', ...]` |
| Footnote markers | Marker digits stick to the word before them: `recycled content1`, `ENERGY STAR.18` | They are flagged as **superscript** spans in the PDF (the 2 in CO₂ is a subscript, so it is not confused) |
| Endnotes (pp. 11-12) | The caveat sits pages away from the claim it limits | e.g. footnote 18 says iMac was tested with the 143W Power Adapter |
| Carbon chart (p. 2) | The text has `445 kg`, `–99 kg`, `346 kg` and bar labels, but not which number belongs to which bar | Page text dump |
| Call-outs (p. 6) | "76%" and "recycled content in fiber packaging" are separate text blocks | Page text dump |

## The three strategies

| Strategy | What is indexed |
|---|---|
| **plain** | Page text only, cut into ~900-character chunks. What basic RAG does. |
| **structured** | **Tables:** PyMuPDF finds the cell grid, each cell's words are re-read from the page (correct decimals), and every row also becomes a sentence, e.g. `Power consumption for iMac - Sleep: 100V = 0.56W; 115V = 0.57W; 230V = 0.59W`.<br>**Footnotes:** superscript markers become `[^18]`, and the endnote text is appended to the chunk that cites it.<br>**Endnotes:** also indexed as their own chunks.<br>Running page headers are removed, and table text is not duplicated in body chunks. |
| **vision** | Structured, plus a Gemini (Flash-Lite) description of each visual page, rendered at 150 dpi.<br>**Verification:** every number the model reports is checked against the page's text layer, and the chunk records whether all numbers were confirmed.<br>**Filtering:** descriptions with fewer than 2 numbers are dropped. Pages 1, 3, 5, 7 and 8 only repeat the same decorative lifecycle diagram, which is noise.<br>**Caching:** results are saved in `cache/`, so re-indexing makes no new vision calls. |

What vision recovered (all numbers confirmed against the text layer):
- **Page 2 chart:** "Baseline emissions 445 kg → Emissions reductions −99 kg → iMac emissions 346 kg", with each bar's components.
- **Page 4:** each material photo with its claim, e.g. "100 percent recycled aluminum in the stand".
- **Page 6:** the call-outs "100% fiber-based packaging", "76% recycled content in fiber packaging" and "100% responsibly sourced virgin wood fiber".

## Results

**Answers.** All 8 questions are answered correctly by all three strategies, including the 3 sample questions, and the out-of-scope question ("iMac units sold in 2024") is refused each time. On a 12-page document, the model can often piece an answer together even from messy text.

**Retrieval quality is where the strategies differ.** The table below shows the rank of the first retrieved passage that actually contains the evidence:

| Question | plain | structured | vision |
|---|---|---|---|
| Energy vs. ENERGY STAR *(sample)* | 1 | 1 | 1 |
| Total carbon footprint; use vs. manufacturing *(sample)* | 1 | 1 | 1 |
| Stand material and recycled % *(sample)* | 2 | 1 | 1 |
| Sleep power at 115V (table) | 2 | 1 | 1 |
| Chart: baseline and reduction (chart) | not found | not found | 1 |
| Power adapter used for ENERGY STAR test (footnote) | 1 | 1 | 1 |
| Recycled content of fiber packaging (call-out) | 1 | 1 | 1 |
| **MRR** | **0.71** | **0.86** | **1.00** |
| Ranked #1 | 4/7 | 6/7 | 7/7 |

Answers:
- Energy: **58 percent less** than the ENERGY STAR requirement (footnote 18 gives the test conditions).
- Footprint: **346 kg CO2e** total for iMac (two ports) 256GB, of which **production is 50%** and **product use 45%**.
- Stand: **100 percent recycled aluminum**.
- Sleep power at 115V: **0.57W**.
- Chart: baseline **445 kg**, reductions **−99 kg**.
- Power adapter: **143W Power Adapter** (with AC Cable 1m and DC Cable 2m).
- Fiber packaging: **76%** recycled content.

### What this shows

- **Tables:** rebuilding them moved the table answer from rank 2 to rank 1, and the values are correct. The built-in table extractor would have produced `057W`.
- **The chart needed vision.** Without it, no single passage holds both chart numbers. The plain and structured answers were right only because the model stitched together two separate scraps from the top 6. With vision, one passage states the relationship and ranks first.
- **Footnotes travel with the claim.** The "58 percent less energy" chunk now carries footnote 18's test conditions, so a follow-up about how it was tested is answered from the same chunk.
- **Vision needs guarding.** 5 of 8 page descriptions were repetitive noise and are filtered out. Numbers are verified against the text layer, and the answer prompt prefers text and table passages when they disagree with a machine-read chart.
- **Limitation:** with 7 answerable questions this is a sanity check, not a benchmark. The vision filter and evidence patterns were set while looking at this document.

## Run

```
..\.venv\Scripts\python rag.py parse                 # extraction only, no API calls
..\.venv\Scripts\python rag.py index                 # vision captions (cached) + embeddings
..\.venv\Scripts\python rag.py ask "What is the power consumption in sleep mode at 115V?" vision
..\.venv\Scripts\python rag.py eval                  # answers x 3 strategies -> results.md
..\.venv\Scripts\python retrieval_eval.py            # rank / MRR table appended to results.md
```
