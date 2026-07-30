"""EPUB → list of chapters (title + plain text) plus book metadata."""

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup
from ebooklib import epub, ITEM_DOCUMENT, ITEM_COVER, ITEM_IMAGE

# Spine items shorter than this are treated as front/back matter and skipped.
MIN_CHAPTER_CHARS = 200

# Many books style headings as <p class="heading11"> etc. rather than <h1>-<h6>.
HEADING_CLASS = re.compile(r"head|title|chapter", re.I)
# Longer than this and it's body text wearing a heading class, not a heading.
MAX_HEADING_CHARS = 100
# Cap for titles derived from opening text when a section has no heading at all.
MAX_DERIVED_CHARS = 60

# Sections whose titles look like front/back matter are excluded by default.
# The user can re-include any of them in the review step.
FRONT_MATTER = re.compile(
    r"^\s*(table of )?contents\b|^\s*(cover|copyright|colophon|title page|half title"
    r"|index|dedication|acknowledge?ments?|about the (author|publisher)|imprint"
    r"|advertisement|also by|praise for|front ?matter|back ?matter)\b",
    re.I,
)


@dataclass
class Chapter:
    title: str
    text: str
    # Where the title came from: "toc", "heading", "derived", or "generic".
    # Anything but "toc" is worth a human glance before a long conversion.
    source: str = "toc"
    # True when this looks like front/back matter rather than content.
    front_matter: bool = False
    # The same content with its markup kept, one entry per paragraph, heading,
    # list item or quotation. `text` is these joined by blank lines, so the two
    # stay in step and a chunk always falls inside a single block.
    blocks: list[dict] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass
class Book:
    title: str
    author: str
    chapters: list[Chapter] = field(default_factory=list)
    cover: bytes | None = None
    cover_ext: str = ".jpg"


def _block_text(el) -> str:
    """Text of one block, without inserting spaces between inline tags.

    A separator would split drop caps — <span>T</span><strong>HE ...</strong>
    is one word and must not become "T HE".
    """
    return re.sub(r"\s+", " ", el.get_text()).strip()


def _heading(soup) -> str | None:
    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        text = _block_text(el)
        if text:
            return text
    for el in soup.find_all(class_=HEADING_CLASS):
        text = _block_text(el)
        if text and len(text) <= MAX_HEADING_CHARS:
            return text
    return None


def _derive_title(text: str) -> str | None:
    """Last resort: name a section after its opening words."""
    first = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0].strip()
    if not first:
        return None
    if len(first) > MAX_DERIVED_CHARS:
        first = first[:MAX_DERIVED_CHARS].rsplit(" ", 1)[0] + "…"
    return first.rstrip(".")


# Inline markup worth keeping when the book is shown on screen. Everything else
# is unwrapped — its text survives, its tag does not.
SAFE_INLINE = {"em", "i", "strong", "b", "u", "span", "sup", "sub",
               "code", "small", "br", "q", "cite", "abbr", "a"}
# Only these attributes survive, and only on the tags that use them. Dropping
# the rest takes event handlers (onclick, onerror) and inline styles with it.
SAFE_ATTRS = {"a": {"href"}, "abbr": {"title"}}


def _sanitize(element) -> str:
    """The block's inner markup, reduced to what is safe to render.

    A book is an untrusted document — anyone can hand Vocalis an EPUB — and
    this HTML ends up inside the page. Whitelisting rather than blacklisting:
    unknown tags are unwrapped so their text still reads, and every attribute
    outside the short list goes, which is what removes onerror handlers and
    javascript: URLs without having to enumerate them.
    """
    from copy import copy

    clone = copy(element)
    for tag in clone.find_all(True):
        if tag.name not in SAFE_INLINE:
            tag.unwrap()
            continue
        allowed = SAFE_ATTRS.get(tag.name, set())
        for name in list(tag.attrs):
            if name not in allowed:
                del tag[name]
        href = tag.get("href", "")
        if tag.name == "a" and not href.startswith(("http://", "https://", "#")):
            del tag["href"]
    return clone.decode_contents()


def _item_text(item) -> tuple[str | None, str, list[dict]]:
    """Title, the plain text to narrate, and the formatted blocks it came from.

    The plain text is unchanged — it is what the narrator reads, and altering
    it would invalidate every cached recording. The blocks are the same content
    with its markup intact, in the same order, so a reader can show italics,
    headings and verse rather than a flattened transcript.

    They line up exactly: the text is these blocks joined by blank lines, and
    chunking never crosses a blank line, so every chunk belongs to precisely
    one block. That is what lets a highlight follow the narration without any
    character-offset bookkeeping.
    """
    soup = BeautifulSoup(item.get_content(), "lxml")
    # Footnote markers and page-number anchors add noise when read aloud.
    for tag in soup.find_all(["sup", "script", "style"]):
        tag.decompose()
    for tag in soup.find_all(attrs={"epub:type": "pagebreak"}):
        tag.decompose()

    title = _heading(soup)

    body = soup.body or soup
    found = body.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li"])

    # A <blockquote> wrapping a <p> matches twice, and both carry the same
    # words — so quoted passages were being narrated twice over. Keep only the
    # outermost of any nested pair; its text already contains the inner one's.
    outermost = [e for e in found if not any(a in found for a in e.parents)]

    blocks: list[dict] = []
    for element in outermost:
        content = _block_text(element)
        if not content:
            continue
        blocks.append({
            "tag": element.name,
            # Sanitized inner markup: the wrapper is dropped so the reader
            # picks its own element, and anything not on the whitelist is
            # unwrapped rather than rendered.
            "html": _sanitize(element),
            "text": content,
        })

    text = "\n\n".join(b["text"] for b in blocks)
    if not text:
        text = _block_text(body)
        blocks = [{"tag": "p", "html": text, "text": text}] if text else []
    return title, text, blocks


def _toc_titles(eb: epub.EpubBook) -> dict[str, str]:
    """Map document paths (as in item.get_name()) to their TOC titles.

    The TOC nests as Links and (Section, children) tuples; entries may carry
    #fragments pointing into a file — the first entry for a file wins.
    """
    titles: dict[str, str] = {}

    def add(href: str | None, title: str | None) -> None:
        if href and title:
            titles.setdefault(href.split("#")[0], title.strip())

    def walk(entries) -> None:
        for entry in entries:
            if isinstance(entry, tuple):
                section, children = entry
                add(getattr(section, "href", None), getattr(section, "title", None))
                walk(children)
            else:
                add(getattr(entry, "href", None), getattr(entry, "title", None))

    walk(eb.toc or [])
    return titles


def _meta_cover(eb: epub.EpubBook):
    """EPUB2: <meta name="cover" content="<manifest-item-id>"/>."""
    for _value, attrs in eb.get_metadata("OPF", "meta") or []:
        if (attrs or {}).get("name") == "cover" and attrs.get("content"):
            item = eb.get_item_with_id(attrs["content"])
            if item is not None:
                return item
    return None


def _guide_cover(eb: epub.EpubBook):
    """Guide/landmark cover page — take the first image it displays."""
    for ref in getattr(eb, "guide", None) or []:
        if ref.get("type") != "cover" or not ref.get("href"):
            continue
        doc = eb.get_item_with_href(ref["href"].split("#")[0])
        if doc is None:
            continue
        soup = BeautifulSoup(doc.get_content(), "lxml")
        tag = soup.find("img") or soup.find("image")
        src = tag.get("src") or tag.get("xlink:href") if tag else None
        if not src:
            continue
        # src is relative to the document that references it.
        href = posixpath.normpath(posixpath.join(posixpath.dirname(doc.get_name()), src))
        image = eb.get_item_with_href(href)
        if image is not None:
            return image
    return None


def _named_cover(eb: epub.EpubBook):
    """Filename fallback. 'backcover.jpg' must never beat 'cover.jpg'."""
    def score(name: str) -> int:
        stem = Path(name).stem.lower()
        if stem == "cover":
            return 3
        if stem.startswith("cover"):
            return 2
        if "cover" in stem and "back" not in stem:
            return 1
        return 0

    candidates = [(score(i.get_name()), i) for i in eb.get_items_of_type(ITEM_IMAGE)]
    best = max(candidates, key=lambda c: c[0], default=(0, None))
    return best[1] if best[0] > 0 else None


def _cover(eb: epub.EpubBook) -> tuple[bytes | None, str]:
    for item in eb.get_items_of_type(ITEM_COVER):
        return item.get_content(), Path(item.get_name()).suffix or ".jpg"
    for finder in (_meta_cover, _guide_cover, _named_cover):
        item = finder(eb)
        if item is not None:
            return item.get_content(), Path(item.get_name()).suffix or ".jpg"
    return None, ".jpg"


def parse_epub(path: Path) -> Book:
    eb = epub.read_epub(str(path), options={"ignore_ncx": True})

    def meta(name: str) -> str:
        values = eb.get_metadata("DC", name)
        return values[0][0] if values else ""

    cover, cover_ext = _cover(eb)
    book = Book(
        title=meta("title") or path.stem,
        author=meta("creator") or "Unknown",
        cover=cover,
        cover_ext=cover_ext,
    )

    toc_titles = _toc_titles(eb)
    # TOC hrefs and item names are both OPF-relative, but some books disagree
    # on directory prefixes — fall back to basename matching.
    toc_by_basename = {Path(k).name: v for k, v in reversed(toc_titles.items())}

    docs = {item.get_name(): item for item in eb.get_items_of_type(ITEM_DOCUMENT)}
    for spine_id, _linear in eb.spine:
        item = eb.get_item_with_id(spine_id)
        if item is None or item.get_name() not in docs:
            continue
        heading, text, blocks = _item_text(item)
        if len(text) < MIN_CHAPTER_CHARS:
            continue
        name = item.get_name()
        toc_title = toc_titles.get(name) or toc_by_basename.get(Path(name).name)
        if toc_title:
            title, source = toc_title, "toc"
        elif heading:
            title, source = heading, "heading"
        elif derived := _derive_title(text):
            title, source = derived, "derived"
        else:
            title, source = f"Section {len(book.chapters) + 1}", "generic"

        book.chapters.append(
            Chapter(
                title=title,
                text=text,
                source=source,
                front_matter=bool(FRONT_MATTER.match(title)),
                blocks=blocks,
            )
        )

    return book
