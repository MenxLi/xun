from __future__ import annotations
from typing import Awaitable, Callable, TypeVar, Union, ParamSpec
from typing import get_origin, get_args, cast
from typing_extensions import TypedDict
from functools import wraps
import inspect, types

P = ParamSpec("P")
R = TypeVar("R")
class ErrorInfo(TypedDict):
    error: str
    details: str
def except_safe(fn: Callable[P, R]) -> Callable[P, Union[R, ErrorInfo]]:

    def _error_info(exc: Exception) -> ErrorInfo:
        return {
            "error": str(exc),
            "details": repr(exc),
        }

    def _with_error_return_annotation(annotation: object) -> object:
        if annotation is inspect.Signature.empty:
            return ErrorInfo

        origin = get_origin(annotation)
        if origin in (Union, types.UnionType) and ErrorInfo in get_args(annotation):
            return annotation

        return Union[annotation, ErrorInfo]

    def _preserve_signature(wrapper: Callable[..., object], fn: Callable[..., object]) -> None:
        try:
            signature = inspect.signature(fn)
            return_annotation = _with_error_return_annotation(signature.return_annotation)
            setattr(
                wrapper,
                "__signature__",
                signature.replace(return_annotation=return_annotation),
            )
            annotations = dict(getattr(fn, "__annotations__", {}))
            annotations["return"] = return_annotation
            setattr(wrapper, "__annotations__", annotations)
        except (TypeError, ValueError):
            print(f"Warning: Unable to preserve signature for function {fn.__name__}")

    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> object:
            try:
                return await cast(Callable[P, Awaitable[object]], fn)(*args, **kwargs)
            except Exception as exc:
                return _error_info(exc)

        _preserve_signature(async_wrapper, fn)
        return cast(Callable[P, Union[R, ErrorInfo]], async_wrapper)

    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> Union[R, ErrorInfo]:
        try:
            return cast(Callable[P, R], fn)(*args, **kwargs)
        except Exception as exc:
            return _error_info(exc)

    _preserve_signature(wrapper, fn)
    wrapper.__setattr__("__xun_except_safe_wrapper__", True)  # type: ignore
    return cast(Callable[P, Union[R, ErrorInfo]], wrapper)

def is_except_safe_wrapper(fn: Callable[..., object]) -> bool:
    return getattr(fn, "__xun_except_safe_wrapper__", False) is True