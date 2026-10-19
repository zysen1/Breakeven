"""A tiny boolean expression language for state assertions.

The language is intentionally not Python. There are no calls, no attributes,
and no names other than the case fields. That keeps a customer YAML file from
becoming an execution sink when someone opens a dossier on a laptop that also
holds production data.
"""

from __future__ import annotations

from dataclasses import dataclass


class DSLError(ValueError):
    pass


@dataclass
class _Tok:
    kind: str
    value: object


_OPS = {
    "==": "EQ",
    "!=": "NE",
    "<=": "LE",
    ">=": "GE",
    "<": "LT",
    ">": "GT",
}


class _Lexer:
    def __init__(self, source: str) -> None:
        self.source = source
        self.i = 0

    def tokens(self) -> list[_Tok]:
        out: list[_Tok] = []
        while self.i < len(self.source):
            ch = self.source[self.i]
            if ch.isspace():
                self.i += 1
                continue
            if ch in "()[]":
                out.append(_Tok(ch, ch))
                self.i += 1
                continue
            if ch in ",":
                out.append(_Tok("COMMA", ch))
                self.i += 1
                continue
            two = self.source[self.i : self.i + 2]
            if two in ("==", "!=", "<=", ">=", "in"):
                # "in" is only an operator when it is a whole word.
                if two == "in" and self._ident_continues():
                    out.append(self._word())
                    continue
                if two == "in":
                    self.i += 2
                    out.append(_Tok("IN", "in"))
                    continue
                self.i += 2
                out.append(_Tok(_OPS[two], two))
                continue
            if ch in "<>":
                self.i += 1
                out.append(_Tok(_OPS[ch], ch))
                continue
            if ch in "'\"":
                out.append(self._string(ch))
                continue
            if ch.isdigit() or (ch == "." and self._peek_digit()) or (
                ch == "-" and self._peek_digit()
            ):
                out.append(self._number())
                continue
            if ch.isalpha() or ch == "_":
                out.append(self._word())
                continue
            raise DSLError(f"unexpected character {ch!r} in {self.source!r}")
        out.append(_Tok("EOF", None))
        return out

    def _ident_continues(self) -> bool:
        j = self.i + 2
        return j < len(self.source) and (self.source[j].isalnum() or self.source[j] == "_")

    def _peek_digit(self) -> bool:
        j = self.i + 1
        return j < len(self.source) and self.source[j].isdigit()

    def _string(self, quote: str) -> _Tok:
        self.i += 1
        start = self.i
        while self.i < len(self.source) and self.source[self.i] != quote:
            self.i += 1
        if self.i >= len(self.source):
            raise DSLError("unterminated string")
        value = self.source[start : self.i]
        self.i += 1
        return _Tok("STRING", value)

    def _number(self) -> _Tok:
        start = self.i
        if self.source[self.i] == "-":
            self.i += 1
        while self.i < len(self.source) and (self.source[self.i].isdigit() or self.source[self.i] == "."):
            self.i += 1
        raw = self.source[start : self.i]
        value: int | float = float(raw) if "." in raw else int(raw)
        return _Tok("NUMBER", value)

    def _word(self) -> _Tok:
        start = self.i
        while self.i < len(self.source) and (self.source[self.i].isalnum() or self.source[self.i] == "_"):
            self.i += 1
        word = self.source[start : self.i]
        if word in {"and", "or", "not"}:
            return _Tok(word.upper(), word)
        if word == "in":
            return _Tok("IN", word)
        if word == "true":
            return _Tok("BOOL", True)
        if word == "false":
            return _Tok("BOOL", False)
        return _Tok("NAME", word)


class _Parser:
    def __init__(self, tokens: list[_Tok]) -> None:
        self.tokens = tokens
        self.i = 0

    def parse(self):
        node = self._or()
        if self._peek().kind != "EOF":
            raise DSLError(f"trailing input near {self._peek().value!r}")
        return node

    def _peek(self) -> _Tok:
        return self.tokens[self.i]

    def _pop(self) -> _Tok:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def _or(self):
        node = self._and()
        while self._peek().kind == "OR":
            self._pop()
            node = ("or", node, self._and())
        return node

    def _and(self):
        node = self._not()
        while self._peek().kind == "AND":
            self._pop()
            node = ("and", node, self._not())
        return node

    def _not(self):
        if self._peek().kind == "NOT":
            self._pop()
            return ("not", self._not())
        return self._cmp()

    def _cmp(self):
        node = self._primary()
        kinds = {"EQ", "NE", "LT", "GT", "LE", "GE", "IN"}
        while self._peek().kind in kinds:
            op = self._pop().kind
            if op == "IN":
                node = ("in", node, self._list_or_primary())
            else:
                node = (op.lower(), node, self._primary())
        return node

    def _list_or_primary(self):
        if self._peek().kind == "[":
            return self._list()
        return self._primary()

    def _list(self):
        self._pop()
        items = []
        if self._peek().kind != "]":
            items.append(self._primary())
            while self._peek().kind == "COMMA":
                self._pop()
                items.append(self._primary())
        if self._pop().kind != "]":
            raise DSLError("expected ]")
        return ("list", items)

    def _primary(self):
        tok = self._pop()
        if tok.kind == "(":
            node = self._or()
            if self._pop().kind != ")":
                raise DSLError("expected )")
            return node
        if tok.kind in {"NUMBER", "STRING", "BOOL"}:
            return ("lit", tok.value)
        if tok.kind == "NAME":
            return ("name", tok.value)
        raise DSLError(f"expected a value, found {tok.value!r}")


def compile_expr(source: str):
    source = source.strip()
    if not source:
        raise DSLError("empty expression")
    tokens = _Lexer(source).tokens()
    return _Parser(tokens).parse()


def evaluate(node, context: dict) -> object:
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "name":
        name = node[1]
        if name not in context:
            raise DSLError(f"unknown field {name!r}")
        return context[name]
    if kind == "list":
        return [evaluate(item, context) for item in node[1]]
    if kind == "not":
        return not _truth(evaluate(node[1], context))
    if kind == "and":
        return _truth(evaluate(node[1], context)) and _truth(evaluate(node[2], context))
    if kind == "or":
        return _truth(evaluate(node[1], context)) or _truth(evaluate(node[2], context))
    left = evaluate(node[1], context)
    right = evaluate(node[2], context)
    if kind == "eq":
        return left == right
    if kind == "ne":
        return left != right
    if kind == "lt":
        return left < right
    if kind == "gt":
        return left > right
    if kind == "le":
        return left <= right
    if kind == "ge":
        return left >= right
    if kind == "in":
        return left in right
    raise DSLError(f"unknown node {kind}")


def _truth(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise DSLError(f"expected a boolean, found {value!r}")


def eval_expr(source: str, context: dict) -> bool:
    return bool(_truth(evaluate(compile_expr(source), context)))
