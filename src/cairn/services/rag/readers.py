"""Document readers — dispatches sources to the right parser.

Supported: PDF, HTML/URL, Markdown, RST, OpenAPI, JSON, YAML, CSV, Notebooks.
Adapted from multimodel-rag.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import List
from urllib.parse import urlparse

import yaml
from llama_index.core import Document


async def read_source(source: str) -> tuple[List[Document], str]:
    """Returns (documents, source_type)."""
    if source.startswith("http://") or source.startswith("https://"):
        docs = await _read_url(source)
        return docs, "html"

    path = Path(source)
    ext = path.suffix.lower()

    if ext == ".pdf":
        return [], "pdf"
    if ext in (".html", ".htm"):
        docs = await _read_html_file(source)
        return docs, "html"
    if ext in (".md", ".mdx"):
        docs = await _read_text_file(source, source_type="markdown")
        return docs, "markdown"
    if ext == ".rst":
        docs = await _read_rst(source)
        return docs, "rst"
    if ext in (".json", ".yaml", ".yml"):
        docs, source_type = await _route_structured(source, ext)
        return docs, source_type
    if ext == ".csv":
        docs = await _read_csv(source)
        return docs, "csv"
    if ext == ".ipynb":
        docs = await _read_notebook(source)
        return docs, "notebook"

    docs = await _read_text_file(source, source_type="text")
    return docs, "text"


async def _read_url(url: str) -> List[Document]:
    import trafilatura

    downloaded = await asyncio.to_thread(trafilatura.fetch_url, url)
    if downloaded is None:
        return []
    text = trafilatura.extract(
        downloaded, include_tables=True, include_links=False,
        include_comments=False, no_fallback=False,
    )
    if not text:
        return []
    meta = trafilatura.extract_metadata(downloaded)
    return [Document(
        text=text,
        metadata={
            "source_type": "html", "source_url": url,
            "domain": urlparse(url).netloc,
            "title": meta.title if meta else "",
            "description": meta.description if meta else "",
        },
    )]


async def _read_html_file(path: str) -> List[Document]:
    import trafilatura

    content = Path(path).read_text(encoding="utf-8", errors="ignore")
    text = trafilatura.extract(content, include_tables=True)
    if not text:
        text = re.sub(r"<[^>]+>", " ", content)
        text = re.sub(r"\s+", " ", text).strip()
    return [Document(text=text, metadata={"source_type": "html", "source": path})]


async def _read_text_file(path: str, source_type: str = "text") -> List[Document]:
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    return [Document(text=text, metadata={"source_type": source_type, "source": path})]


async def _read_rst(path: str) -> List[Document]:
    raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    text = re.sub(r"\.\. \w+::.*?\n(\s+.*?\n)*", "", raw)
    text = re.sub(r":\w+:`([^`]*)`", r"\1", text)
    text = re.sub(r"^[=\-~^\"#*+]{4,}\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return [Document(text=text, metadata={"source_type": "rst", "source": path})]


async def _read_openapi(path: str) -> List[Document]:
    raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    try:
        spec = json.loads(raw) if path.endswith(".json") else yaml.safe_load(raw)
    except Exception:
        return [Document(text=raw, metadata={"source_type": "text", "source": path})]

    if not isinstance(spec, dict) or "paths" not in spec:
        return [Document(text=raw, metadata={"source_type": "text", "source": path})]

    info = spec.get("info", {})
    api_title = info.get("title", "API")
    docs: List[Document] = []

    for endpoint_path, methods in spec.get("paths", {}).items():
        for method, details in methods.items():
            if method.upper() not in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"}:
                continue
            lines = [
                f"API: {api_title}",
                f"Endpoint: {method.upper()} {endpoint_path}",
                f"Summary: {details.get('summary', '')}",
                f"Description: {details.get('description', '')}",
            ]
            params = details.get("parameters", [])
            if params:
                lines.append("Parameters:")
                for p in params:
                    req = "required" if p.get("required") else "optional"
                    lines.append(f"  {p.get('name')} ({p.get('in', '?')}, {req}): {p.get('description', '')}")
            body = details.get("requestBody", {})
            if body:
                lines.append(f"Request body: {body.get('description', 'see schema')}")
            for code, resp in list(details.get("responses", {}).items())[:4]:
                lines.append(f"Response {code}: {resp.get('description', '')}")
            docs.append(Document(
                text="\n".join(lines),
                metadata={
                    "source_type": "openapi", "source": path,
                    "chunk_type": "code",
                    "endpoint": f"{method.upper()} {endpoint_path}",
                },
            ))
    return docs


async def _route_structured(path: str, ext: str) -> tuple[List[Document], str]:
    raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    try:
        spec = json.loads(raw) if ext == ".json" else yaml.safe_load(raw)
    except Exception:
        return [Document(text=raw, metadata={"source_type": "text", "source": path})], "text"

    if isinstance(spec, dict) and "paths" in spec:
        return await _read_openapi(path), "openapi"
    if ext == ".json":
        return _read_generic_json(spec, path), "json"
    return _read_generic_yaml(spec, path), "yaml"


def _read_generic_json(data, path: str) -> List[Document]:
    docs: List[Document] = []
    if isinstance(data, dict):
        for key, value in data.items():
            docs.append(Document(
                text=f"{key}:\n{_flatten_value(value)}",
                metadata={"source_type": "json", "source": path, "chunk_type": "json", "header_path": str(key)},
            ))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            docs.append(Document(
                text=_flatten_value(item),
                metadata={"source_type": "json", "source": path, "chunk_type": "json", "header_path": f"item_{i}"},
            ))
    else:
        docs.append(Document(text=str(data), metadata={"source_type": "json", "source": path, "chunk_type": "json"}))
    return docs


def _read_generic_yaml(data, path: str) -> List[Document]:
    docs: List[Document] = []
    if isinstance(data, dict):
        for key, value in data.items():
            docs.append(Document(
                text=f"{key}:\n{_flatten_value(value)}",
                metadata={"source_type": "yaml", "source": path, "chunk_type": "yaml", "header_path": str(key)},
            ))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            docs.append(Document(
                text=_flatten_value(item),
                metadata={"source_type": "yaml", "source": path, "chunk_type": "yaml", "header_path": f"item_{i}"},
            ))
    else:
        docs.append(Document(text=str(data), metadata={"source_type": "yaml", "source": path, "chunk_type": "yaml"}))
    return docs


def _flatten_value(value, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(value, dict):
        return "\n".join(f"{pad}{k}: {_flatten_value(v, indent + 1)}" for k, v in value.items())
    if isinstance(value, list):
        return "\n".join(f"{pad}- {_flatten_value(item, indent + 1)}" for item in value)
    return str(value)


async def _read_csv(path: str) -> List[Document]:
    import csv
    with open(path, encoding="utf-8", errors="ignore", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    header, data_rows = rows[0], rows[1:]
    docs: List[Document] = []
    for start in range(0, max(len(data_rows), 1), 50):
        group = data_rows[start: start + 50]
        sep = "| " + " | ".join(["---"] * len(header)) + " |"
        body = "\n".join("| " + " | ".join(r) + " |" for r in group)
        text = "| " + " | ".join(header) + " |\n" + sep + "\n" + body
        docs.append(Document(
            text=text,
            metadata={
                "source_type": "csv", "source": path,
                "chunk_type": "table",
                "page_label": f"rows {start + 1}-{start + len(group)}",
            },
        ))
    return docs


async def _read_notebook(path: str) -> List[Document]:
    import json as _json
    try:
        nb = _json.loads(Path(path).read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return [Document(text=Path(path).read_text(), metadata={"source_type": "notebook", "source": path})]
    docs: List[Document] = []
    for i, cell in enumerate(nb.get("cells", [])):
        source = "".join(cell.get("source", []))
        if not source.strip():
            continue
        chunk_type = "code" if cell.get("cell_type") == "code" else "text"
        docs.append(Document(
            text=source,
            metadata={
                "source_type": "notebook", "source": path,
                "chunk_type": chunk_type, "page_label": f"cell_{i + 1}",
            },
        ))
    return docs
