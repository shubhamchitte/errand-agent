class ToolError(Exception):
    code = "tool_error"
    retryable = False

    def __init__(self, message: str = ""):
        super().__init__(message or self.code)


class TransientError(ToolError):
    """HTTP-503-like failure; nothing was executed. Safe to retry."""
    code = "503"
    retryable = True


class ToolTimeout(ToolError):
    """Timeout with UNKNOWN outcome: the side effect may or may not have happened."""
    code = "timeout"
    retryable = True
