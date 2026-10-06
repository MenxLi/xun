from __future__ import annotations
import sys

if sys.version_info >= (3, 13):
    from typing import TypeVar
else:
    # type var with `default` is available in Python 3.13 and later
    from typing_extensions import TypeVar

# https://pydantic.dev/docs/validation/latest/concepts/types/#named-recursive-types
if sys.version_info >= (3, 12):
    type JsonType = str | int | float | bool | None | dict[str, JsonType] | list[JsonType]
else:
    from typing import Union
    from typing_extensions import TypeAliasType
    JsonType = TypeAliasType(
        'JsonType',
        'Union[dict[str, JsonType], list[JsonType], str, int, float, bool, None]',  
    )

import json
from typing import Callable, Literal, Protocol, Self, Union, cast
from dataclasses import dataclass

# These are not an abstract that must obey a specific inheritance,
# but a suggested contract for all classes implementing JSON ser/de.
class ToJson(Protocol):
    def to_json(self) -> JsonType: ...
class FromJson(Protocol):
    @classmethod
    def from_json(cls, data: JsonType) -> Self: ...

class Result[T, E](ToJson):
    def __init__(self, value: Union[T, E], is_ok: bool):
        self._value = value
        self._is_ok = is_ok

    @classmethod
    def Ok(cls, value: T) -> Result[T, E]:
        return cls(value, True)

    @classmethod
    def Err(cls, error: E) -> Result[T, E]:
        return cls(error, False)

    def is_ok(self) -> bool:
        return self._is_ok

    def is_err(self) -> bool:
        return not self._is_ok

    def unwrap(self) -> T:
        if self.is_ok():
            return cast(T, self._value)
        else:
            raise Exception(f"Called unwrap on an Err value: {self._value}")

    def unwrap_err(self) -> E:
        if self.is_err():
            return cast(E, self._value)
        else:
            raise Exception(f"Called unwrap_err on an Ok value: {self._value}")
    
    @property
    def value(self) -> Union[T, E]:
        return self._value
    
    def value_json(self) -> JsonType:
        from .util import to_json_object
        return to_json_object(self._value)
    
    def value_str(self) -> str:
        if isinstance(self._value, str):
            return self._value
        return json.dumps(self.value_json(), ensure_ascii=False)
    
    def to_json(self) -> dict[str, JsonType]:
        return {
            "value": self.value_json(),
            "is_ok": self._is_ok,
        }
    
    @classmethod
    def from_payload[OkT, ErrT](
        cls,
        data: JsonType,
        ok_factory: Callable[[JsonType], OkT] = lambda x: x,
        err_factory: Callable[[JsonType], ErrT] = lambda x: x,
    ) -> Result[OkT, ErrT]:
        if not isinstance(data, dict):
            raise TypeError(f"Result payload must be a JSON object, got {type(data).__name__}")
        value = data["value"]
        is_ok = data.get("is_ok")
        if not isinstance(is_ok, bool):
            raise TypeError(f"Result payload requires bool 'is_ok', got {type(is_ok).__name__}")
        factory = ok_factory if is_ok else err_factory
        return Result[OkT, ErrT](factory(value), is_ok)
    
    def __str__(self) -> str:
        return f"Result({self.value_str()}, is_ok={self._is_ok})"
    
    def __repr__(self) -> str:
        return self.__str__()

@dataclass
class ErrorInfo(ToJson, FromJson):
    error: str
    details: str

    def to_json(self) -> dict[str, JsonType]:
        return {
            "error": self.error,
            "details": self.details,
        }

    @classmethod
    def from_json(cls, data: JsonType) -> "ErrorInfo":
        if not isinstance(data, dict):
            raise TypeError(f"ErrorInfo payload must be a JSON object, got {type(data).__name__}")
        error, details = data.get("error"), data.get("details")
        if not isinstance(error, str) or not isinstance(details, str):
            raise TypeError("ErrorInfo payload requires string 'error' and 'details'")
        return cls(error=error, details=details)

class CancelledError(Exception):
    """Raised when an operation is cancelled."""
    pass

type ModelCapabilityType = Literal['vision']
ModelCapabilityOptions = set(['vision'])

type ToolResultType = Result[JsonType, ErrorInfo]