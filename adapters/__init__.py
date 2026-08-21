"""Platform adapters, plugin-style - one folder per platform.

RULE: never imports from engine/. Only shared/. This is the boundary that makes
a new platform a new folder rather than a change everywhere.
"""