class QueryError(Exception):
    """Base class for controlled query-time failures (NFR-04)."""


class ConfigurationError(QueryError):
    pass


class InvalidQueryError(QueryError):
    pass


class EmbeddingError(QueryError):
    pass


class RetrievalError(QueryError):
    pass


class GenerationError(QueryError):
    pass


class ConversationNotFoundError(QueryError):
    pass
