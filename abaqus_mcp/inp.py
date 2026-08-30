"""A minimal, edit-friendly model of an Abaqus keyword input deck.

Abaqus decks are line-oriented: ``*KEYWORD, PARAM=value`` lines each own the
data lines that follow until the next keyword. ``**`` lines are comments. This
module parses a deck into ordered blocks, exposes the names it defines and
references, and supports the surgical edits the fix engine performs (add a
parameter, correct a mistyped name, tweak a step's increment controls, insert a
block). Round-trips unknown constructs verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple, Union


@dataclass
class Block:
    keyword: str                       # upper-case, no leading '*'
    params: List[Tuple[str, Optional[str]]] = field(default_factory=list)
    data_lines: List[str] = field(default_factory=list)
    is_comment: bool = False
    raw_keyword: str = ""              # original keyword line (comments: the text)

    def param(self, key: str) -> Optional[str]:
        key = key.upper()
        for k, v in self.params:
            if k == key:
                return v
        return None

    def has_param(self, key: str) -> bool:
        key = key.upper()
        return any(k == key for k, _ in self.params)

    def set_param(self, key: str, value: Optional[str]) -> None:
        key = key.upper()
        for i, (k, _) in enumerate(self.params):
            if k == key:
                self.params[i] = (key, value)
                return
        self.params.append((key, value))

    def render_keyword_line(self) -> str:
        if self.is_comment:
            return self.raw_keyword
        parts = ["*" + self.keyword]
        for k, v in self.params:
            parts.append(k if v is None else "%s=%s" % (k, v))
        return ", ".join(parts)

    def render(self) -> str:
        lines = [self.render_keyword_line()]
        lines.extend(self.data_lines)
        return "\n".join(lines)


def _parse_keyword_line(line: str) -> Tuple[str, List[Tuple[str, Optional[str]]]]:
    # Split on commas that are not inside the (rare) quoted names.
    body = line.lstrip()[1:]  # drop leading '*'
    tokens = [t.strip() for t in body.split(",")]
    keyword = tokens[0].upper()
    params: List[Tuple[str, Optional[str]]] = []
    for tok in tokens[1:]:
        if not tok:
            continue
        if "=" in tok:
            k, v = tok.split("=", 1)
            params.append((k.strip().upper(), v.strip()))
        else:
            params.append((tok.upper(), None))
    return keyword, params


class Deck:
    def __init__(self, blocks: List[Block]):
        self.blocks = blocks

    # ---- construction -------------------------------------------------
    @classmethod
    def parse(cls, text: str) -> "Deck":
        blocks: List[Block] = []
        current: Optional[Block] = None
        for raw in text.splitlines():
            stripped = raw.strip()
            if stripped.startswith("**"):
                blocks.append(Block(keyword="", is_comment=True, raw_keyword=raw))
                current = None
                continue
            if stripped.startswith("*"):
                keyword, params = _parse_keyword_line(stripped)
                current = Block(keyword=keyword, params=params, raw_keyword=raw)
                blocks.append(current)
                continue
            # data line
            if current is None:
                # Stray data before any keyword; keep as comment to preserve.
                blocks.append(Block(keyword="", is_comment=True, raw_keyword=raw))
            else:
                current.data_lines.append(raw)
        return cls(blocks)

    @classmethod
    def load(cls, path: Union[str, Path]) -> "Deck":
        return cls.parse(Path(path).read_text(errors="replace"))

    def render(self) -> str:
        return "\n".join(b.render() for b in self.blocks) + "\n"

    def save(self, path: Union[str, Path]) -> None:
        Path(path).write_text(self.render())

    # ---- queries ------------------------------------------------------
    def find(self, keyword: str) -> List[Block]:
        keyword = keyword.upper()
        return [b for b in self.blocks if not b.is_comment and b.keyword == keyword]

    def first(self, keyword: str) -> Optional[Block]:
        found = self.find(keyword)
        return found[0] if found else None

    def defined_names(self) -> dict:
        """Map of category -> set of names the deck defines."""
        names = {"nset": set(), "elset": set(), "surface": set(), "material": set()}
        for b in self.blocks:
            if b.is_comment:
                continue
            if b.keyword == "NSET" and b.param("NSET"):
                names["nset"].add(b.param("NSET").upper())
            elif b.keyword == "ELSET" and b.param("ELSET"):
                names["elset"].add(b.param("ELSET").upper())
            # Sets are also implicitly defined via the NSET/ELSET param on
            # *NODE/*ELEMENT and other generating keywords.
            if b.param("NSET"):
                names["nset"].add(b.param("NSET").upper())
            if b.param("ELSET"):
                names["elset"].add(b.param("ELSET").upper())
            if b.keyword == "SURFACE" and b.param("NAME"):
                names["surface"].add(b.param("NAME").upper())
            if b.keyword == "MATERIAL" and b.param("NAME"):
                names["material"].add(b.param("NAME").upper())
        return names

    # ---- edits --------------------------------------------------------
    def index_of(self, block: Block) -> int:
        return self.blocks.index(block)

    def insert_after(self, block: Block, new_text: str) -> None:
        idx = self.index_of(block) + 1
        for off, b in enumerate(Deck.parse(new_text).blocks):
            self.blocks.insert(idx + off, b)

    def insert_before(self, block: Block, new_text: str) -> None:
        idx = self.index_of(block)
        for off, b in enumerate(Deck.parse(new_text).blocks):
            self.blocks.insert(idx + off, b)

    def rename_token(self, old: str, new: str) -> int:
        """Replace whole-word ``old`` with ``new`` in params and data lines.

        Case-insensitive, whole token only. Returns the number of replacements.
        Used to fix a mistyped set/material name across every reference.
        """
        pat = re.compile(r"(?<![\w-])" + re.escape(old) + r"(?![\w-])", re.I)
        count = 0
        for b in self.blocks:
            if b.is_comment:
                # Still fix references hiding in raw comment? No -- leave comments.
                continue
            new_params = []
            for k, v in b.params:
                nk, n1 = pat.subn(new, k)
                if v is None:
                    new_params.append((nk, None))
                    count += n1
                else:
                    nv, n2 = pat.subn(new, v)
                    new_params.append((nk, nv))
                    count += n1 + n2
            b.params = new_params
            for i, dl in enumerate(b.data_lines):
                nl, n = pat.subn(new, dl)
                b.data_lines[i] = nl
                count += n
        return count
