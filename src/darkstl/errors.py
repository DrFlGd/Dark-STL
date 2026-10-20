"""Errors shared by every importer and the pipeline."""


class FormatError(ValueError):
    """Raised when a file does not match the format being parsed."""
