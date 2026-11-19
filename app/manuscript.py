"""Turn an uploaded manuscript into plain text for the editing workflow.

The model reads Unicode prose. Word, OpenDocument, RTF, HTML, EPUB, and PDF
are converted. The stored manuscript is that text.
"""

from __future__ import annotations

import html
import io
import shutil
import subprocess
import tempfile
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

MAX_BYTES = 8 * 1024 * 1024
_ACCEPTED = ".txt, .md, .docx, .doc, .odt, .rtf, .pdf, .html, or .epub"

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"


def manuscript_text(filename: str, data: bytes) -> str:
    """Return the prose the workflow should read."""
    if not data:
        raise ValueError("upload a manuscript file")
    if len(data) > MAX_BYTES:
        raise ValueError("manuscript file is over 8 MB")
    ext = Path(filename or "").suffix.lower()
    readers = {
        ".txt": _plain,
        ".text": _plain,
        ".md": _plain,
        ".markdown": _plain,
        ".html": _html_bytes,
        ".htm": _html_bytes,
        ".docx": _docx,
        ".odt": _odt,
        ".rtf": _rtf,
        ".pdf": _pdf,
        ".epub": _epub,
        ".doc": _doc,
    }
    reader = readers.get(ext)
    if reader is None:
        raise ValueError(f"upload a manuscript as {_ACCEPTED}")
    text = _normalize(reader(data))
    if not text:
        raise ValueError("this file has no manuscript text")
    return text


def _normalize(text: str) -> str:
    text = text.replace("\x00", "").replace("\u00ad", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n\n")
    lines = [line.rstrip() for line in text.split("\n")]
    collapsed: list[str] = []
    blank = 0
    for line in lines:
        if line.strip():
            collapsed.append(line)
            blank = 0
        else:
            blank += 1
            if blank <= 2:
                collapsed.append("")
    return "\n".join(collapsed).strip()


def _plain(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8")
    except UnicodeError:
        return data.decode("cp1252", "replace")


def _html_bytes(data: bytes) -> str:
    return _html_text(_plain(data))


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._skip += 1
        if tag in {"p", "div", "br", "h1", "h2", "h3", "h4", "li", "tr", "blockquote"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
        if tag in {"p", "div", "h1", "h2", "h3", "h4", "li", "tr", "blockquote"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_text(raw: str) -> str:
    parser = _HTMLText()
    parser.feed(raw)
    return "".join(parser.parts)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _zip_xml(data: bytes, name: str) -> ElementTree.Element:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo(name)
            if info.file_size > 20_000_000:
                raise ValueError("this manuscript is too large to read")
            raw = archive.read(name)
    except zipfile.BadZipFile as exc:
        raise ValueError("this manuscript file could not be read") from exc
    except KeyError as exc:
        raise ValueError("this manuscript file could not be read") from exc
    return ElementTree.fromstring(raw)


def _docx(data: bytes) -> str:
    root = _zip_xml(data, "word/document.xml")
    paragraphs = []
    for paragraph in root.iter(_W + "p"):
        bits = []
        for node in paragraph.iter():
            if node.tag == _W + "t" and node.text:
                bits.append(node.text)
            elif node.tag == _W + "tab":
                bits.append("\t")
            elif node.tag == _W + "br":
                bits.append("\n")
        paragraphs.append("".join(bits))
    return "\n\n".join(part for part in paragraphs if part.strip())


def _odt_inline(node: ElementTree.Element) -> str:
    parts = [node.text or ""]
    for child in list(node):
        if child.tag == _TEXT + "line-break":
            parts.append("\n")
        elif child.tag == _TEXT + "tab":
            parts.append("\t")
        elif child.tag == _TEXT + "s":
            count = child.get(_TEXT + "c") or "1"
            parts.append(" " * max(1, int(count) if count.isdigit() else 1))
        else:
            parts.append(_odt_inline(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _odt(data: bytes) -> str:
    root = _zip_xml(data, "content.xml")
    paragraphs = []
    for node in root.iter():
        if node.tag in {_TEXT + "p", _TEXT + "h"}:
            paragraphs.append(_odt_inline(node))
    return "\n\n".join(part.strip() for part in paragraphs if part.strip())


def _rtf(data: bytes) -> str:
    raw = data.decode("latin-1", "replace")
    if "\\rtf" not in raw[:40]:
        raise ValueError("this RTF file is not readable")
    out: list[str] = []
    i = 0
    n = len(raw)
    depth = 0
    skip_at = None
    while i < n:
        ch = raw[i]
        if ch in "\r\n":
            i += 1
            continue
        if ch == "{":
            depth += 1
            i += 1
            if raw.startswith("\\*", i):
                skip_at = depth
                i += 2
            continue
        if ch == "}":
            if skip_at is not None and depth == skip_at:
                skip_at = None
            depth = max(0, depth - 1)
            i += 1
            continue
        if ch != "\\":
            if skip_at is None:
                out.append(ch)
            i += 1
            continue
        i += 1
        if i >= n:
            break
        if raw[i] == "'":
            token = raw[i + 1 : i + 3]
            i += 3
            if skip_at is None:
                try:
                    out.append(bytes([int(token, 16)]).decode("cp1252"))
                except ValueError:
                    pass
            continue
        if not raw[i].isalpha():
            symbol = raw[i]
            i += 1
            if skip_at is None and symbol in "{}\\":
                out.append(symbol)
            continue
        end = i
        while end < n and raw[end].isalpha():
            end += 1
        word = raw[i:end]
        i = end
        sign = 1
        if i < n and raw[i] in "+-":
            sign = -1 if raw[i] == "-" else 1
            i += 1
        num_end = i
        while num_end < n and raw[num_end].isdigit():
            num_end += 1
        number = int(raw[i:num_end]) * sign if num_end > i else None
        i = num_end
        if i < n and raw[i] == " ":
            i += 1
        if word in {
            "fonttbl",
            "colortbl",
            "stylesheet",
            "info",
            "pict",
            "object",
            "header",
            "footer",
            "headerl",
            "headerr",
            "footerl",
            "footerr",
        }:
            skip_at = depth
            continue
        if skip_at is not None:
            continue
        if word in {"par", "line", "page"}:
            out.append("\n")
        elif word == "tab":
            out.append("\t")
        elif word == "emdash":
            out.append("—")
        elif word == "endash":
            out.append("–")
        elif word == "lquote":
            out.append("‘")
        elif word == "rquote":
            out.append("’")
        elif word == "ldblquote":
            out.append("“")
        elif word == "rdblquote":
            out.append("”")
        elif word == "u" and number is not None:
            code = number + 65536 if number < 0 else number
            out.append(chr(code))
            i = _skip_rtf_fallback(raw, i)
    return "".join(out)


def _skip_rtf_fallback(raw: str, i: int) -> int:
    """Drop the one ANSI stand-in that follows a \\u character."""
    if i >= len(raw) or raw[i] in "{}":
        return i
    if raw[i] != "\\":
        return i + 1
    if raw.startswith("\\'", i):
        return i + 4
    i += 1
    while i < len(raw) and raw[i].isalpha():
        i += 1
    if i < len(raw) and raw[i] in "+-":
        i += 1
    while i < len(raw) and raw[i].isdigit():
        i += 1
    if i < len(raw) and raw[i] == " ":
        i += 1
    return i


def _pdf(data: bytes) -> str:
    extracted = ""
    if shutil.which("pdftotext"):
        extracted = _pdftotext(data)
    if not extracted.strip():
        extracted = _pdf_builtin(data)
    if not extracted.strip():
        raise ValueError("this PDF has no extractable text. Upload a .docx or .txt file instead.")
    return extracted


def _pdftotext(data: bytes) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", "-q", "-", "-"],
            input=data,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", "replace")


def _pdf_streams(data: bytes) -> list[bytes]:
    streams = []
    marker = b"stream"
    start = 0
    while True:
        at = data.find(marker, start)
        if at < 0:
            break
        header = data[max(0, at - 300) : at]
        begin = at + len(marker)
        if data[begin : begin + 2] == b"\r\n":
            begin += 2
        elif begin < len(data) and data[begin : begin + 1] in (b"\n", b"\r"):
            begin += 1
        end = data.find(b"endstream", begin)
        if end < 0:
            break
        chunk = data[begin:end].rstrip(b"\r\n")
        if b"/FlateDecode" in header:
            import zlib

            try:
                chunk = zlib.decompress(chunk)
            except zlib.error:
                chunk = b""
        streams.append(chunk)
        start = end + len(b"endstream")
    return streams


def _pdf_builtin(data: bytes) -> str:
    pieces = []
    for stream in _pdf_streams(data):
        pieces.extend(_pdf_show_strings(stream))
    return "\n".join(pieces)


def _pdf_show_strings(stream: bytes) -> list[str]:
    """Pull strings that a page actually shows with Tj, TJ, ', or \"."""
    lines: list[str] = []
    current: list[str] = []
    i = 0
    n = len(stream)

    def take_string(pos: int) -> tuple[str, int]:
        if pos >= n:
            return "", pos
        if stream[pos : pos + 1] == b"(":
            return _pdf_literal(stream, pos)
        if stream[pos : pos + 1] == b"<":
            return _pdf_hex(stream, pos)
        return "", pos

    while i < n:
        byte = stream[i : i + 1]
        if byte in b" \t\r\n":
            i += 1
            continue
        if byte == b"%":
            nxt = stream.find(b"\n", i)
            i = n if nxt < 0 else nxt + 1
            continue
        if byte in b"(<":
            text, i = take_string(i)
            shown, i = _pdf_string_is_shown(stream, i)
            if shown and text:
                current.append(text)
            continue
        if byte == b"[":
            text, i = _pdf_array_text(stream, i)
            shown, i = _pdf_string_is_shown(stream, i)
            if shown and text:
                current.append(text)
            continue
        if byte == b"/":
            i += 1
            _, i = _pdf_word(stream, i)
            continue
        word, nxt = _pdf_word(stream, i)
        if nxt == i:
            i += 1
            continue
        i = nxt
        if word in {b"T*", b"Td", b"TD"}:
            if current:
                lines.append("".join(current))
                current = []
        elif word in {b"'", b'"'}:
            if current:
                lines.append("".join(current))
                current = []
    if current:
        lines.append("".join(current))
    return lines


def _pdf_string_is_shown(stream: bytes, i: int) -> tuple[bool, int]:
    j = i
    while j < len(stream) and stream[j : j + 1] in b" \t\r\n":
        j += 1
    word, after = _pdf_word(stream, j)
    if word in {b"Tj", b"TJ", b"'", b'"'}:
        return True, after
    return False, i


def _pdf_word(stream: bytes, i: int) -> tuple[bytes, int]:
    start = i
    while i < len(stream) and stream[i : i + 1] not in b" \t\r\n()<>[]{}/%":
        i += 1
    return stream[start:i], i


def _pdf_literal(stream: bytes, i: int) -> tuple[str, int]:
    i += 1
    raw = bytearray()
    depth = 1
    while i < len(stream) and depth:
        char = stream[i]
        i += 1
        if char == 0x5C:  # backslash
            if i >= len(stream):
                break
            esc = stream[i]
            i += 1
            mapping = {ord("n"): "\n", ord("r"): "\r", ord("t"): "\t", ord("b"): "\b", ord("f"): "\f", ord("("): "(", ord(")"): ")", ord("\\"): "\\"}
            if esc in mapping:
                raw.extend(mapping[esc].encode("latin-1"))
            elif 48 <= esc <= 55:
                octal = bytes([esc])
                for _ in range(2):
                    if i < len(stream) and 48 <= stream[i] <= 55:
                        octal += bytes([stream[i]])
                        i += 1
                raw.append(int(octal, 8) & 0xFF)
            continue
        if char == 0x28:
            depth += 1
        elif char == 0x29:
            depth -= 1
            if depth == 0:
                break
        raw.append(char)
    return _pdf_decode_text(bytes(raw)), i


def _pdf_hex(stream: bytes, i: int) -> tuple[str, int]:
    end = stream.find(b">", i + 1)
    if end < 0:
        return "", len(stream)
    hex_text = bytes(ch for ch in stream[i + 1 : end] if ch not in b" \t\r\n")
    if len(hex_text) % 2:
        hex_text += b"0"
    try:
        raw = bytes(int(hex_text[pos : pos + 2], 16) for pos in range(0, len(hex_text), 2))
    except ValueError:
        raw = b""
    return _pdf_decode_text(raw), end + 1


def _pdf_array_text(stream: bytes, i: int) -> tuple[str, int]:
    i += 1
    parts = []
    while i < len(stream) and stream[i : i + 1] != b"]":
        if stream[i : i + 1] in b" \t\r\n":
            i += 1
            continue
        if stream[i : i + 1] in b"(<":
            text, i = (_pdf_literal if stream[i : i + 1] == b"(" else _pdf_hex)(stream, i)
            parts.append(text)
            continue
        i += 1
    if i < len(stream) and stream[i : i + 1] == b"]":
        i += 1
    return "".join(parts), i


def _pdf_decode_text(raw: bytes) -> str:
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", "replace")
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", "replace")
    return raw.decode("latin-1", "replace")


def _epub(data: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("this EPUB could not be read") from exc
    with archive:
        names = set(archive.namelist())
        ordered = _epub_spine(archive, names)
        if not ordered:
            ordered = sorted(name for name in names if name.lower().endswith((".xhtml", ".html", ".htm")))
        parts = []
        for name in ordered:
            if name not in names or ".." in name.split("/"):
                continue
            info = archive.getinfo(name)
            if info.file_size > 5_000_000:
                continue
            parts.append(_html_text(archive.read(name).decode("utf-8", "replace")))
    return "\n\n".join(part.strip() for part in parts if part.strip())


def _epub_spine(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    if "META-INF/container.xml" not in names:
        return []
    root = ElementTree.fromstring(archive.read("META-INF/container.xml"))
    opf_name = ""
    for node in root.iter():
        if _local(node.tag) == "rootfile":
            opf_name = node.attrib.get("full-path", "")
            break
    if not opf_name or opf_name not in names:
        return []
    opf = ElementTree.fromstring(archive.read(opf_name))
    base = str(Path(opf_name).parent)
    if base == ".":
        base = ""
    hrefs = {}
    spine = []
    for node in opf.iter():
        kind = _local(node.tag)
        if kind == "item":
            hrefs[node.attrib.get("id", "")] = node.attrib.get("href", "")
        elif kind == "itemref":
            spine.append(node.attrib.get("idref", ""))
    ordered = []
    for item_id in spine:
        href = hrefs.get(item_id, "")
        if not href:
            continue
        href = html.unescape(href.split("#", 1)[0])
        path = f"{base}/{href}" if base else href
        ordered.append(path)
    return ordered


def _doc(data: bytes) -> str:
    soffice = shutil.which("soffice")
    if not soffice:
        raise ValueError("save .doc files as .docx and upload that")
    with tempfile.TemporaryDirectory(prefix="manuscript-") as tmp:
        folder = Path(tmp)
        source = folder / "manuscript.doc"
        profile = folder / "profile"
        source.write_bytes(data)
        try:
            result = subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation={profile.as_uri()}",
                    "--headless",
                    "--norestore",
                    "--nofirststartwizard",
                    "--convert-to",
                    "txt:Text",
                    "--outdir",
                    str(folder),
                    str(source),
                ],
                capture_output=True,
                timeout=45,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("this .doc file could not be read. Save it as .docx and upload that.") from exc
        converted = folder / "manuscript.txt"
        if result.returncode != 0 or not converted.exists():
            raise ValueError("this .doc file could not be read. Save it as .docx and upload that.")
        return converted.read_text(encoding="utf-8", errors="replace")
