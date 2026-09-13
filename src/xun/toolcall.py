from __future__ import annotations
from dataclasses import dataclass, field
import json
from typing import (
    Callable, Any, Optional, Sequence, 
    get_origin, cast, get_type_hints, 
    TYPE_CHECKING
)
from functools import wraps
import inspect
from openai.types.chat import ChatCompletionToolParam
from pydantic import BaseModel, ConfigDict, ValidationError, create_model
from .types import ModelCapabilityType
if TYPE_CHECKING:
    from .agent import Agent

class ToolCallContext[T]:
    """
    Context for a tool call, 
    can be used to pass additional information to the tool.
    """
    def __init__( self, agent: "Agent[Agent.T.Init]", tool_name: str, v: T, ):
        self._agent = agent
        self._tool_name = tool_name
        self._v = v
    @property
    def agent(self): return self._agent
    @property
    def tool_name(self): return self._tool_name
    @property
    def value(self): return self._v

    @staticmethod
    def _dummy() -> ToolCallContext[None]:
        # for testing and debugging purposes, when no actual context is available
        return ToolCallContext(None, "", None)  # type: ignore[arg-type]

def _context_var_name(func: Callable) -> Optional[str]:
    sig = inspect.signature(func)
    type_hints = get_type_hints(func)
    for name in sig.parameters.keys():
        hint = type_hints.get(name)
        if hint is ToolCallContext or get_origin(hint) is ToolCallContext:
            return name
    return None

@dataclass(frozen=True)
class ToolAttr:
    name: Optional[str]
    required_capabilities: Sequence[ModelCapabilityType]
    override: bool

    def attach_to[F: Callable](self, func: F) -> F:
        """Attach this ToolAttr to a function."""
        setattr(func, "__xun_tool_attr", self)
        return func
    
    @classmethod
    def extract_from(cls, func: Callable) -> Optional[ToolAttr]:
        """Extract a ToolAttr from a function, if it exists."""
        return getattr(func, "__xun_tool_attr", None)

def tool_attr(
    name: Optional[str] = None, 
    required_capabilities: Sequence[ModelCapabilityType] = [], 
    override: bool = False,
    ):
    """ Attach metadata to a function """
    def _wrapper[F: Callable](fn: F) -> F:
        @wraps(fn)
        def wrapped(*args, **kwargs):
            return fn(*args, **kwargs)
        ToolAttr(
            name=name, 
            required_capabilities=required_capabilities, 
            override=override,
            ).attach_to(wrapped)
        return wrapped  # type: ignore[return-value]
    return _wrapper

@dataclass(frozen=True)
class Function:
    func: Callable
    name: str
    description: str
    args_model: type[BaseModel]
    tool_schema: ChatCompletionToolParam
    context_param: Optional[str]
    required_capabilities: set[ModelCapabilityType]
    override: bool

    @staticmethod
    def from_function(func: Callable) -> Function:
        """
        Create a Function instance from a regular Python function.
        The function's signature and docstring are used to generate the tool schema.
        The return type of the function is supposed to be JSON-serializable.
        """
        description = func.__doc__ or ""
        description = description.strip()

        attr = ToolAttr.extract_from(func)

        if attr and attr.name: name = attr.name
        else: name = func.__name__

        if attr and attr.required_capabilities: 
            required_capabilities = set(attr.required_capabilities)
        else: required_capabilities = set()

        override = attr.override if attr else False

        ctx_param_name = _context_var_name(func)
        args_model = _build_args_model_from_function(
            func, 
            skip_keys=[ctx_param_name] if ctx_param_name else []
            )
        tool_param = ChatCompletionToolParam(
            type="function",
            function={
                "name": name,
                "description": description,
                "parameters": args_model.model_json_schema(),
            },
        )
        return Function(
            func=func,
            name=name,
            description=description,
            tool_schema=tool_param,
            args_model=args_model,
            context_param=ctx_param_name,
            required_capabilities=required_capabilities,
            override=override
        )

    def call(self, args: str | dict[str, Any], context: ToolCallContext):
        """Invoke the wrapped function with OpenAI tool-call arguments."""
        if isinstance(args, str):
            raw = args.strip()
            if not raw:
                parsed_args: Any = {}
            else:
                try:
                    parsed_args = json.loads(raw)
                except json.JSONDecodeError as e:
                    raise ValueError(
                        f"Tool '{self.name}' arguments must be valid JSON: {e.msg}"
                    ) from e
        elif isinstance(args, dict):
            parsed_args = args
        else:
            raise TypeError(
                f"Tool '{self.name}' arguments must be a JSON string or dict, got {type(args).__name__}."
            )

        if not isinstance(parsed_args, dict):
            raise ValueError(
                f"Tool '{self.name}' arguments must decode to a JSON object, got {type(parsed_args).__name__}."
            )

        try:
            validated = self.args_model.model_validate(parsed_args, strict=True)
        except ValidationError as e:
            raise ValueError(f"Invalid arguments for tool '{self.name}': {e}") from e

        if self.context_param:
            return self.func(**validated.model_dump(), **{self.context_param: context})
        else:
            return self.func(**validated.model_dump())


def _build_args_model_from_function(func: Callable, skip_keys: list[str] = []) -> type[BaseModel]:
    sig = inspect.signature(func)
    type_hints = get_type_hints(func)
    fields: dict[str, tuple[Any, Any]] = {}
    for name, param in sig.parameters.items():
        if name in ("self", "cls") or name in skip_keys:
            continue
        if name in type_hints:
            param_type = type_hints[name]
        elif param.default is not inspect.Parameter.empty and param.default is not None:
            param_type = type(param.default)
        else:
            param_type = Any

        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (param_type, default)

    create_fields = cast(dict[str, Any], fields)
    return create_model(  # type: ignore[call-overload]
        f"ToolArgs_{func.__name__}",
        __config__=ConfigDict(extra="forbid"),
        **create_fields,
    )

if __name__ == "__main__":
    def example_tool(x: int, y: str = "default", ctx: ToolCallContext = ToolCallContext._dummy()) -> str:
        """Example tool that takes an integer and a string."""
        return f"x={x}, y={y}, ctx={ctx.value}"
    
    print(_context_var_name(example_tool))
