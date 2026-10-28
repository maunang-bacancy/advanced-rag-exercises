"""
Retrieval quality per extraction strategy: the rank of the first passage that
contains the evidence for each question, and MRR (1.0 = always ranked first).

Usage: python retrieval_eval.py      # appends a table to results.md
"""

import re

import numpy as np

from rag import EVAL_QUESTIONS, HERE, STRATEGIES, TOP_K, embed, load_index

# Evidence an answering passage must contain, per answerable question
EVIDENCE = [
    r"58 percent less energy",
    r"(?s)Product use.{0,60}45\s?%",
    r"100 percent recycled aluminum in the stand",
    r"Sleep.{0,40}0\.57\s?W|0\.57W",
    r"(?s)445.{0,600}99|99.{0,600}445",
    r"143W Power Adapter",
    r"(?s)76\s?(%|percent).{0,80}recycled content|recycled content.{0,40}76",
]


def rank_of(pattern, chunks, order, scores):
    for r, i in enumerate(order, start=1):
        if scores[i] == -1:
            return None       # only excluded (vision) chunks are left
        if re.search(pattern, chunks[i]["text"]):
            return r
    return None


def main():
    index = load_index()
    qvecs = embed(EVAL_QUESTIONS[:len(EVIDENCE)], "RETRIEVAL_QUERY")
    ranks = {s: [] for s in STRATEGIES}
    for qv, pattern in zip(qvecs, EVIDENCE):
        for s in STRATEGIES:
            chunks, vecs = index["plain" if s == "plain" else "structured"]
            scores = vecs @ qv
            if s == "structured":
                scores = np.where([c["kind"] != "vision" for c in chunks], scores, -1)
            ranks[s].append(rank_of(pattern, chunks, np.argsort(-scores), scores))

    def fmt(r):
        return "not found" if r is None else (f"**{r}**" if r == 1 else str(r))

    lines = ["", "## Retrieval quality (rank of the first passage holding the evidence)", "",
             "| Question | plain | structured | vision |", "|---|---|---|---|"]
    for i, q in enumerate(EVAL_QUESTIONS[:len(EVIDENCE)]):
        lines.append(f"| {q[:70]} | " + " | ".join(fmt(ranks[s][i]) for s in STRATEGIES) + " |")
    mrr = {s: np.mean([1 / r if r else 0 for r in ranks[s]]) for s in STRATEGIES}
    top1 = {s: sum(r == 1 for r in ranks[s]) for s in STRATEGIES}
    in_k = {s: sum(r is not None and r <= TOP_K for r in ranks[s]) for s in STRATEGIES}
    n = len(EVIDENCE)
    lines += [f"| **MRR** | " + " | ".join(f"**{mrr[s]:.2f}**" for s in STRATEGIES) + " |",
              f"| Ranked #1 | " + " | ".join(f"{top1[s]}/{n}" for s in STRATEGIES) + " |",
              f"| In top {TOP_K} (sent to the model) | " + " | ".join(f"{in_k[s]}/{n}" for s in STRATEGIES) + " |"]
    print("\n".join(lines))
    text = (HERE / "results.md").read_text(encoding="utf-8").split("\n## Retrieval quality")[0]
    (HERE / "results.md").write_text(text.rstrip() + "\n" + "\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
