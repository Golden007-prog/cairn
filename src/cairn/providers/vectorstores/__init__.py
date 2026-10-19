"""Vector-store providers — external backends behind the ``cairn.vector_stores``
entry-point group (#76).

Core (``cairn.services.rag.stores``) ships only the in-built backends
``sqlite_vec`` (default) and ``pgvector``. ``qdrant`` / ``pinecone`` live here
and are resolved lazily when ``RAG_VECTOR_STORE`` selects them, with their deps
behind the ``rag-qdrant`` / ``rag-pinecone`` optional extras.
"""
