# pyright: reportMissingTypeStubs=false
# pyright: reportUnknownVariableType=false
"""Pure AX/DOM semantic classification helpers."""

from __future__ import annotations

import re

from nodriver.cdp.accessibility import AXNode, AXNodeId, AXPropertyName, AXValue
from nodriver.cdp.dom import BackendNodeId, Node

INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "checkbox",
        "radio",
        "combobox",
        "listbox",
        "option",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "slider",
        "spinbutton",
        "switch",
        "tab",
        "treeitem",
    }
)
CONTEXT_ROLES = frozenset(
    {
        "heading",
        "status",
        "alert",
        "alertdialog",
        "navigation",
        "banner",
        "main",
        "contentinfo",
        "complementary",
        "region",
        "paragraph",
        "label",
        "labeltext",
    }
)
_NATIVE_CONTROL_TAGS = frozenset({"button", "input", "select", "textarea", "summary"})
_STATE_PROPERTIES = frozenset(
    {
        AXPropertyName.DISABLED,
        AXPropertyName.CHECKED,
        AXPropertyName.EXPANDED,
        AXPropertyName.SELECTED,
        AXPropertyName.PRESSED,
        AXPropertyName.REQUIRED,
        AXPropertyName.INVALID,
        AXPropertyName.MULTISELECTABLE,
        AXPropertyName.MULTILINE,
        AXPropertyName.READONLY,
        AXPropertyName.FOCUSED,
    }
)
_SECRET_HINT = re.compile(
    r"(?:password|passcode|passwd|one[-_ ]?time|otp|verification[-_ ]?code|"
    r"security[-_ ]?code|secret|token|cvv|cvc|card[-_ ]?number|credit[-_ ]?card|ssn)",
    re.IGNORECASE,
)


def descendant_text(node: AXNode, by_id: dict[AXNodeId, AXNode]) -> str:
    fragments: list[str] = []

    def walk(current: AXNode) -> None:
        if ax_text(current.role).casefold() == "statictext" and ax_text(current.name):
            fragments.append(ax_text(current.name))
        for child_id in current.child_ids or ():
            if child := by_id.get(child_id):
                walk(child)

    for child_id in node.child_ids or ():
        if child := by_id.get(child_id):
            walk(child)
    return " ".join(fragments)


def index_dom(root: Node) -> dict[BackendNodeId, Node]:
    result: dict[BackendNodeId, Node] = {}

    def walk(node: Node) -> None:
        result[node.backend_node_id] = node
        for child in (*tuple(node.children or ()), *tuple(node.shadow_roots or ())):
            walk(child)
        if node.content_document is not None:
            walk(node.content_document)

    walk(root)
    return result


def is_control(role: str, node: Node | None, properties: dict[str, object]) -> bool:
    if role in INTERACTIVE_ROLES or truthy(properties.get("editable")):
        return True
    if node is None:
        return False
    tag = node.node_name.casefold()
    if truthy(properties.get("focusable")):
        structural = {"rootwebarea", "webarea", "document", "iframe", "none", "presentation"}
        if role not in structural or attribute(node, "tabindex"):
            return True
    if tag in _NATIVE_CONTROL_TAGS:
        return tag != "input" or attribute(node, "type").casefold() != "hidden"
    return tag == "a" and bool(attribute(node, "href"))


def properties(node: AXNode) -> dict[str, object]:
    return {prop.name.value: prop.value.value for prop in node.properties or ()}


def is_modal(node: AXNode) -> bool:
    return ax_text(node.role).casefold() in {"dialog", "alertdialog"} or truthy(
        properties(node).get("modal")
    )


def state_strings(node: AXNode) -> frozenset[str]:
    states: set[str] = set()
    for prop in node.properties or ():
        if prop.name not in _STATE_PROPERTIES:
            continue
        value = prop.value.value
        if value is True or isinstance(value, str) and value.casefold() == "true":
            states.add(prop.name.value)
        elif value not in (None, False, "false"):
            states.add(f"{prop.name.value}:{str(value).casefold()}")
    return frozenset(states)


def is_sensitive(node: Node | None, input_type: str, name: str, description: str) -> bool:
    hints = " ".join(
        (
            name,
            description,
            *(
                attribute(node, key)
                for key in ("name", "id", "autocomplete", "aria-label", "placeholder")
            ),
        )
    )
    return input_type == "password" or bool(_SECRET_HINT.search(hints))


def attribute(node: Node | None, name: str) -> str:
    if node is None or not node.attributes:
        return ""
    for index in range(0, len(node.attributes) - 1, 2):
        if node.attributes[index].casefold() == name.casefold():
            return node.attributes[index + 1]
    return ""


def truncate(value: str, limit: int) -> tuple[str, bool]:
    return (value, False) if len(value) <= limit else (value[:limit], True)


def truthy(value: object) -> bool:
    return (
        value.casefold() not in {"", "false", "none", "undefined"}
        if isinstance(value, str)
        else bool(value)
    )


def ax_text(value: AXValue | None) -> str:
    return "" if value is None or value.value is None else str(value.value)
