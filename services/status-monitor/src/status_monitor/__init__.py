"""Status monitoring service for FreeInference models.

Periodically sends a small synthetic ("dummy") chat request to every model in
the registry through the FreeInference gateway and serves a status dashboard
plus JSON endpoints describing the latest probe results.
"""

__version__ = "0.1.0"
