"""Incremental extraction of one string field from a JSON object that is arriving in fragments.

A model streaming a FORCED tool call sends the arguments as a JSON document in pieces - `{"reply": "Here are tw`, `o pairs...", "cited_reason": ...}` -
and the text a shopper should see is only the decoded value of the `reply` key. `ReplyExtractor.feed()` takes each fragment and returns the NEW
decoded characters of that value, so the UI can show them while the model is still writing, without waiting for the closing brace and without ever
showing JSON syntax, other fields, or an escape sequence half-decoded.

Properties (all asserted by tests/shopper_v2/test_stream_parse.py, including splitting at EVERY position):
- the concatenation of everything returned equals `json.loads(whole)[key]` whatever the fragment boundaries (escapes, `\\uXXXX`, surrogate pairs and
  multi-byte text may be split anywhere);
- only the TOP-LEVEL `key` is read, and only its first occurrence: the same word inside another field's value or a nested object is ignored;
- it never raises: malformed input marks the extractor failed and it stops emitting (text already emitted is never retracted by this module - the caller
  decides what to do with a stream that did not complete);
- nothing is returned after the closing quote of the value.

Character-at-a-time state machine, O(total characters); no reparsing and no JSON library on partial input.
"""

from __future__ import annotations

_WS = " \t\r\n"
_SIMPLE = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_HEX = "0123456789abcdefABCDEF"

(
    _BEFORE_OBJECT, _EXPECT_KEY, _IN_KEY, _AFTER_KEY, _AFTER_COLON, _IN_TARGET,
    _IN_OTHER_STRING, _IN_NESTED, _IN_PRIMITIVE, _AFTER_VALUE, _DONE, _FAILED,
) = range(12)


class _StringDecoder:
    """Decodes the characters of ONE JSON string as they arrive, one at a time.

    `push(ch)` returns `(text, closed)`: the decoded text this character completes (possibly empty while an escape is pending) and whether this
    character was the closing quote. A malformed escape sets `bad`.
    """

    def __init__(self) -> None:
        self.bad = False
        self._escape = False
        self._hex: list[str] | None = None  # digits of an in-progress \uXXXX
        self._high: int | None = None  # a high surrogate waiting for its low half

    def _lone_high(self) -> str:
        if self._high is None:
            return ""
        self._high = None
        return "�"

    def push(self, ch: str) -> tuple[str, bool]:
        if self._hex is not None:
            if ch not in _HEX:
                self.bad = True
                return "", False
            self._hex.append(ch)
            if len(self._hex) < 4:
                return "", False
            code = int("".join(self._hex), 16)
            self._hex = None
            return self._code_unit(code), False
        if self._escape:
            self._escape = False
            if ch == "u":
                self._hex = []
                return "", False
            simple = _SIMPLE.get(ch)
            if simple is None:
                self.bad = True
                return "", False
            return self._lone_high() + simple, False
        if ch == "\\":
            self._escape = True
            return "", False
        if ch == '"':
            return self._lone_high(), True
        return self._lone_high() + ch, False

    def _code_unit(self, code: int) -> str:
        if 0xD800 <= code <= 0xDBFF:  # a high surrogate: its low half should follow as the next \uXXXX
            lone = self._lone_high()
            self._high = code
            return lone
        if 0xDC00 <= code <= 0xDFFF:
            if self._high is not None:
                pair = 0x10000 + ((self._high - 0xD800) << 10) + (code - 0xDC00)
                self._high = None
                return chr(pair)
            return "�"  # a lone low surrogate has no character
        return self._lone_high() + chr(code)


class ReplyExtractor:
    """Feed JSON text fragments; receive the newly decoded characters of the top-level string value under `key`."""

    def __init__(self, key: str = "reply") -> None:
        self._target = key
        self._state = _BEFORE_OBJECT
        self._decoder = _StringDecoder()
        self._key_chars: list[str] = []
        self._key_is_target = False
        self._seen_target = False
        self._nest_depth = 0
        self._nest_in_string = False
        self._nest_escape = False
        self._text: list[str] = []
        self._value_kind: str | None = None  # "string" | "null" | "other", once the target's value is known to be complete
        self._primitive: list[str] = []
        self._in_target_primitive = False

    @property
    def resolved(self) -> bool:
        """The target key's value is fully known: a finished string, or a non-string (null, a number, an object...).

        A caller that only needs to know WHAT the field turned out to be (never to forward it) waits for this: a string is read from `text`;
        anything else - including `null` - is `value_kind` "null" or "other".
        """
        return self._value_kind is not None

    @property
    def value_kind(self) -> str | None:
        return self._value_kind

    @property
    def text(self) -> str:
        """Everything decoded so far."""
        return "".join(self._text)

    @property
    def finished(self) -> bool:
        """The closing quote of the value was seen."""
        return self._state == _DONE and self._seen_target

    @property
    def failed(self) -> bool:
        return self._state == _FAILED

    def feed(self, fragment: str) -> str:
        """Consume a fragment; return the newly decoded characters of the value (possibly empty)."""
        if self._state in (_DONE, _FAILED) or not fragment:
            return ""
        out: list[str] = []
        for ch in fragment:
            if self._state in (_DONE, _FAILED):
                break
            self._step(ch, out)
        emitted = "".join(out)
        if emitted:
            self._text.append(emitted)
        return emitted

    def _fail(self) -> None:
        self._state = _FAILED

    def _step(self, ch: str, out: list[str]) -> None:
        s = self._state
        if s == _BEFORE_OBJECT:
            if ch == "{":
                self._state = _EXPECT_KEY
            elif ch not in _WS:
                self._fail()
        elif s == _EXPECT_KEY:
            if ch == '"':
                self._key_chars = []
                self._decoder = _StringDecoder()
                self._state = _IN_KEY
            elif ch == "}":
                self._state = _DONE
            elif ch not in _WS and ch != ",":
                self._fail()
        elif s == _IN_KEY:
            text, closed = self._decoder.push(ch)
            if self._decoder.bad:
                self._fail()
            elif closed:
                self._key_chars.append(text)
                self._key_is_target = "".join(self._key_chars) == self._target
                self._state = _AFTER_KEY
            else:
                self._key_chars.append(text)
        elif s == _AFTER_KEY:
            if ch == ":":
                self._state = _AFTER_COLON
            elif ch not in _WS:
                self._fail()
        elif s == _AFTER_COLON:
            self._begin_value(ch)
        elif s == _IN_TARGET:
            text, closed = self._decoder.push(ch)
            if self._decoder.bad:
                self._fail()
                return
            if text:
                out.append(text)
            if closed:
                self._value_kind = "string"
                self._state = _DONE  # the value is complete: nothing after it matters
        elif s == _IN_OTHER_STRING:
            _text, closed = self._decoder.push(ch)
            if self._decoder.bad:
                self._fail()
            elif closed:
                self._state = _AFTER_VALUE
        elif s == _IN_NESTED:
            self._step_nested(ch)
        elif s == _IN_PRIMITIVE:
            if self._in_target_primitive:
                if ch in _WS or ch in ",}":
                    word = "".join(self._primitive)
                    self._value_kind = "null" if word == "null" else "other"
                    self._in_target_primitive = False
                    self._state = _DONE  # the target's value is known; nothing after it matters
                else:
                    self._primitive.append(ch)
            elif ch == ",":
                self._state = _EXPECT_KEY
            elif ch == "}":
                self._state = _DONE
        elif s == _AFTER_VALUE:
            if ch == ",":
                self._state = _EXPECT_KEY
            elif ch == "}":
                self._state = _DONE
            elif ch not in _WS:
                self._fail()

    def _begin_value(self, ch: str) -> None:
        if ch in _WS:
            return
        if ch == '"':
            self._decoder = _StringDecoder()
            if self._key_is_target and not self._seen_target:
                self._seen_target = True  # only the first occurrence of the key is the reply
                self._state = _IN_TARGET
            else:
                self._state = _IN_OTHER_STRING
        elif ch in "{[":
            if self._key_is_target and not self._seen_target:
                self._seen_target = True
                self._value_kind = "other"  # an object/array under the target key: known to be not a string, contents skipped
            self._nest_depth = 1
            self._nest_in_string = False
            self._nest_escape = False
            self._state = _IN_NESTED
        elif ch in "}]":
            self._fail()
        else:
            if self._key_is_target and not self._seen_target:
                self._seen_target = True
                self._in_target_primitive = True
                self._primitive = [ch]
            self._state = _IN_PRIMITIVE

    def _step_nested(self, ch: str) -> None:
        """Skip an object/array value, honouring strings so a bracket inside one does not end it."""
        if self._nest_in_string:
            if self._nest_escape:
                self._nest_escape = False
            elif ch == "\\":
                self._nest_escape = True
            elif ch == '"':
                self._nest_in_string = False
            return
        if ch == '"':
            self._nest_in_string = True
        elif ch in "{[":
            self._nest_depth += 1
        elif ch in "}]":
            self._nest_depth -= 1
            if self._nest_depth == 0:
                self._state = _AFTER_VALUE
