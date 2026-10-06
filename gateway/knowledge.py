"""Runbook knowledge base (Stage 7): shared + per-namespace runbooks, retrieved
by meaning and filtered by namespace.

Isolation model
---------------
Every chunk carries a `namespace` payload field:
    "*"         shared runbooks, owned by the platform team (baked into the image)
    "<ns>"      team runbooks, written ONLY through /v1/runbooks/sync, where the
                namespace is stamped from the caller's verified token identity.
Every search filters `namespace IN ("*", <caller ns>)` inside the vector
store, so a forbidden chunk is never returned at all.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from kubelantern_common.redaction import redact

log = logging.getLogger("kubelantern.knowledge")

SHARED = "*"
MAX_RUNBOOKS_PER_NAMESPACE = 50
MAX_CONTENT_BYTES = 16 * 1024
MAX_TITLE = 200
CHUNK_CHARS = 1200
MIN_SCORE = 0.35
_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
_UUID_NS = uuid.UUID("6f1d2a3c-5b4e-4f8a-9c7d-1e2f3a4b5c6d")


class KnowledgeError(Exception):
    pass


# -- runbook actions (commands + owner), extracted from the whole runbook ------------

_COMMAND = re.compile(r"^\s*((?:kubectl|helm|az|aws|gcloud|oc|kustomize|argocd|flux)\s.+?)\s*$",
                      re.MULTILINE)
_CONTACT = re.compile(r"^\s*#*\s*(?:owner|escalat\w*|contact|on-?call)\b[ \t]*[:—-]?[ \t]*(.*)$",
                      re.IGNORECASE | re.MULTILINE)


def extract_actions(text: str) -> tuple[list[str], str | None]:
    """Commands without placeholders, and the owner/escalation line."""
    commands: list[str] = []
    for m in _COMMAND.finditer(text or ""):
        cmd = m.group(1).strip().rstrip(".")
        if "<" not in cmd and cmd not in commands:
            commands.append(cmd)
    escalation = None
    c = _CONTACT.search(text or "")
    if c:
        line = c.group(1).strip()
        if not line:  # heading form: "# Owner" with the contact on the next line
            after = [ln.strip() for ln in text[c.end():].splitlines() if ln.strip()]
            line = after[0] if after else ""
        escalation = line[:200] or None
    return commands[:5], escalation


# -- runbooks ---------------------------------------------------------------------

@dataclass
class Runbook:
    namespace: str
    name: str
    title: str
    content: str
    category: str | None = None
    workloads: list[str] = field(default_factory=list)

    @property
    def source(self) -> str:
        return f"{'shared' if self.namespace == SHARED else self.namespace}/{self.name}"


def parse_markdown_runbook(path: Path) -> Runbook:
    """Shared runbooks: Markdown with a small `key: value` front matter block."""
    text = path.read_text()
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        _, front, body = text.split("---", 2)
        for line in front.strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"')
    return Runbook(
        namespace=SHARED,
        name=path.stem,
        title=meta.get("title", path.stem),
        content=body.strip(),
        category=meta.get("category") or None,
        workloads=[w.strip() for w in meta.get("workloads", "").split(",") if w.strip()],
    )


def chunk_runbook(rb: Runbook, size: int = CHUNK_CHARS) -> list[str]:
    """Split by Markdown headings, then by size. Each chunk is prefixed with the
    title so it stays meaningful on its own."""
    sections = re.split(r"(?m)^(?=#{1,3} )", rb.content)
    chunks: list[str] = []
    for sec in (s.strip() for s in sections):
        if not sec:
            continue
        while len(sec) > size:
            cut = sec.rfind("\n", 0, size)
            cut = cut if cut > size // 2 else size
            chunks.append(sec[:cut].strip())
            sec = sec[cut:].strip()
        if sec:
            chunks.append(sec)
    return [f"{rb.title}\n\n{c}" for c in chunks] or [rb.title]


def validate_team_runbooks(items: object) -> list[dict]:
    """Validate the sync payload from an agent. Namespace is NOT read from it."""
    if not isinstance(items, list):
        raise KnowledgeError("runbooks must be a list")
    if len(items) > MAX_RUNBOOKS_PER_NAMESPACE:
        raise KnowledgeError(f"at most {MAX_RUNBOOKS_PER_NAMESPACE} runbooks per namespace")
    seen, out = set(), []
    for it in items:
        if not isinstance(it, dict):
            raise KnowledgeError("each runbook must be an object")
        name, title, content = it.get("name"), it.get("title"), it.get("content")
        if not isinstance(name, str) or not _NAME.match(name):
            raise KnowledgeError(f"invalid runbook name: {name!r}")
        if name in seen:
            raise KnowledgeError(f"duplicate runbook name: {name}")
        seen.add(name)
        if not isinstance(title, str) or not (3 <= len(title) <= MAX_TITLE):
            raise KnowledgeError(f"{name}: title must be 3..{MAX_TITLE} chars")
        if not isinstance(content, str) or len(content.encode()) > MAX_CONTENT_BYTES:
            raise KnowledgeError(f"{name}: content must be a string of at most {MAX_CONTENT_BYTES} bytes")
        workloads = it.get("workloads") or []
        if not isinstance(workloads, list) or not all(isinstance(w, str) for w in workloads):
            raise KnowledgeError(f"{name}: workloads must be a list of strings")
        out.append({"name": name, "title": title, "content": content,
                    "category": it.get("category") if isinstance(it.get("category"), str) else None,
                    "workloads": workloads[:20]})
    return out


# -- embeddings ------------------------------------------------------------------------

class Embedder(Protocol):
    def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        """kind: 'document' or 'query'."""


class OllamaEmbedder:
    """nomic-embed-text via Ollama. Uses the task prefixes the model expects."""

    def __init__(self, base_url: str, model: str = "nomic-embed-text", timeout: float = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(self.base_url + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read() or b"{}")

    def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        prefix = "search_query: " if kind == "query" else "search_document: "
        inputs = [prefix + t for t in texts]
        try:
            out = self._post("/api/embed", {"model": self.model, "input": inputs})
            vecs = out.get("embeddings")
            if vecs:
                return vecs
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        # older Ollama: one prompt per call
        return [self._post("/api/embeddings", {"model": self.model, "prompt": t})["embedding"]
                for t in inputs]


# -- vector stores ---------------------------------------------------------------------------

@dataclass
class Hit:
    score: float
    payload: dict


class VectorStore(Protocol):
    def ensure(self, dim: int) -> None: ...
    def replace_namespace(self, namespace: str, points: list[tuple[str, list[float], dict]]) -> None: ...
    def search(self, vector: list[float], namespaces: list[str], limit: int) -> list[Hit]: ...


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class InMemoryStore:
    """Reference implementation with the same filter semantics as Qdrant."""

    def __init__(self) -> None:
        self.points: dict[str, tuple[list[float], dict]] = {}

    def ensure(self, dim: int) -> None:
        pass

    def replace_namespace(self, namespace, points) -> None:
        self.points = {k: v for k, v in self.points.items() if v[1]["namespace"] != namespace}
        for pid, vec, payload in points:
            self.points[pid] = (vec, payload)

    def search(self, vector, namespaces, limit) -> list[Hit]:
        allowed = set(namespaces)
        hits = [Hit(_cosine(vector, v), p) for v, p in self.points.values() if p["namespace"] in allowed]
        return sorted(hits, key=lambda h: h.score, reverse=True)[:limit]


class QdrantStore:
    """Qdrant over its REST API (no client library needed)."""

    def __init__(self, base_url: str, collection: str = "kubelantern-runbooks", timeout: float = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.collection = collection
        self.timeout = timeout
        self._ready = False

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(self.base_url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read() or b"{}")

    def ensure(self, dim: int) -> None:
        if self._ready:
            return
        c = f"/collections/{self.collection}"
        try:
            self._req("GET", c)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            self._req("PUT", c, {"vectors": {"size": dim, "distance": "Cosine"}})
            self._req("PUT", f"{c}/index", {"field_name": "namespace", "field_schema": "keyword"})
        self._ready = True

    def replace_namespace(self, namespace, points) -> None:
        c = f"/collections/{self.collection}"
        self._req("POST", f"{c}/points/delete?wait=true",
                  {"filter": {"must": [{"key": "namespace", "match": {"value": namespace}}]}})
        if points:
            self._req("PUT", f"{c}/points?wait=true", {"points": [
                {"id": pid, "vector": vec, "payload": payload} for pid, vec, payload in points]})

    def search(self, vector, namespaces, limit) -> list[Hit]:
        out = self._req("POST", f"/collections/{self.collection}/points/search", {
            "vector": vector, "limit": limit, "with_payload": True,
            "filter": {"must": [{"key": "namespace", "match": {"any": list(namespaces)}}]},
        })
        return [Hit(float(r.get("score", 0)), r.get("payload") or {}) for r in out.get("result", [])]

    def ready(self) -> tuple[bool, str]:
        try:
            self._req("GET", "/collections")
            return True, "ok"
        except (urllib.error.URLError, OSError, ValueError) as e:
            return False, f"qdrant unreachable: {e}"


# -- the knowledge base ---------------------------------------------------------------------------

class KnowledgeBase:
    def __init__(self, embedder: Embedder, store: VectorStore, min_score: float = MIN_SCORE,
                 relative_margin: float = 0.08) -> None:
        self.embedder = embedder
        self.store = store
        self.min_score = min_score
        self.relative_margin = relative_margin

    def _index(self, namespace: str, runbooks: list[Runbook]) -> int:
        texts, payloads = [], []
        for rb in runbooks:
            commands, escalation = extract_actions(rb.content)
            for i, chunk in enumerate(chunk_runbook(rb)):
                texts.append(chunk)
                payloads.append({
                    "namespace": namespace, "source": rb.source, "name": rb.name, "title": rb.title,
                    "category": rb.category, "workloads": rb.workloads, "chunk": i, "text": chunk,
                    "commands": commands, "escalation": escalation,
                })
        vectors = self.embedder.embed(texts, "document") if texts else []
        if vectors:
            self.store.ensure(len(vectors[0]))
        points = [(str(uuid.uuid5(_UUID_NS, f"{p['namespace']}/{p['name']}/{p['chunk']}")), v, p)
                  for v, p in zip(vectors, payloads, strict=True)]
        self.store.replace_namespace(namespace, points)
        return len(points)

    def load_shared(self, directory: str | Path) -> int:
        paths = sorted(Path(directory).glob("*.md"))
        runbooks = [parse_markdown_runbook(p) for p in paths]
        n = self._index(SHARED, runbooks)
        log.info("loaded %d shared runbooks (%d chunks)", len(runbooks), n)
        return n

    def sync_namespace(self, namespace: str, items: list[dict]) -> dict:
        """Replace a namespace's team runbooks. `namespace` MUST come from the
        caller's verified identity, never from the request."""
        if namespace == SHARED or not _NAME.match(namespace):
            raise KnowledgeError("invalid namespace")
        runbooks = [Runbook(namespace=namespace, name=it["name"], title=redact(it["title"]),
                            content=redact(it["content"]), category=it.get("category"),
                            workloads=it.get("workloads") or [])
                    for it in validate_team_runbooks(items)]
        chunks = self._index(namespace, runbooks)
        return {"namespace": namespace, "runbooks": len(runbooks), "chunks": chunks,
                "digest": hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()[:12]}

    def retrieve(self, query: str, namespace: str, category: str | None = None,
                 workload: str | None = None, limit: int = 3, strict_category: bool = False
                 ) -> list[dict]:
        """strict_category: drop runbooks tagged with a different category (used when
        the rules are confident, so off-topic runbooks don't distract the model)."""
        [vec] = self.embedder.embed([query], "query")
        hits = self.store.search(vec, [SHARED, namespace], limit * 4)
        ranked = []
        for h in hits:
            p = h.payload
            # Defence in depth: never trust the store alone for isolation.
            if p.get("namespace") not in (SHARED, namespace):
                log.error("vector store returned a chunk from namespace %s for %s — dropped",
                          p.get("namespace"), namespace)
                continue
            if p.get("workloads") and workload and workload not in p["workloads"]:
                continue
            if strict_category and category and p.get("category") and p["category"] != category:
                continue
            score = h.score
            if category and p.get("category") == category:
                score += 0.05
            if p.get("namespace") == namespace:
                score += 0.03  # prefer the team's own knowledge when equally relevant
            if score >= self.min_score:
                ranked.append((score, p))
        ranked.sort(key=lambda x: x[0], reverse=True)
        # Only pass runbooks close to the best match: loosely related "also-rans"
        # distract small models more than they help.
        if ranked:
            best = ranked[0][0]
            ranked = [(sc, p) for sc, p in ranked if sc >= best - self.relative_margin]

        out, seen = [], set()
        for score, p in ranked:
            if p["source"] in seen:  # one chunk per runbook keeps the prompt small
                continue
            seen.add(p["source"])
            out.append({"source": p["source"], "title": p["title"], "text": p["text"],
                        "category": p.get("category"), "score": round(score, 3),
                        "commands": p.get("commands"), "escalation": p.get("escalation")})
            if len(out) >= limit:
                break
        return out
