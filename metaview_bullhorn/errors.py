"""Exception types. Every failure path raises one of these so the runner can
abort loudly instead of writing a note built from partial data."""


class SyncError(Exception):
    """Base class for all sync errors."""


class ConfigError(SyncError):
    """A required setting is missing or malformed."""


class ExtractionError(SyncError):
    """A Metaview field could not be extracted. Names the field and the
    conversation so a layout change surfaces loudly."""

    def __init__(self, field: str, detail: str, conversation: str | None = None):
        self.field = field
        self.detail = detail
        self.conversation = conversation
        where = f" (conversation {conversation})" if conversation else ""
        super().__init__(f"Metaview extraction failed for field '{field}'{where}: {detail}")


class BullhornError(SyncError):
    """Bullhorn authentication or REST failure."""


class NoteValidationError(SyncError):
    """The drafted note breaks the house-style rules."""


class NoteGenerationError(SyncError):
    """The note could not be drafted."""


class WriteRefused(SyncError):
    """A write was attempted on an item that has not been confirmed."""
