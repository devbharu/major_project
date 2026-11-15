"""ChromaDB (local vector DB): per-repo code index + per-repo team learnings."""
import hashlib
import logging
import posixpath
import re

import chromadb
from chromadb import Documents, EmbeddingFunction, Embeddings
from chromadb.utils import embedding_functions
from langchain_text_splitters import Language, RecursiveCharacterTextSplitter

from ..config import settings

log = logging.getLogger("warren.vectors")
DIM = 384


class HashEmbedding(EmbeddingFunction[Documents]):
    """Offline fallback: hashed bag-of-words. Keeps the app working when no embedding model is reachable."""

    def __init__(self):
        pass

    def __call__(self, input: Documents) -> Embeddings:
        out = []
        for doc in input:
            vec = [0.0] * DIM
            for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]+", doc.lower()):
                vec[int(hashlib.md5(tok.encode(), usedforsecurity=False).hexdigest(), 16) % DIM] += 1.0
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / norm for v in vec])
        return out

    @staticmethod
    def name() -> str:
        return "warren-hash"

    def get_config(self) -> dict:
        return {}

    @staticmethod
    def build_from_config(config: dict) -> "HashEmbedding":
        return HashEmbedding()


def _build_embedding() -> tuple[str, EmbeddingFunction]:
    provider = settings.embeddings_provider
    try:
        if provider == "omniroute":
            ef = embedding_functions.OpenAIEmbeddingFunction(
                api_key=settings.llm_api_key, api_base=settings.llm_base_url,
                model_name=settings.embedding_model or "text-embedding-3-small")
        elif provider == "hash":
            return "hash", HashEmbedding()
        else:
            provider, ef = "default", embedding_functions.DefaultEmbeddingFunction()
        ef(["probe"])  # fail fast (e.g. model download blocked) and fall back
        return provider, ef
    except Exception as exc:
        log.warning("embedding provider %r unavailable (%s) - falling back to offline hash embeddings", provider, exc)
        return "hash", HashEmbedding()


LANGS = {".py": Language.PYTHON, ".js": Language.JS, ".jsx": Language.JS, ".ts": Language.TS, ".tsx": Language.TS, ".go": Language.GO,
         ".java": Language.JAVA, ".rb": Language.RUBY, ".rs": Language.RUST, ".php": Language.PHP, ".cs": Language.CSHARP,
         ".c": Language.C, ".cpp": Language.CPP, ".kt": Language.KOTLIN, ".swift": Language.SWIFT}


def chunk(text: str, path: str = ""):
    """Split on code structure (functions/classes) with LangChain's language-aware splitter; yield (start_line, end_line, text)."""
    lang = LANGS.get(posixpath.splitext(path)[1])
    splitter = (RecursiveCharacterTextSplitter.from_language(lang, chunk_size=1400, chunk_overlap=150) if lang
                else RecursiveCharacterTextSplitter(chunk_size=1400, chunk_overlap=150))
    cursor = 0
    for piece in splitter.split_text(text):
        idx = text.find(piece, max(cursor - 300, 0))
        idx = idx if idx >= 0 else cursor
        cursor = idx + len(piece)
        start = text.count("\n", 0, idx) + 1
        yield start, start + piece.count("\n"), piece


class VectorStore:
    """Embedded, local ChromaDB only. Security notes: never start a Chroma server from this app, never pass trust_remote_code or
    load embedding models from untrusted repositories (the open advisories against chromadb concern those server/remote-code paths)."""

    def __init__(self, path: str):
        self.client = chromadb.PersistentClient(path=path)
        self.provider, self.ef = _build_embedding()

    def _col(self, kind: str, repo_id: int):
        return self.client.get_or_create_collection(f"{kind}_{repo_id}_{self.provider}", embedding_function=self.ef)

    # ---- code index ----
    def index_file(self, repo_id: int, path: str, blob_sha: str, text: str) -> bool:
        """Index one file; skip if this exact blob is already indexed. Returns True when (re)indexed."""
        col = self._col("code", repo_id)
        existing = col.get(where={"path": path}, limit=1)
        if existing["metadatas"] and existing["metadatas"][0].get("blob_sha") == blob_sha:
            return False
        if existing["ids"]:
            col.delete(where={"path": path})
        docs, metas, ids = [], [], []
        for i, (start, end, body) in enumerate(chunk(text, path)):
            docs.append(f"# {path}\n{body}")
            metas.append({"path": path, "start_line": start, "end_line": end, "blob_sha": blob_sha})
            ids.append(f"{path}:{i}")
        if docs:
            col.add(documents=docs, metadatas=metas, ids=ids)
        return True

    def search_code(self, repo_id: int, query: str, k: int = 5) -> list[dict]:
        col = self._col("code", repo_id)
        if col.count() == 0:
            return []
        res = col.query(query_texts=[query], n_results=min(k, col.count()))
        return [{"path": m["path"], "start_line": m["start_line"], "end_line": m["end_line"], "text": d}
                for d, m in zip(res["documents"][0], res["metadatas"][0], strict=True)]

    # ---- team learnings ----
    def add_learning(self, repo_id: int, learning_id: int, text: str, path_hint: str = ""):
        self._col("learn", repo_id).upsert(ids=[str(learning_id)], documents=[text], metadatas=[{"path_hint": path_hint or ""}])

    def search_learnings(self, repo_id: int, query: str, k: int = 5) -> list[str]:
        col = self._col("learn", repo_id)
        if col.count() == 0:
            return []
        res = col.query(query_texts=[query[:2000]], n_results=min(k, col.count()))
        return res["documents"][0]

    def drop_learning(self, repo_id: int, learning_id: int):
        self._col("learn", repo_id).delete(ids=[str(learning_id)])
