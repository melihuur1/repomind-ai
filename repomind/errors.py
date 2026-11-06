class RepoMindError(Exception):
    """An expected failure, reported to the CLI user without a traceback."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint
