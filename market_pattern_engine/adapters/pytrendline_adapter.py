from __future__ import annotations


class PyTrendlineAdapter:
    name = "pytrendline_adapter"
    detector_source = "EXTERNAL_ADAPTER"
    detector_version = "unavailable"

    def __init__(self, config):
        self.config = config

    def detect(self, market_context):
        try:
            import pytrendline  # type: ignore  # noqa: F401
        except Exception:
            return []
        return []
