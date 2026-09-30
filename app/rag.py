"""Retrieval-augmented Q&A over the MDS documents.

Retrieval is BM25 over ~900-character chunks, each tagged with its source
file and page. Answers come from one of two modes (config.RAG_MODE):

  local  - extractive: the best-matching sentences, quoted with page refs.
           Nothing leaves the server.
  claude - the top excerpts are sent to the Claude API, which writes the
           answer from them only and cites pages.
"""

import hashlib
import json
import logging
import math
import re
import threading
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config

log = logging.getLogger("mds.rag")

CHUNK_CHARS = 900
CHUNK_OVERLAP = 200
TOP_K = 6

_STOP = set(
    """a an and are as at be by can for from has have how in is it its of on or that the this to was
    were what when where which who why will with do does did many much any all into i me my you your
    we our they their there than then so if not no yes about""".split()
)
# Map words to the forms the handout uses, e.g. "first line" -> "1st line".
_NORMALISE = {
    "first": "1st", "second": "2nd", "third": "3rd", "authorised": "auth", "authorized": "auth",
    "authorisation": "auth", "authorization": "auth", "minefield": "mfd", "minefields": "mfd",
    "hrs": "hour", "hr": "hour", "mins": "minute", "min": "minute", "vehicle": "veh", "vehicles": "veh",
    "atm": "at", "apm": "ap",
}
_HEADER = re.compile(r"^\s*(\d+\s*)?RESTRICTED\s*$", re.M)


def tokenize(text: str) -> list[str]:
    text = text.lower()
    text = re.sub(r"self\s*-?\s*destruct\w*", " sd ", text)
    text = re.sub(r"anti\s*-?\s*tank", " at ", text)
    text = re.sub(r"anti\s*-?\s*personnel", " ap ", text)
    out = []
    for w in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text):
        if w in _STOP:
            continue
        w = _NORMALISE.get(w, w)
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


@dataclass
class Chunk:
    doc: str
    page: int
    text: str


def extract_pages(pdf: Path) -> list[str]:
    from pypdf import PdfReader

    pages = []
    for page in PdfReader(str(pdf)).pages:
        text = page.extract_text() or ""
        text = _HEADER.sub("", text)
        text = re.sub(r"^\s*\d+\s*$", "", text, count=1, flags=re.M)  # page number line
        pages.append(re.sub(r"[ \t]+", " ", text).strip())
    return pages


def chunk_pages(doc: str, pages: list[str]) -> list[Chunk]:
    chunks = []
    for i, text in enumerate(pages, start=1):
        if not text:
            continue
        pos = 0
        while pos < len(text):
            end = min(len(text), pos + CHUNK_CHARS)
            if end < len(text):  # break on a line or sentence boundary
                cut = max(text.rfind("\n", pos + CHUNK_CHARS // 2, end), text.rfind(". ", pos + CHUNK_CHARS // 2, end))
                if cut > pos:
                    end = cut + 1
            chunks.append(Chunk(doc, i, text[pos:end].strip()))
            if end >= len(text):
                break
            pos = max(end - CHUNK_OVERLAP, pos + 1)
    return chunks


class Index:
    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self.tokens = [tokenize(c.text) for c in chunks]
        self.df = Counter(t for toks in self.tokens for t in set(toks))
        self.avgdl = sum(map(len, self.tokens)) / max(1, len(self.tokens))
        self.tf = [Counter(t) for t in self.tokens]

    def search(self, query: str, k: int = TOP_K) -> list[tuple[float, Chunk]]:
        q = tokenize(query)
        n = len(self.chunks)
        k1, b = 1.5, 0.75
        scored = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            dl = len(self.tokens[i])
            for t in set(q):
                f = tf.get(t)
                if not f:
                    continue
                idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / self.avgdl))
            # Reward adjacent query-term pairs ("1st line"), which BM25 ignores.
            toks = self.tokens[i]
            pairs = set(zip(q, q[1:]))
            s += 1.5 * sum(1 for p in zip(toks, toks[1:]) if p in pairs)
            if s > 0:
                scored.append((s, self.chunks[i]))
        scored.sort(key=lambda x: -x[0])
        return scored[:k]


# ------------------------------------------------------------ index cache

_lock = threading.Lock()
_index: Index | None = None


def _doc_paths() -> list[Path]:
    paths = [p for p in config.BUILTIN_DOCS if p.exists()]
    paths += sorted(config.DOC_UPLOAD_DIR.glob("*.pdf")) if config.DOC_UPLOAD_DIR.exists() else []
    return paths


def _fingerprint(p: Path) -> str:
    st = p.stat()
    return hashlib.sha1(f"{p.name}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()


def build_index() -> Index:
    """(Re)build the index, reusing cached page text for unchanged PDFs."""
    global _index
    cache_file = config.DATA_DIR / "rag_cache.json"
    try:
        cache = json.loads(cache_file.read_text())
    except (OSError, ValueError):
        cache = {}
    chunks: list[Chunk] = []
    new_cache = {}
    for p in _doc_paths():
        fp = _fingerprint(p)
        pages = cache.get(fp)
        if pages is None:
            log.info("Extracting text from %s", p.name)
            pages = extract_pages(p)
        new_cache[fp] = pages
        chunks += chunk_pages(p.name, pages)
    try:
        cache_file.write_text(json.dumps(new_cache))
    except OSError:
        log.warning("Could not write RAG cache")
    with _lock:
        _index = Index(chunks)
    log.info("RAG index: %d chunks from %d document(s)", len(chunks), len(new_cache))
    return _index


def get_index() -> Index:
    with _lock:
        idx = _index
    return idx or build_index()


def documents() -> list[dict]:
    idx = get_index()
    counts = Counter(c.doc for c in idx.chunks)
    return [{"name": p.name, "chunks": counts.get(p.name, 0), "uploaded": p.parent == config.DOC_UPLOAD_DIR}
            for p in _doc_paths()]


# ------------------------------------------------------------------ answer


def answer(question: str, history: list[dict] | None = None) -> dict:
    question = question.strip()
    if not question:
        return {"answer": "Please type a question.", "sources": [], "mode": config.RAG_MODE}
    hits = get_index().search(question)
    sources = [{"doc": c.doc, "page": c.page, "excerpt": c.text[:400], "score": round(s, 2)} for s, c in hits]
    if not hits:
        return {"answer": "I couldn't find anything about that in the MDS documents.", "sources": [], "mode": config.RAG_MODE}
    if config.RAG_MODE == "claude":
        try:
            return {"answer": _claude_answer(question, hits, history or []), "sources": sources, "mode": "claude"}
        except Exception as e:
            log.warning("Claude answer failed, falling back to extractive: %s", e)
    return {"answer": _extractive(question, hits), "sources": sources, "mode": "local"}


def _extractive(question: str, hits: list[tuple[float, Chunk]]) -> str:
    """Pick the sentences/lines that share the most query terms."""
    q = set(tokenize(question))
    candidates = []
    for rank, (_, c) in enumerate(hits[:4]):
        # PDF lines wrap mid-sentence; re-join them unless the next line
        # starts a new list item such as "(4)", "b." or "5.".
        text = re.sub(r"\s*\n(?!\s*(?:\(\w{1,4}\)|[a-z]\.|\d{1,2}\.)\s)", " ", c.text)
        for sent in re.split(r"(?<=[.;])\s+(?=[A-Z(])|\n", text):
            sent = sent.strip()[:400]
            if len(sent) < 12:
                continue
            toks = tokenize(sent)
            overlap = len(q & set(toks))
            if overlap:
                candidates.append((overlap - 0.1 * rank, sent, c))
    if not candidates:
        _, c = hits[0]
        return f"{c.text[:500].strip()}\n\n(Source: {c.doc}, p. {c.page})"
    candidates.sort(key=lambda x: -x[0])
    lines, seen = [], set()
    for _, sent, c in candidates:
        key = sent[:60]  # overlapping chunks repeat the same sentence
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"• {sent} (p. {c.page})")
        if len(lines) == 3:
            break
    return "\n".join(lines)


SYSTEM_PROMPT = """You are the MDS assistant for engineer officers operating the GBL-131 Mine \
Dispensing System (MDS). Answer only from the handout excerpts provided in the user's message. \
Cite page numbers like (p. 85). If the excerpts don't contain the answer, say so plainly instead \
of guessing, and suggest what to look up. Keep answers short and precise: give figures exactly \
as the handout states them."""


def _claude_answer(question: str, hits: list[tuple[float, Chunk]], history: list[dict]) -> str:
    import anthropic

    client = anthropic.Anthropic()
    excerpts = "\n\n".join(f'<excerpt doc="{c.doc}" page="{c.page}">\n{c.text}\n</excerpt>' for _, c in hits)
    messages = []
    for turn in history[-6:]:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": str(turn["content"])[:2000]})
    # History must start with a user turn and alternate.
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    if messages and messages[-1]["role"] == "user":
        messages.pop()
    messages.append({"role": "user", "content": f"{excerpts}\n\nQuestion: {question}"})

    response = client.beta.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=messages,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to answer")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise RuntimeError("Empty answer")
    return text
