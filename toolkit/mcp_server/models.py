from dataclasses import dataclass
from typing import Any


class HubError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": self.message, "retryable": self.retryable}}


@dataclass
class Record:
    metadata: dict
    body: str
    markdown: str
    relative_path: str

    @property
    def id(self) -> str:
        return self.metadata["id"]

    @property
    def type(self) -> str:
        return self.metadata["type"]

    @property
    def status(self) -> str:
        return self.metadata["status"]
