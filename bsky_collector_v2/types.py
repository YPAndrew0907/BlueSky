from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NewType

RunId = NewType("RunId", str)
FeedUri = NewType("FeedUri", str)
PostUri = NewType("PostUri", str)
Did = NewType("Did", str)

ViewerMode = Literal["unauth", "auth"]
LogLevel = Literal["debug", "info"]


@dataclass(frozen=True)
class CollectorPaths:
    out_base: str

