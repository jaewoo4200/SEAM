"""Standalone engine worker scripts.

These files are executed by OTHER venvs' interpreters (the engine venv, not
the backend venv) through the file-based JSON job protocol in
services/engines.py. Keep them free of seam_studio imports and third-party
dependencies beyond what the target engine venv guarantees (numpy, sionna).

Shipping them inside the package means a pip install carries the workers too
(previously they lived only in the repo's backend/engine_workers/, so
subprocess engines silently could not run on pip installs).
"""
