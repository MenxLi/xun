"""Agent lifecycle state machine: the runtime state classes, the `T` annotation
namespace, and the lifecycle TypeVars.

Kept in its own module (no xun imports beyond `types`) so `agent.py`,
`display_abstract.py` and any other mixin can all reference the lifecycle
states without a circular import of `agent.py`.
"""

from __future__ import annotations

from .types import TypeVar

class _Lifecycle: v=0
class _Uninit(_Lifecycle): v=1
class _Init(_Lifecycle): v=2
class _Final(_Lifecycle): v=3

# covariant: an Agent[T.Init] is usable anywhere an Agent[T.Any] is expected, 
# but not vice versa. default=_Uninit: a bare `Agent` denotes a freshly constructed agent,
LifecycleT = TypeVar("LifecycleT", bound=_Lifecycle, covariant=True, default=_Uninit)
_LT = TypeVar("_LT", bound=_Lifecycle, covariant=True)

class T:
    """
    Namespace for type-level lifecycle states for annotations: 
    `Agent[T.Uninit]` / `Agent[T.Init]` / ...
    """
    Uninit = _Uninit
    Init = _Init
    Final = _Final
    Alive = _Uninit | _Init
    Any = _Lifecycle
