"""Normalize extracted chapter text and split it into TTS-sized chunks."""

import re

# Lines that are just a page number, possibly decorated ("- 42 -", "[42]").
_PAGE_NUMBER = re.compile(r"^\s*[-–—\[\(]*\s*\d{1,4}\s*[-–—\]\)]*\s*$")
# Bracketed footnote references left inline: [1], [note 3], (12).
_FOOTNOTE_REF = re.compile(r"\[\s*(?:note\s*)?\d{1,3}\s*\]")
# Note: no "…" here on purpose — an ellipsis is usually an *omission* inside a
# sentence ("same Spirit … same Lord"), not a full stop. Treating it as a
# boundary shredded such quotes into fragments; see normalize_for_speech.
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"'”’\)]*\s+")

# A period is only sometimes a full stop. Splitting on every one of them cut
# "Dr. Stuart Scott" into "Dr." + "Stuart Scott…", and a chunk that ends on a
# bare "Dr." gives the model a fragment no sentence in its training data ends
# with — it fills the gap by inventing a word. That is where the phantom
# "Dr. carry Stuart Scott" came from. "J. I. Packer" and "Rom. 12:4" broke the
# same way.
_ABBREVIATIONS = {
    # Titles and names
    "dr", "mr", "mrs", "ms", "prof", "rev", "fr", "sr", "jr", "st", "hon",
    # Everyday
    "vs", "etc", "eg", "ie", "cf", "no", "vol", "vols", "ch", "chap", "chaps",
    "pp", "fig", "figs", "approx", "est", "dept", "univ", "co", "inc", "ltd",
    "ed", "eds", "trans", "ibid", "al",
    "am", "pm", "ad", "bc", "ca", "circa",
    # Books of the Bible — this library is full of them, and a citation split
    # mid-reference is both wrong and unreadable aloud.
    "gen", "ex", "exod", "lev", "num", "deut", "josh", "judg", "sam", "kgs",
    "kings", "chron", "ezra", "neh", "esth", "ps", "pss", "prov", "eccl",
    "song", "isa", "jer", "lam", "ezek", "dan", "hos", "obad", "mic", "nah",
    "hab", "zeph", "hag", "zech", "mal", "matt", "mk", "lk", "jn", "rom",
    "cor", "gal", "eph", "phil", "col", "thess", "tim", "titus", "phlm",
    "heb", "jas", "pet", "jude", "rev",
}

# The word sitting immediately before the period, allowing internal dots so
# "a.m." and "U.S." arrive as one token rather than as a bare trailing letter.
_WORD_BEFORE_DOT = re.compile(r"([A-Za-z](?:[A-Za-z.]*[A-Za-z])?)\.$")

# A whole "sentence" that is only a numbered-list marker: "1.", "(3).", "iv.".
_LIST_MARKER = re.compile(r"^[\(\[]?(?:\d{1,3}|[ivxlIVXL]{1,5})[\)\].]{1,2}$")


def _is_false_break(head: str, tail: str) -> bool:
    """True when a candidate sentence break is really mid-sentence."""
    if not head.endswith("."):
        return False  # '!' and '?' are not abbreviations
    # A continuation starting lower-case is almost never a new sentence.
    if tail[:1].islower():
        return True
    # A bare list marker — "1.", "10.", "(3)." — belongs to the item that
    # follows it, not to the sentence before. Matching only when the marker is
    # the *whole* head keeps "he was born in 1985. Later…" splitting normally.
    if _LIST_MARKER.match(head.strip()):
        return True
    match = _WORD_BEFORE_DOT.search(head)
    if not match:
        return False
    word = match.group(1).replace(".", "")
    # A lone letter is an initial — "J. I. Packer", "C. S. Lewis".
    if len(word) == 1:
        return True
    return word.lower() in _ABBREVIATIONS


def split_sentences(paragraph: str) -> list[str]:
    """Split into sentences, keeping abbreviations and initials intact."""
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(paragraph):
        head = paragraph[start:match.start()]
        if _is_false_break(head, paragraph[match.end():]):
            continue
        if head.strip():
            sentences.append(head.strip())
        start = match.end()
    if paragraph[start:].strip():
        sentences.append(paragraph[start:].strip())
    return sentences

# Two or more periods, space-separated or not (". . .", "...", ".."), i.e. an
# ellipsis however it was typeset. Space is [ \t] only so it can't swallow a
# real full stop across a line break.
_ELLIPSIS = re.compile(r"\.(?:[ \t]*\.){1,}")


def normalize_for_speech(text: str) -> str:
    """Rewrite punctuation Chatterbox mishandles into forms it was trained on.

    Chatterbox is autoregressive: on input that rarely appears in training it
    doesn't fail cleanly, it hallucinates plausible-sounding audio ("d e though
    s"). Two constructs trigger this reliably:

      * Spaced or repeated periods (". . .", "...") — a run of bare full stops
        with nothing to pronounce between them. Collapsed to a single "…",
        which the model treats as one clean pause.
      * The semicolon — far rarer in training than the comma, and doubly so
        next to an ellipsis ("Spirit; . . ."). Softened to a comma, which reads
        as the same list-like pause the author intended.

    This is deliberately lossy: "…" is a slightly different pause than ". . ."
    and a comma a lighter one than ";". For narration that is an improvement,
    but it means the spoken text is not a character-perfect echo of the page —
    which matters if read-along is built on top of it.
    """
    text = _ELLIPSIS.sub("…", text)
    text = text.replace(";", ",")
    # An ellipsis hard against the previous word ("Lord…same") gets a space so
    # the model sees a token boundary, not a fused non-word.
    text = re.sub(r"(?<=\w)…", " …", text)
    return text


# A parenthetical is a bare citation when it is nothing but a reference: a
# scripture cite ("1 Cor. 12:4–6"), a cross-reference ("see p. 42", "cf.",
# "ibid."), a verse pointer ("v. 21", "vv. 1–11"), or a year/number. Spoken
# aloud these are the worst of both worlds — disruptive to the ear *and* the
# exact numeric/colon soup Chatterbox garbles — so they are candidates to drop.
# The guard is deliberately narrow: anything with real prose in it is kept.
_CITATION = re.compile(
    r"""^\s*(?:
        (?:see\s+|cf\.?\s*|ibid\.?|e\.g\.?|i\.e\.?|vol\.?|chap\.?|pp?\.|vv?\.)  # ref lead-ins
      | (?:\d+\s*[:.]\s*\d+)                                # 12:4 verse form
      | (?:[1-3]?\s*[A-Z][a-z]{1,5}\.?\s*\d)                # 1 Cor. 12 / Eph. 4
      | (?:AD|BC|c\.|ca\.)?\s*\d{1,4}(?:[–-]\d{1,4})?       # years, ranges
    )""",
    re.VERBOSE,
)
# But never drop a parenthetical carrying a real clause: three or more ordinary
# words (letters only, 3+ chars) means it is prose, not a citation.
_HAS_PROSE = re.compile(r"(?:\b[A-Za-z]{3,}\b[^A-Za-z0-9]*){3,}")


_PARENTHETICAL = re.compile(r"\(([^()]{1,90})\)")


def _is_citation(inner: str) -> bool:
    """Whether a parenthetical's contents are a bare reference and nothing more.

    The single source of truth for both dropping and previewing. The UI shows
    the user exactly what will be removed, so the two must never be able to
    disagree — a preview computed from a second, slightly different rule would
    be worse than no preview.
    """
    if _HAS_PROSE.search(inner) and not _CITATION.match(inner):
        return False               # prose aside — keep verbatim
    return bool(_CITATION.match(inner))


def _pre_clean(text: str) -> str:
    """Everything that happens before citations are considered.

    Shared so `find_citations` sees the same string the stripper will, rather
    than offsets computed against the raw, un-de-footnoted text.
    """
    lines = [line for line in text.splitlines() if not _PAGE_NUMBER.match(line)]
    text = "\n".join(lines)
    text = _FOOTNOTE_REF.sub("", text)
    return text.replace("­", "")  # soft hyphens


def _strip_citations(text: str) -> str:
    text = _PARENTHETICAL.sub(
        lambda m: "" if _is_citation(m.group(1)) else m.group(0), text
    )
    # Collapse the space a removed citation leaves ("word (ref) ," -> "word,").
    return re.sub(r"\s+([,.;:])", r"\1", re.sub(r"[ \t]{2,}", " ", text))


def find_citations(text: str, context: int = 60) -> list[dict]:
    """Every parenthetical `drop_citations` would remove, with its surroundings.

    Lets the review screen say how many will go and show them in place, so the
    choice is made from the actual book rather than a guess.
    """
    text = _pre_clean(text)
    found: list[dict] = []
    for match in _PARENTHETICAL.finditer(text):
        if not _is_citation(match.group(1)):
            continue
        start, end = match.span()
        found.append(
            {
                "text": match.group(0),
                "before": " ".join(text[max(0, start - context) : start].split()),
                "after": " ".join(text[end : end + context].split()),
            }
        )
    return found


def clean_text(text: str, drop_citations: bool = False) -> str:
    text = _pre_clean(text)
    # Normalize before stripping, not after. _strip_citations tidies the gap a
    # removed reference leaves ("says (Rom. 12:4), we" -> "says, we") by
    # deleting whitespace before punctuation — which also ate the spaces inside
    # a typeset ellipsis, turning ". . ." into "..." and then ",…" instead of
    # ", …". Collapsing ellipses to a single character first puts them out of
    # that rule's reach.
    text = normalize_for_speech(text)
    if drop_citations:
        text = _strip_citations(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _fit(sentence: str, max_chars: int) -> list[str]:
    """Break one over-long sentence on commas or spaces as a last resort."""
    pieces = []
    while len(sentence) > max_chars:
        cut = sentence.rfind(",", 0, max_chars)
        if cut < max_chars // 2:
            cut = sentence.rfind(" ", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        pieces.append(sentence[:cut + 1].strip())
        sentence = sentence[cut + 1:].strip()
    if sentence:
        pieces.append(sentence)
    return pieces


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Pack whole sentences into chunks of at most max_chars.

    Chunks never span a paragraph break. Packing across one glued a signature
    line onto the end of the preceding paragraph — "…and what He blesses. John
    MacArthur" — and that trailing unterminated fragment made Chatterbox fail
    to stop: 28 seconds of audio for 192 characters, roughly triple what the
    text warranted, with the name lost somewhere inside it. The same text as
    two chunks renders in 11.9s and says the name. Adding a full stop did not
    help; only the split did.

    Respecting the boundary is also what the text means. Chunks are joined with
    a short pause, so a paragraph break now produces one, where before it was
    flattened to a single space mid-chunk.
    """
    chunks: list[str] = []
    for paragraph in text.split("\n\n"):
        paragraph = paragraph.replace("\n", " ").strip()
        if not paragraph:
            continue
        current = ""
        for sentence in split_sentences(paragraph):
            for piece in _fit(sentence, max_chars):
                if current and len(current) + len(piece) + 1 > max_chars:
                    chunks.append(current)
                    current = piece
                else:
                    current = f"{current} {piece}".strip()
        if current:
            chunks.append(current)
    return chunks
