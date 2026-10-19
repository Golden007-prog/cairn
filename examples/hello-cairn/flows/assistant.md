---
name: assistant
version: 0.1.0
module: ask
tags:
  - flow:state_machine
  - domain:example
description: |
  Answer a question from the pack's own docs: retrieve from the `docs` corpus,
  re-rank, ask a model, measure the answer with a tool on the example's MCP server,
  and remember the exchange. The agentic leg of `ask-the-docs`.

flow:
  type: state_machine
  source: behavior_tree
  root:
    type: sequence
    children:
      # Retrieval: the builtin rag/search over the corpus pack.toml declares as
      # [requires.rag.docs] (sqlite-vec by default; pgvector when RAG_VECTOR_STORE
      # says so). Read-only.
      - type: operation
        operator: rag/search
        id: search
        input:
          corpus: docs
          query: $input.question
          top_k: 5
        output:
          hits: $state.findings.hits
          ok: $state.findings.search_ok
          error: $state.findings.search_error
      # Re-rank the hits (identity unless a cross-encoder or Cohere is installed).
      - type: operation
        operator: rag/rerank
        id: rerank
        input:
          query: $input.question
          hits: $default($state.findings.hits, [])
          top_k: 3
        output:
          hits: $state.findings.context
          reranker_used: $state.findings.reranker
      # The model call. The compiler builds it through core's build_chat_model, so
      # the call is redacted (when a redact.text binding is active) and costed.
      - type: action
        ref: answer
      # A tool call on the example's own stdio MCP server (tools/calc_server.py),
      # through the builtin mcp/call operator: governed and audited like any tool call.
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
          ok: $state.findings.measure_ok
          error: $state.findings.measure_error
      # Remember the exchange in a local memory store (memory/write, sqlite-vec). A
      # store name is a collection name: letters, digits and underscores only.
      - type: operation
        operator: memory/write
        id: remember
        input:
          store: hello_answers
          record:
            question: $input.question
            answer: $state.findings.answer
          embed_text: $input.question
        output:
          ok: $state.findings.remembered
          id: $state.findings.memory_id
          error: $state.findings.memory_error

  nodes:
    answer:
      type: prompt
      model: gpt-4o-mini
      capabilities: []
      prompt_section: answer

tools:
  required: []
---

# Ask the docs

## answer

You answer a question about the hello-cairn example pack. The retrieved passages
are in `state.findings.context` (each hit's `payload.content` is the text, and
`payload.source` names the file). Answer in at most three sentences, using only
what the passages say, and name the file you used. When the passages do not cover
the question, say that the docs do not cover it. Do not invent features.
