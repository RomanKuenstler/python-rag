from __future__ import annotations
from .js_bridge import export_accessor
_MODULE = "shared/src/runtime-config.js"
def __getattr__(name: str):
    return export_accessor(_MODULE, name)
