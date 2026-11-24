import string

import pytest

from hybrid_agent.actions import Action
from hybrid_agent.browser.browser_session import BrowserSession


def test_shortcut_letters_use_physical_keys_and_preserve_shift():
    for letter in string.ascii_letters:
        for modifiers in (["CtRl"], ["cTrL", "sHiFt"]):
            action = Action("hotkey", {"keys": [*modifiers, letter]})
            modifier_binding = "ControlOrMeta+Shift" if len(modifiers) == 2 else "ControlOrMeta"
            assert BrowserSession._keyboard_binding(action) == f"{modifier_binding}+Key{letter.upper()}"


@pytest.mark.parametrize(("keys", "expected"), [
    (["WiN", "R"], "Meta+KeyR"),
    (["AlT", "f12"], "Alt+F12"),
    (["MeTa", "F1"], "Meta+F1"),
    (["CTRL", "1"], "ControlOrMeta+1"),
    (["CTRL", "+"], "ControlOrMeta++"),
    (["SHIFT", "?"], "Shift+?"),
    (["CTRL", "KeyZ"], "ControlOrMeta+KeyZ"),
])
def test_shortcut_modifiers_named_keys_and_symbols(keys, expected):
    assert BrowserSession._keyboard_binding(Action("hotkey", {"keys": keys})) == expected


@pytest.mark.parametrize("key", ["A", "a", "?", "+"])
def test_single_press_keeps_its_literal_character(key):
    assert BrowserSession._keyboard_binding(Action("press", {"key": key})) == key
