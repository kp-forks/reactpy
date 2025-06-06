# pyright: reportIncompatibleMethodOverride=false
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import (
    Any,
    Callable,
    cast,
    overload,
)

from fastjsonschema import compile as compile_json_schema

from reactpy._warnings import warn
from reactpy.config import REACTPY_CHECK_JSON_ATTRS, REACTPY_DEBUG
from reactpy.core._f_back import f_module_name
from reactpy.core.events import EventHandler, to_event_handler_function
from reactpy.types import (
    ComponentType,
    CustomVdomConstructor,
    EllipsisRepr,
    EventHandlerDict,
    EventHandlerType,
    ImportSourceDict,
    InlineJavaScript,
    InlineJavaScriptDict,
    VdomAttributes,
    VdomChildren,
    VdomDict,
    VdomJson,
)

EVENT_ATTRIBUTE_PATTERN = re.compile(r"^on[A-Z]\w+")

VDOM_JSON_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema",
    "$ref": "#/definitions/element",
    "definitions": {
        "element": {
            "type": "object",
            "properties": {
                "tagName": {"type": "string"},
                "key": {"type": ["string", "number", "null"]},
                "error": {"type": "string"},
                "children": {"$ref": "#/definitions/elementChildren"},
                "attributes": {"type": "object"},
                "eventHandlers": {"$ref": "#/definitions/elementEventHandlers"},
                "inlineJavaScript": {"$ref": "#/definitions/elementInlineJavaScripts"},
                "importSource": {"$ref": "#/definitions/importSource"},
            },
            # The 'tagName' is required because its presence is a useful indicator of
            # whether a dictionary describes a VDOM model or not.
            "required": ["tagName"],
            "dependentSchemas": {
                # When 'error' is given, the 'tagName' should be empty.
                "error": {"properties": {"tagName": {"maxLength": 0}}}
            },
        },
        "elementChildren": {
            "type": "array",
            "items": {"$ref": "#/definitions/elementOrString"},
        },
        "elementEventHandlers": {
            "type": "object",
            "patternProperties": {
                ".*": {"$ref": "#/definitions/eventHandler"},
            },
        },
        "eventHandler": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "preventDefault": {"type": "boolean"},
                "stopPropagation": {"type": "boolean"},
            },
            "required": ["target"],
        },
        "elementInlineJavaScripts": {
            "type": "object",
            "patternProperties": {
                ".*": "str",
            },
        },
        "importSource": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "sourceType": {"enum": ["URL", "NAME"]},
                "fallback": {
                    "type": ["object", "string", "null"],
                    "if": {"not": {"type": "null"}},
                    "then": {"$ref": "#/definitions/elementOrString"},
                },
                "unmountBeforeUpdate": {"type": "boolean"},
            },
            "required": ["source"],
        },
        "elementOrString": {
            "type": ["object", "string"],
            "if": {"type": "object"},
            "then": {"$ref": "#/definitions/element"},
        },
    },
}
"""JSON Schema describing serialized VDOM - see :ref:`VDOM` for more info"""


# we can't add a docstring to this because Sphinx doesn't know how to find its source
_COMPILED_VDOM_VALIDATOR: Callable = compile_json_schema(VDOM_JSON_SCHEMA)  # type: ignore


def validate_vdom_json(value: Any) -> VdomJson:
    """Validate serialized VDOM - see :attr:`VDOM_JSON_SCHEMA` for more info"""
    _COMPILED_VDOM_VALIDATOR(value)
    return cast(VdomJson, value)


def is_vdom(value: Any) -> bool:
    """Return whether a value is a :class:`VdomDict`"""
    return isinstance(value, VdomDict)


class Vdom:
    """Class-based constructor for VDOM dictionaries.
    Once initialized, the `__call__` method on this class is used as the user API
    for `reactpy.html`."""

    def __init__(
        self,
        tag_name: str,
        /,
        allow_children: bool = True,
        custom_constructor: CustomVdomConstructor | None = None,
        import_source: ImportSourceDict | None = None,
    ) -> None:
        """Initialize a VDOM constructor for the provided `tag_name`."""
        self.allow_children = allow_children
        self.custom_constructor = custom_constructor
        self.import_source = import_source

        # Configure Python debugger attributes
        self.__name__ = tag_name
        module_name = f_module_name(1)
        if module_name:
            self.__module__ = module_name
            self.__qualname__ = f"{module_name}.{tag_name}"

    def __getattr__(self, attr: str) -> Vdom:
        """Supports accessing nested web module components"""
        if not self.import_source:
            msg = "Nested components can only be accessed on web module components."
            raise AttributeError(msg)
        return Vdom(
            f"{self.__name__}.{attr}",
            allow_children=self.allow_children,
            import_source=self.import_source,
        )

    @overload
    def __call__(
        self, attributes: VdomAttributes, /, *children: VdomChildren
    ) -> VdomDict: ...

    @overload
    def __call__(self, *children: VdomChildren) -> VdomDict: ...

    def __call__(
        self, *attributes_and_children: VdomAttributes | VdomChildren
    ) -> VdomDict:
        """The entry point for the VDOM API, for example reactpy.html(<WE_ARE_HERE>)."""
        attributes, children = separate_attributes_and_children(attributes_and_children)
        key = attributes.get("key", None)
        attributes, event_handlers, inline_javascript = (
            separate_attributes_handlers_and_inline_javascript(attributes)
        )
        if REACTPY_CHECK_JSON_ATTRS.current:
            json.dumps(attributes)

        # Run custom constructor, if defined
        if self.custom_constructor:
            result = self.custom_constructor(
                key=key,
                children=children,
                attributes=attributes,
                event_handlers=event_handlers,
            )

        # Otherwise, use the default constructor
        else:
            result = {
                **({"key": key} if key is not None else {}),
                **({"children": children} if children else {}),
                **({"attributes": attributes} if attributes else {}),
                **({"eventHandlers": event_handlers} if event_handlers else {}),
                **(
                    {"inlineJavaScript": inline_javascript} if inline_javascript else {}
                ),
                **({"importSource": self.import_source} if self.import_source else {}),
            }

        # Validate the result
        result = result | {"tagName": self.__name__}
        if children and not self.allow_children:
            msg = f"{self.__name__!r} nodes cannot have children."
            raise TypeError(msg)

        return VdomDict(**result)  # type: ignore


def separate_attributes_and_children(
    values: Sequence[Any],
) -> tuple[VdomAttributes, list[Any]]:
    if not values:
        return {}, []

    _attributes: VdomAttributes
    children_or_iterables: Sequence[Any]
    # ruff: noqa: E721
    if type(values[0]) is dict:
        _attributes, *children_or_iterables = values
    else:
        _attributes = {}
        children_or_iterables = values

    _children: list[Any] = _flatten_children(children_or_iterables)

    return _attributes, _children


def separate_attributes_handlers_and_inline_javascript(
    attributes: Mapping[str, Any],
) -> tuple[VdomAttributes, EventHandlerDict, InlineJavaScriptDict]:
    _attributes: VdomAttributes = {}
    _event_handlers: dict[str, EventHandlerType] = {}
    _inline_javascript: dict[str, InlineJavaScript] = {}

    for k, v in attributes.items():
        if callable(v):
            _event_handlers[k] = EventHandler(to_event_handler_function(v))
        elif isinstance(v, EventHandler):
            _event_handlers[k] = v
        elif EVENT_ATTRIBUTE_PATTERN.match(k) and isinstance(v, str):
            _inline_javascript[k] = InlineJavaScript(v)
        elif isinstance(v, InlineJavaScript):
            _inline_javascript[k] = v
        else:
            _attributes[k] = v

    return _attributes, _event_handlers, _inline_javascript


def _flatten_children(children: Sequence[Any]) -> list[Any]:
    _children: list[VdomChildren] = []
    for child in children:
        if _is_single_child(child):
            _children.append(child)
        else:
            _children.extend(_flatten_children(child))
    return _children


def _is_single_child(value: Any) -> bool:
    if isinstance(value, (str, Mapping)) or not hasattr(value, "__iter__"):
        return True
    if REACTPY_DEBUG.current:
        _validate_child_key_integrity(value)
    return False


def _validate_child_key_integrity(value: Any) -> None:
    if hasattr(value, "__iter__") and not hasattr(value, "__len__"):
        warn(
            f"Did not verify key-path integrity of children in generator {value} "
            "- pass a sequence (i.e. list of finite length) in order to verify"
        )
    else:
        for child in value:
            if isinstance(child, ComponentType) and child.key is None:
                warn(f"Key not specified for child in list {child}", UserWarning)
            elif isinstance(child, Mapping) and "key" not in child:
                # remove 'children' to reduce log spam
                child_copy = {**child, "children": EllipsisRepr()}
                warn(f"Key not specified for child in list {child_copy}", UserWarning)
