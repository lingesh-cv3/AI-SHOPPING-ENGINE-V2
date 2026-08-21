"""Shared contracts for the CV3 AI Shopping Assistant Engine.

The only package imported by both the engine and every adapter. Nothing here
depends on a platform, a database, an LLM provider, or a web framework — it is
pure shape. Keep it that way; the moment this package acquires a dependency,
every adapter inherits it.
"""