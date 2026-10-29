from __future__ import annotations
from typing import Callable, overload
from functools import wraps
from .types import Result, ErrorInfo, CancelledError

def is_except_safe_wrapper(fn: Callable[..., object]) -> bool:
    return getattr(fn, "__xun_except_safe_wrapper__", False)

def mark_except_safe_wrapper(fn: Callable[..., object]) -> None:
    setattr(fn, "__xun_except_safe_wrapper__", True)

@overload
def except_safe[**P, T, E](fn: Callable[P, Result[T, E]]) -> Callable[P, Result[T, E | ErrorInfo]]: ...  # type: ignore[misc]
@overload
def except_safe[**P, R](fn: Callable[P, R]) -> Callable[P, Result[R, ErrorInfo]]: ...

def except_safe[**P](fn: Callable):
    if is_except_safe_wrapper(fn):
        return fn

    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> Result:
        try:
            result = fn(*args, **kwargs)
            if isinstance(result, Result):
                return result
            return Result.Ok(result)
        except (KeyboardInterrupt, CancelledError):
            raise
        except Exception as exc:
            return Result.Err(ErrorInfo(
                error=str(exc),
                details=repr(exc),
            ))

    mark_except_safe_wrapper(wrapper)
    return wrapper