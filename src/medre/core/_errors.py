"""Exception types shared by storage and the adapter contract."""


class StorageError(Exception):
    """Base exception for all storage-related errors."""


class DuplicateEventError(StorageError):
    """A canonical event with the requested event ID is already stored."""
