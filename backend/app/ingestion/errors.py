class IngestionError(Exception):
    """Base class for controlled ingestion failures (NFR-04)."""


class RegistryError(IngestionError):
    pass


class FetchError(IngestionError):
    pass


class ParseError(IngestionError):
    pass


class QualityGateError(IngestionError):
    pass


class EmbeddingError(IngestionError):
    pass
