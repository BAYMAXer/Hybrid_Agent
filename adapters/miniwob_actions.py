"""Translate resolved agent actions to the MiniWoB++ 1.1 all_supported action space."""
from __future__ import annotations

import math
from collections.abc import Iterable
from numbers import Real
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..actions import Action


SUPPORTED_ACTIONS = frozenset({
    "click", "double_click", "type", "press", "hotkey", "scroll", "drag_and_drop",
})

_KEY_ALIASES = {
    "return": "enter", "esc": "escape", "left": "arrowleft", "up": "arrowup",
    "right": "arrowright", "down": "arrowdown", "pgup": "pageup", "pgdn": "pagedown",
    "del": "delete", "spacebar": "space", " ": "space",
}
_MODIFIERS = {"ctrl": "C-", "control": "C-", "alt": "A-", "shift": "S-", "meta": "M-", "cmd": "M-"}
_SUBMIT_TAGS = {"input_text", "input_search"}
_EDITABLE_TAGS = _SUBMIT_TAGS | {
    "textarea", "input_email", "input_number", "input_password", "input_tel", "input_url",
    "input_date", "input_time", "input_month", "input_week", "input_datetime-local",
}


def _press(keys):
    from miniwob.constants import DEFAULT_ALLOWED_KEYS

    if not isinstance(keys, (tuple, list)) or not keys or not all(isinstance(k, str) and k for k in keys):
        raise ValueError("Expected a key name or a list of key names")
    modifiers = []
    for name in keys[:-1]:
        modifier = _MODIFIERS.get(name.casefold())
        if not modifier or modifier in modifiers:
            raise ValueError(f"Invalid MiniWoB key combination: {keys!r}")
        modifiers.append(modifier)
    key = keys[-1]
    if len(key) == 1 and key != " ":
        normalized = key.lower() if modifiers else key
        if modifiers == ["S-"] and key.isascii() and key.isalpha():
            normalized = key.upper()
            modifiers = []
    else:
        label = key.strip("<>").casefold()
        label = _KEY_ALIASES.get(label, label)
        normalized = next((k for k in DEFAULT_ALLOWED_KEYS if k.casefold() == f"<{label}>"), "")
    combo = "".join(modifiers) + normalized
    if combo not in DEFAULT_ALLOWED_KEYS:
        raise ValueError(f"Key combination outside MiniWoB's all_supported allowlist: {keys!r}")
    return "PRESS_KEY", {"key": DEFAULT_ALLOWED_KEYS.index(combo)}


def _point(x, y, width, height):
    if not all(isinstance(n, Real) and not isinstance(n, bool) and math.isfinite(n) for n in (x, y)):
        raise ValueError("Missing or invalid grounded coordinates")
    if not (0 <= x < width and 0 <= y < height):
        raise ValueError(f"Target is outside the {width}x{height} task screenshot")
    return {"coords": [int(x), int(y)]}


def _ref_node(raw, ref):
    if ref is not None and ref > 0:
        for node in raw["dom_elements"]:
            if node["ref"] == ref:
                return node
    raise ValueError("MiniWoB element reference is absent from the current observation")


def _center(node, width, height):
    # MiniWoB serializes each geometry component as a one-value NumPy array.
    values = [node[k] for k in ("left", "top", "width", "height")]
    left, top, w, h = [float(v.item() if hasattr(v, "item") else v) for v in values]
    right, bottom = min(left + w, width), min(top + h, height)
    left, top = max(left, 0), max(top, 0)
    if left >= right or top >= bottom:
        raise ValueError("Element is outside the task screenshot")
    return _point((left + right) / 2, (top + bottom) / 2, width, height)


def translate_action(action: Action, raw_observation: dict, *, ref: int | None = None) -> Iterable[tuple[str, dict]]:
    """Produce official primitives, validating the whole action before the first dispatch.

    The environment must stop this sequence on termination and call
    validate_continuation before dispatching each subsequent primitive.
    """
    name, args = action.action, action.args
    if name not in SUPPORTED_ACTIONS:
        raise ValueError(f"Action {name!r} is unavailable in the MiniWoB adapter")
    if args.get("grounding_failed"):
        raise ValueError("Grounding failed; no MiniWoB action was dispatched")
    if name == "press":
        return [_press([args.get("key")])]
    if name == "hotkey":
        return [_press(args.get("keys"))]

    image = raw_observation["screenshot"]
    if hasattr(image, "shape"):
        height, width = image.shape[:2]
    else:
        width, height = image.size
    node = _ref_node(raw_observation, ref) if ref is not None else None
    if name == "drag_and_drop":
        start = _point(args.get("x1"), args.get("y1"), width, height)
        end = _point(args.get("x2"), args.get("y2"), width, height)
        return [("MOUSEDOWN_COORDS", start), ("MOVE_COORDS", end), ("MOUSEUP_COORDS", end)]
    if name == "scroll":
        direction, amount = args.get("direction"), args.get("amount")
        if type(amount) is not int or amount < 1 or direction not in {"up", "down"}:
            raise ValueError("Specify scroll direction up/down and a positive wheel tick count")
        if node is not None:
            point = _center(node, width, height)
        elif "x" in args or "y" in args:
            point = _point(args.get("x"), args.get("y"), width, height)
        else:
            point = _point(width // 2, height // 2, width, height)
        # The official episode timer can end a long scroll between any two ticks.
        primitive = (f"SCROLL_{direction.upper()}_COORDS", point)
        return (primitive for _ in range(amount))
    if name == "double_click":
        point = (_center(node, width, height) if node is not None else
                 _point(args.get("x"), args.get("y"), width, height))
        return [("DBLCLICK_COORDS", point)]

    click = (("CLICK_ELEMENT", {"ref": int(ref)}) if node is not None else
             ("CLICK_COORDS", _point(args.get("x"), args.get("y"), width, height)))
    if name == "click":
        return [click]

    from miniwob.constants import TYPING_MAX_LENGTH, WEBDRIVER_SPECIAL_KEYS

    value = args.get("text")
    if not isinstance(value, str) or not value:
        raise ValueError("type requires non-empty text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("type text contains an unpaired Unicode surrogate") from exc
    # Selenium interprets these code points as key events even in TYPE_TEXT.
    if any(key in value for key in WEBDRIVER_SPECIAL_KEYS.values()):
        raise ValueError("WebDriver special keys require an allowed press/hotkey action")
    if node is not None and node["tag"].casefold() not in _EDITABLE_TAGS:
        raise ValueError("DOM typing requires a text-entry control")
    overwrite = args.get("overwrite", False)
    if type(overwrite) is not bool:
        raise ValueError("overwrite must be a boolean")
    submit = args.get("submit_key")
    if submit is not None and (submit != "Enter" or not overwrite or node is None or
                               node["tag"].casefold() not in _SUBMIT_TAGS):
        raise ValueError("Combined Enter requires overwrite on a referenced single-line text/search input")
    actions = [click]
    if overwrite:
        # Selecting the old value then typing replaces its contents in one edit.
        actions.append(_press(["ctrl", "a"]))
    for start in range(0, len(value), TYPING_MAX_LENGTH):
        actions.append(("TYPE_TEXT", {"text": value[start:start + TYPING_MAX_LENGTH]}))
    if submit:
        actions.append(_press([submit]))
    return actions


def validate_continuation(action: Action, raw_observation: dict, *, ref: int | None, primitive: tuple[str, dict]):
    """A DOM-targeted type may only continue while that exact input retains focus."""
    if ref is None or action.action != "type":
        return
    node = _ref_node(raw_observation, ref)
    if not node["flags"][0]:
        raise ValueError("Input lost focus after dispatch; remaining keystrokes were skipped")
    if action.args.get("submit_key") and primitive == _press(["Enter"]):
        if node["value"] != action.args["text"] or node["tag"].casefold() not in _SUBMIT_TAGS:
            raise ValueError("Input readback did not match; Enter was skipped")
