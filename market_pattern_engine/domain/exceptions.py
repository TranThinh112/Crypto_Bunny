from __future__ import annotations


class MarketPatternEngineError(Exception):
    pass


class InvalidOHLCVError(MarketPatternEngineError):
    pass


class InsufficientCandleError(MarketPatternEngineError):
    pass


class UnsupportedTimeframeError(MarketPatternEngineError):
    pass


class DetectorExecutionError(MarketPatternEngineError):
    pass


class ConfigurationError(MarketPatternEngineError):
    pass


class SnapshotNotFoundError(MarketPatternEngineError):
    pass


class DataQualityError(MarketPatternEngineError):
    pass


class MongoRepositoryError(MarketPatternEngineError):
    pass
