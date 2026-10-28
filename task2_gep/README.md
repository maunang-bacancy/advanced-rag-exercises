# Task 2 - World Bank GEP June 2026: Advanced Document RAG

Question answering over a 200-page report with chapters, six regional sections, boxed articles, sub-sections and a statistical appendix. Many sections use the same words ("growth is projected to..."), so the system has to know **where** a passage lives, not just what it says.

## What I found in the PDF first

| Finding | How |
|---|---|
| Body text is Garamond 11pt (10.6pt inside boxes); headings are Helvetica Bold 16 (sections / regions), Helvetica 11 (sub-sections), Garamond Bold Italic (sub-sub-sections), Garamond 26-30 (chapter titles) | Font survey of every text span |
| The font drops the "h" of the "Th" ligature: "Te" ×151, "Tis" ×52; ~1,000 "ﬁ"/"ﬂ" ligature characters | Regex counts on the extracted text |
| PDF bookmarks are off by one page (South Asia bookmarked p. 95, heading on p. 96) | Bookmarks vs. heading positions, so bookmarks are **not** used |
| Tiny Helvetica = chart axis labels; Helvetica Light 7pt = running page header | Samples per font |

## Pipeline

| Step | What it does |
|---|---|
| Parse | Each line is classified by its font (heading level, body, box body, figure text, table cell, noise). Two-column pages are read left column then right. |
| Heading state machine | While reading top to bottom it tracks **chapter > section (region in Ch. 2) > box > sub-section > sub-sub-section**. "BOX 1.1 ... (continued)" on later pages stays the same box; the box closes when main text resumes. Pages 1-16 (contents) and 192-200 (publication lists) and all References sections are skipped. |
| Repair | Ligatures normalised; "T"+word becomes "Th"+word **only** if the document never uses "t"+word in lower case but does use "th"+word. "Te" → "The", while real words like "Trough" are untouched. No hand-made word list. Result: 0 damaged words left. |
| Section-aware chunking | Text is packed **only inside one leaf section**, never across a heading. Child chunks ~1,000 chars (~250 tokens) are searched; consecutive children form parent passages ~3,600 chars (~900 tokens) that the model reads. Figure text and table rows get their own chunks (tables rebuilt row by row from cell positions). |
| Metadata | Every chunk carries `chapter`, `chapter_no`, `region` (EAP/ECA/LAC/MNA/SAR/SSA), `box`, heading `path`, `pages`, `kind`. |
| Contextual embeddings | The heading path is embedded in front of the text: `[Chapter 2: Regional Outlooks > South Asia > Outlook] Growth in SAR is expected to soften to 6.3 percent...`. The passage says "SAR", the heading says "South Asia". |
| Query routing | One Flash-Lite JSON call turns the question into filters `{region, chapter, executive_summary}`. It knows the World Bank grouping (Pakistan and Afghanistan are in **MNA**, not South Asia). A chapter filter is allowed **only** when the question names a chapter: a wrong filter hides the answer. If routing fails, search runs unfiltered. |
| Small-to-big retrieval | Search the children inside the filter, then hand the model their parent passages (top 4 distinct parents). |
| Grounded answer | Passages are labelled with their heading path; the prompt forbids borrowing numbers across regions or chapters, requires `[n]` citations, and a fixed refusal. |

Index: 702 child chunks (616 text, 46 figure, 40 table) in 378 parents, 143 distinct section paths, `gemini-embedding-001`.

## Results

Each question was answered twice from the same index:
- **Structured:** routing, filters and parent passages.
- **Flat:** no filters; answers come from the top child chunks.

| Question | Router filter | Structured | Flat |
|---|---|---|---|
| Growth outlook for South Asia *(sample)* | region = SAR | ✅ 6.3% in 2026, ~7% a year in 2027-28 | ✅ Same, plus "excluding India" figures (correctly labelled) |
| What Chapter 3 says about sovereign debt *(sample)* | chapter = 3 | ✅ Debt raises spreads/yields, non-linear, ~110 / 30 bp since 2010, policy priorities | ✅ |
| Main risks per the executive summary *(sample)* | executive_summary | ✅ Escalation of hostilities, commodity disruptions, financial stress (growth could fall to 1.3%), trade uncertainty, weather; AI upside | ❌ Refused: retrieved Chapter 1 passages instead |
| Growth outlook for **Pakistan** | region = MNA | ⚠️ Right numbers (3.0 / 3.2 / 3.7), but years shifted by one | ⚠️ Same shift; also pulled South Asia passages |
| East Asia and Pacific in 2026 | region = EAP | ✅ 4.2% (from 5% in 2025) | ✅ |
| Sub-Saharan Africa growth in 2025 | region = SSA | ✅ Firmed to 4.1% | ✅ |
| Chapter 4 on fiscal rules | chapter = 4 | ✅ | ✅ |
| Brazil's growth in 2031 *(out of scope)* | region = LAC | ✅ Refused | ✅ Refused |

**Structured: 7/8 correct, 1 partial. Flat: 6/8 correct, 1 partial, 1 miss.** All 3 sample questions pass with the structured pipeline. Full answers and retrieved sections are in `results.md`.

### Reading these results honestly

- **Flat is a strong baseline here.** It uses the same section-aware chunks and heading-prefixed embeddings; only the routing and parent passages are removed. That is why it gets South Asia right: the heading "South Asia" is inside every chunk's embedding. Basic RAG with plain page chunks gave a wrong South Asia number in the field guide.
- **Routing mattered most for "according to the executive summary".** The same risks are repeated in Chapter 1, which ranks higher on similarity. Only the filter puts the answer in front of the model.
- **Pakistan: routing worked, table reading did not.** Table 2.8 has *two* fiscal-year header rows (one starting 2023/24, one starting 2022/23). Pakistan's row (-0.2, 2.6, 3.1, 3.0, 3.2, 3.7) follows the second one, so the correct forecasts are **3.0% (2025/26e), 3.2% (2026/27f) and 3.7% (2027/28f)**. Flattened to text, the model paired the row with the first header. Fixing this needs real table structure (which header row belongs to which country), which is the Task 3 problem.
- 8 questions is a sanity check, not a benchmark.

## Run

```
..\.venv\Scripts\python rag.py parse     # structure stats only, no API calls
..\.venv\Scripts\python rag.py index     # ~10 min on the free embedding tier
..\.venv\Scripts\python rag.py ask "What is the growth outlook for South Asia?"
..\.venv\Scripts\python rag.py eval      # writes results.md
```
