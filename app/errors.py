"""Only explicitly safe errors may leave the application boundary."""


class AppError(Exception):
    def __init__(self, code: str, message: str, http_status: int, category: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.category = category
