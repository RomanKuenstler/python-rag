from __future__ import annotations
from .js_bridge import export_accessor
_MODULE = "shared/src/model-clients.js"
def __getattr__(name: str):
    return export_accessor(_MODULE, name)
