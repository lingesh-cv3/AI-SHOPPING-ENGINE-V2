"""Standalone fake merchant backend. Shares NO code with the engine.

Its job is to be *unlike* the CV3 contract: different field names, string
error codes, missing capabilities. If this ever imports from `shared/`, the
adapter abstraction has stopped being tested.
"""