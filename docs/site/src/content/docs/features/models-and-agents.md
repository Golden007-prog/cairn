---
title: Models and agents
description: Agentic template legs, model calls through one governed factory (and a keyless stub for tests), MCP tools, retrieval and memory.
---

An agentic leg is a **template**: a markdown file whose YAML front matter
compiles to a LangGraph graph. Its `operation` nodes call operators (each one
behind the approval gate) and its `prompt` nodes call a model. Four public
pieces make an agent useful, and `ask-the-docs` uses all of them in one leg:

| Piece | Operators | What the template does with it |
|---|---|---|
| model calls | built by the engine's model factory | `answer` prompt node |
| MCP tools | `mcp/call` (`cairn-operator-mcp`) | calls `word_count` on the example's own server |
| retrieval | `rag/search`, `rag/fetch` (`cairn-operator-rag-local`), `rag/rerank` (`cairn-operator-rag-rerank`) | searches the pack's `docs` corpus, then re-ranks |
| memory | `memory/write`, `memory/search` (`cairn-operator-memory-local`) | remembers the question and answer |

## The template

```yaml
# examples/hello-cairn/flows/assistant.md (front matter, abridged)
flow:
  type: state_machine
  source: behavior_tree
  root:
    type: sequence
    children:
      - type: operation
        operator: rag/search
        id: search
        input:
          corpus: docs
          query: $input.question
          top_k: 5
        output:
          hits: $state.findings.hits
      - type: operation
        operator: rag/rerank
        id: rerank
        input:
          query: $input.question
          hits: $default($state.findings.hits, [])
          top_k: 3
        output:
          hits: $state.findings.context
      - type: action
        ref: answer
      - type: operation
        operator: mcp/call
        id: measure
        input:
          server: hello-calc
          tool: word_count
          args:
            text: $state.findings.answer
        output:
          result: $state.findings.answer_stats
      - type: operation
        operator: memory/write
        id: remember
        input:
          store: hello_answers
          record:
            question: $input.question
            answer: $state.findings.answer
          embed_text: $input.question
  nodes:
    answer:
      type: prompt
      model: gpt-4o-mini
      capabilities: []
      prompt_section: answer
```

The pipeline wires it in as one leg:

```yaml
# examples/hello-cairn/pipelines/ask-the-docs.yaml
- id: assistant
  label: Answer from the docs
  template: assistant
  with:
    question: $input.question
```

## Model calls

Every model call a template makes is built by one factory
(`build_chat_model`). That is where redaction (when `redact.text` is bound) and
the cost callback attach, so no template can reach a model around them.

Point the engine at any OpenAI-compatible endpoint:

```bash
export OPENAI_API_KEY=sk-...                 # a hosted provider
# or: OPENAI_BASE_URL=<any OpenAI-compatible endpoint>
# or: LLM_GATEWAY=proxy LLM_GATEWAY_URL=<gateway URL>   (e.g. the light stack's LiteLLM)
```

The engine has no built-in mock model. `CAIRN_BACKEND=mock` is the telemetry
backend, not a model.

### A keyless model for tests

The repository's feature proof runs against `tests/e2e/model_stub.py`: a
deterministic, keyless, stdlib-only OpenAI-compatible server
(`/v1/chat/completions`, `/v1/embeddings`, `/v1/models`). The harness starts
it on a loopback port and points each engine at it with `OPENAI_BASE_URL`, so
the calls leave the engine exactly as they would for a hosted provider. Use
the same pattern to test your own templates without a key.

## MCP tools

`mcp/call` calls a tool on a named MCP server, over stdio or streamable HTTP.
Three places name the server, each for its own reason:

```toml
# examples/hello-cairn/pack.toml — what the pack needs
[requires.mcp]
servers = ["hello-calc"]
tools   = ["hello-calc:word_count", "hello-calc:add"]
```

```toml
# examples/hello-cairn/profiles/hello.toml — what cairn doctor and the tool allow-list read
[profile.mcp_servers.hello-calc]
transport = "stdio"
command   = ["python", "examples/hello-cairn/tools/calc_server.py"]
```

```bash
# the engine's environment — where the MCP client takes the command from
export CAIRN_MCP_SERVERS='{"hello-calc": {"transport": "stdio", "command": "python",
  "args": ["examples/hello-cairn/tools/calc_server.py"]}}'
```

The engine ships connection presets for the GitHub, Slack and Notion MCP
servers: `cairn connect presets` lists them and `cairn connect add --preset
<name>` adds one.

## Retrieval (RAG)

A pack declares a corpus; the engine indexes it; `rag/search` reads it.

```toml
# examples/hello-cairn/pack.toml
[requires.rag.docs]
corpus = "rag/docs"
access = "vector_search"
```

```bash
uv sync --extra rag                     # the ingest stack (plus rag-sqlite-vec, in the hello extra)
export CAIRN_RAG_AUTOINDEX=1            # index every declared corpus at boot
export RAG_EMBED_PROVIDER=openai        # openai | gemini (the default is gemini)
```

| Setting | Values |
|---|---|
| `RAG_VECTOR_STORE` | `sqlite_vec` (default, extra `rag-sqlite-vec`) · `pgvector` (extra `rag-pgvector`, needs `PGVECTOR_DSN`) |
| `rag/rerank` | the identity reranker by default; the operator's `[cross_encoder]` and `[cohere]` extras add real ones |

There is no indexing operator: an un-indexed corpus returns no hits, and the
example's model then says the docs do not cover the question.

## Memory

`memory/write` and `memory/search` keep records in a local vector store
(sqlite-vec). A store name is a collection name: letters, digits and
underscores only (`hello_answers`). The profile binds the capabilities:

```toml
# examples/hello-cairn/profiles/hello.toml
[profile.bindings]
"memory.search"  = "memory/search"
"memory.write"   = "memory/write"
"rag.search"     = "rag/search"
"rag.fetch"      = "rag/fetch"
"mcp.call"       = "mcp/call"
```

## Limits

- The proof runs the model and embedder calls against the keyless stub, not a
  hosted model.
- The cross-encoder and Cohere rerankers are not exercised by the example.
- The `rag-qdrant` extra is not covered by the example; `rag-pinecone` is a
  stub whose every method raises.
- The MCP presets ship with the engine but the example does not use them.
