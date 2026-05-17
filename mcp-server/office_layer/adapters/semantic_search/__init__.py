"""Semantic search adapters — sqlite-vec / lancedb / disabled.

Disabled-by-default per spec §17.6 — vector embeddings require either an
embedding model (heavy ML) or a remote API call (privacy concern). Users opt
in explicitly via ``[vec-sqlite]`` or ``[vec-lancedb]`` extras.
"""
