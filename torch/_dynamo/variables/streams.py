from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

import torch
from torch.fx import Proxy

from .. import graph_break_hints
from ..device_interface import get_interface_for_device
from ..exc import TYPE_CHECKING, unimplemented_v2
from .base import VariableTracker
from .constant import ConstantVariable
from .ctx_manager import ContextWrappingVariable
from .misc import GetAttrVariable


if TYPE_CHECKING:
    from torch._dynamo.symbolic_convert import InstructionTranslator
    from ..codegen import PyCodegen

from torch._library.custom_ops import custom_op


# Avoid circular dependency for the dataclass
TensorVariable = Any
Tensor = torch.Tensor


@custom_op("streams::fork", mutates_args=())
def fork_stream(
    from_index: int,
    from_device: torch.device,
    from_device_index: int,
    to_index: int,
    to_device: torch.device,
    to_device_index: int,
) -> None:
    pass


@custom_op("streams::join", mutates_args=())
def join_stream(
    from_index: int,
    from_device: torch.device,
    from_device_index: int,
    to_index: int,
    to_device: torch.device,
    to_device_index: int,
) -> None:
    pass


# Stream state consists of the fork stream node
# and the external to the stream that are accessed from within the
# stream
@dataclass
class StreamState:
    prev_stream_info: tuple[Proxy, Proxy, Proxy]


class StreamStateManager:
    """
    Class used to track the current stream context we are in and identify
    any used tensors as external (created outside the stream context) or
    internal (created within the stream context). We use this information to
    ensure the fork op is dependent on any external tensors, so that it will not
    be reordered before them or after ops which use the externally created tensors.
    Analagously, we use the internal tensors to ensure that the join op is not
    reordered before any internally created tensors or after ops which use the
    internally created tensors.

    To actually implement this, we have a stack of stream states which track any external tensors that
    have not yet been seen within the stream context and any tensors created within the stream context.
    Once we exit the stream context we populate the args of fork with all external tensors which have been used,
    and join with any internal tensors that were created.
    """

    def __init__(self) -> None:
        self.state_stack: deque[StreamState] = deque()

    def in_stream_context(self) -> bool:
        return bool(self.state_stack)

    def push_stream_state(
        self, index_proxy: Proxy, device_proxy: Proxy, device_index_proxy: Proxy
    ) -> None:
        self.state_stack.append(
            StreamState((index_proxy, device_proxy, device_index_proxy))
        )

    def pop_stream_state(self) -> StreamState:
        assert self.state_stack, "No stream state to pop"
        return self.state_stack.pop()


stream_state_mgr = StreamStateManager()


class StreamContextVariable(ContextWrappingVariable):
    """This represents torch.cuda.StreamContext"""

    @staticmethod
    def create(
        tx: "InstructionTranslator",
        target_value: "StreamVariable",
        **kwargs: dict[str, Any],
    ) -> "StreamContextVariable":
        from .builder import wrap_fx_proxy_cls

        current_stream_method = get_interface_for_device(
            target_value.device
        ).current_stream
        current_stream = wrap_fx_proxy_cls(
            StreamVariable,
            tx,
            tx.output.create_proxy(
                "call_function",
                current_stream_method,
                (None,),
                {},
            ),
        )
        return StreamContextVariable(
            target_values=[target_value],
            initial_values=[current_stream],
            device=target_value.device,
            **kwargs,
        )

    def __init__(
        self,
        target_values: list["StreamVariable"],
        device: torch.device,
        initial_values: Optional[list["StreamVariable"]] = None,
        **kwargs: dict[str, Any],
    ) -> None:
        super().__init__(
            target_values=target_values, initial_values=initial_values, **kwargs
        )
        self.device = device
        self.set_stream_id = get_interface_for_device(self.device)._set_stream_by_id

    def enter(self, tx: "InstructionTranslator") -> "VariableTracker":
        stream_proxy = self.target_values[0].as_proxy()
        stream_id, device, device_index = (
            StreamContextVariable._extract_stream_properties(stream_proxy)
        )
        proxy = tx.output.create_proxy(
            "call_function",
            torch.ops.streams.fork.default,
            (stream_id, device, device_index, []),
            {},
        )
        stream_state_mgr.push_stream_state(proxy.node)
        return ConstantVariable.create(None)

    def exit(self, tx: "InstructionTranslator", *args: tuple[Any]) -> "VariableTracker":
        state = stream_state_mgr.pop_stream_state()
        initial_stream_proxy = self.initial_values[0].as_proxy()
        stream_id, device, device_index = (
            StreamContextVariable._extract_stream_properties(initial_stream_proxy)
        )
        tx.output.create_node(
            "call_function",
            torch.ops.streams.join.default,
            (
                stream_id.node,
                device.node,
                device_index.node,
                list(state.internal_nodes),
            ),
            {},
        )
        state.fork_node.args = (
            state.fork_node.args[0],
            state.fork_node.args[1],
            state.fork_node.args[2],
            list(state.external_nodes),
        )
        return ConstantVariable.create(None)

    @staticmethod
    def _extract_stream_properties(stream_proxy: Proxy) -> tuple[Proxy, Proxy, Proxy]:
        stream_index = GetAttrVariable.create_getattr_proxy(stream_proxy, "stream_id")
        stream_device = GetAttrVariable.create_getattr_proxy(stream_proxy, "device")
        stream_device_index = GetAttrVariable.create_getattr_proxy(
            stream_proxy, "device_index"
        )
        return stream_index, stream_device, stream_device_index


class StreamVariable(VariableTracker):
    """Represents the device-agnostic torch.Stream class"""

    def __init__(
        self,
        proxy: Proxy,
        value: torch.Stream,
        device: torch.device,
        **kwargs: Any,
    ) -> None:
        if proxy is not None and "example_value" in proxy.node.meta:
            assert proxy.node.meta["example_value"] == value
        assert value.device.type == device.type, (
            "stream value is not equal to the passed device"
        )
        super().__init__(**kwargs)
        self.proxy = proxy
        self.value = value
        self.device = device

    def python_type(self) -> type:
        return torch.Stream

    def call_method(
        self,
        tx: "InstructionTranslator",
        name: str,
        args: list[VariableTracker],
        kwargs: dict[str, VariableTracker],
    ) -> "VariableTracker":
        assert hasattr(self.value, name), f"no stream method found named {name}"

        from ..utils import cmp_name_to_op_mapping, proxy_args_kwargs
        from .builder import wrap_fx_proxy_cls

        if name in ("wait_stream", "synchronize", "wait_event"):
            tx.output.create_proxy(
                "call_method", name, *proxy_args_kwargs([self] + args, kwargs)
            )
            return ConstantVariable(None)
        elif name == "query":
            return wrap_fx_proxy_cls(
                target_cls=ConstantVariable,
                tx=tx,
                proxy=tx.output.create_proxy(
                    "call_method", name, *proxy_args_kwargs([self] + args, kwargs)
                ),
            )
        elif name == "record_event":
            return wrap_fx_proxy_cls(
                target_cls=EventVariable,
                tx=tx,
                proxy=tx.output.create_proxy(
                    "call_method", name, *proxy_args_kwargs([self] + args, kwargs)
                ),
            )
        elif name in cmp_name_to_op_mapping and len(args) == 1 and not kwargs:
            from ..guards import GuardBuilder, install_guard

            if self.source:
                install_guard(self.source.make_guard(GuardBuilder.EQUALS_MATCH))

            # NB : Checking for mutation is necessary because we compare
            # constant values
            other = args[0]
            if not isinstance(other, StreamVariable):
                return ConstantVariable.create(NotImplemented)

            if other.source:
                install_guard(self.source.make_guard(GuardBuilder.EQUALS_MATCH))
            return ConstantVariable.create(
                cmp_name_to_op_mapping[name](self.value, other.value)  # type: ignore[arg-type]
            )

        return super().call_method(tx, name, args, kwargs)

    def as_proxy(self) -> Proxy:
        return self.proxy

    def reconstruct(self, codegen: "PyCodegen") -> None:
        # If we got here, this stream is fully subsumed by the graph - this means it is
        # not an input or global
        assert not self.source
        # Since we just proved that - for other such structures, like lists and dicts, reconstruction
        # is fine and sound according to dynamo principles of treating collectives. However,
        # streams are special in that we want to preserve the identity of the stream as the same as in the graph
        # Normally, we would do this via codegen for the proxy mapping to an output - we cannot do this yet, as we do not
        # yet have a plan for how we want to handle the case where the stream is used as an input or an output. Pending
        # design, to unblock current work, we lift the stream into a global and then codegen bytecode to load it from there.
        prefix = f"_stream_{self.device}"
        name = codegen.tx.output.install_global_by_id(prefix, self.value)
        codegen.append_output(codegen.create_load_global(name, add=True))


class EventVariable(VariableTracker):
    def __init__(self, proxy: Proxy, value: torch.Event, **kwargs: Any) -> None:
        if proxy is not None and "example_value" in proxy.node.meta:
            assert proxy.node.meta["example_value"] == value
        super().__init__(**kwargs)
        self.proxy = proxy
        self.value = value

    def call_method(
        self,
        tx: "InstructionTranslator",
        name: str,
        args: list[VariableTracker],
        kwargs: dict[str, VariableTracker],
    ) -> VariableTracker:
        from ..utils import proxy_args_kwargs
        from .builder import wrap_fx_proxy_cls

        if name in ("wait", "record", "synchronize"):
            tx.output.create_proxy(
                "call_method", name, *proxy_args_kwargs([self] + args, kwargs)
            )
            return ConstantVariable(None)
        elif name == "query":
            return wrap_fx_proxy_cls(
                target_cls=ConstantVariable,
                tx=tx,
                proxy=tx.output.create_proxy(
                    "call_method", name, *proxy_args_kwargs([self] + args, kwargs)
                ),
            )
        else:
            method_name = (
                f"{type(self.value).__module__}.{type(self.value).__qualname__}.{name}"
            )
            unimplemented_v2(
                gb_type="Unsupported event method",
                context=str(name),
                explanation=f"Dynamo doesn't support tracing the {method_name} method. "
                f"We currently support wait, record, synchronize, and query.",
                hints=[
                    *graph_break_hints.SUPPORTABLE,
                ],
            )

    def as_proxy(self) -> Proxy:
        return self.proxy

    def reconstruct(self, codegen: "PyCodegen") -> None:
        # If we got here, this event is fully subsumed by the graph - this means it is
        # not an input or global
        assert not self.source
        # Similar to stream handling, we lift the event into a global and then codegen bytecode to load it from there.
        prefix = "_event"
        name = codegen.tx.output.install_global_by_id(prefix, self.value)
        codegen.append_output(codegen.create_load_global(name, add=True))
