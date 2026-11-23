"""Select optional native helpers once; persistence and synchronization stay in Python."""

import importlib
import logging
import os

mode = os.environ.get("PYORM_NATIVE", "auto")
if mode not in ("auto", "off", "required"):
    raise ValueError("PYORM_NATIVE must be auto, off, or required")

native = None
if mode != "off":
    try:
        native = importlib.import_module("pyorm._native")
    except ModuleNotFoundError as exc:
        if exc.name != "pyorm._native" or mode == "required":
            raise
    except ImportError:
        if mode == "required":
            raise
        logging.getLogger(__name__).warning(
            "Native acceleration could not load; using Python helpers", exc_info=True
        )

backend_name = "rust" if native is not None else "python"
