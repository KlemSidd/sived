"""Convert common manuscript files into the prose the workflow reads."""

import io
import unittest
import zipfile
from html import escape
from unittest import mock

from app.manuscript import MAX_BYTES, _pdf_builtin, manuscript_text

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"


def _zip(parts: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _docx(*paragraphs: str) -> bytes:
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{escape(paragraph)}</w:t></w:r></w:p>'
        for paragraph in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>'
    )
    return _zip({"word/document.xml": document})


def _odt(*paragraphs: str) -> bytes:
    body = "".join(f"<text:p>{paragraph}</text:p>" for paragraph in paragraphs)
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<office:document-content xmlns:office="{_OFFICE}" xmlns:text="{_TEXT}">'
        f"<office:body><office:text>{body}</office:text></office:body>"
        "</office:document-content>"
    )
    return _zip({"content.xml": content})


def _epub(chapters: list[tuple[str, str]]) -> bytes:
    container = (
        '<?xml version="1.0"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    items = "".join(
        f'<item id="c{index}" href="{href}" media-type="application/xhtml+xml"/>'
        for index, (href, _page) in enumerate(chapters)
    )
    spine = "".join(f'<itemref idref="c{index}"/>' for index in range(len(chapters)))
    opf = (
        '<?xml version="1.0"?>'
        '<package xmlns="http://www.idpf.org/2007/opf">'
        f"<manifest>{items}</manifest><spine>{spine}</spine></package>"
    )
    parts: dict[str, str | bytes] = {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": container,
        "OEBPS/content.opf": opf,
    }
    for href, page in chapters:
        parts[f"OEBPS/{href}"] = page
    return _zip(parts)


def _pdf(stream: bytes) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


class ManuscriptTests(unittest.TestCase):
    def test_plain_text_markdown_and_windows_bytes(self):
        self.assertEqual(
            manuscript_text("chapter.txt", "The city kept its lights on.\n".encode()),
            "The city kept its lights on.",
        )
        self.assertEqual(
            manuscript_text("notes.md", b"# Harbor\n\nThe river does not return."),
            "# Harbor\n\nThe river does not return.",
        )
        self.assertEqual(
            manuscript_text("chapter.txt", "The caf\u00e9".encode("cp1252")),
            "The café",
        )
        self.assertEqual(
            manuscript_text("chapter.txt", b"\xef\xbb\xbfThe city kept its lights on."),
            "The city kept its lights on.",
        )
        soft = "The city\u00ad kept its lights on.\x00".encode()
        self.assertEqual(manuscript_text("chapter.txt", soft), "The city kept its lights on.")

    def test_html_drops_script_and_keeps_paragraphs(self):
        page = (
            "<html><body><p>The city kept its lights on.</p>"
            "<script>alert(1)</script>"
            "<p>The river does not return.</p></body></html>"
        )
        text = manuscript_text("chapter.html", page.encode())
        self.assertEqual(text, "The city kept its lights on.\n\nThe river does not return.")
        self.assertNotIn("alert", text)

    def test_docx_odt_and_rtf(self):
        docx = manuscript_text(
            "story.docx",
            _docx("The city kept its lights on.", "The river does not return."),
        )
        self.assertEqual(docx, "The city kept its lights on.\n\nThe river does not return.")
        odt = manuscript_text(
            "story.odt",
            _odt(
                "The city kept its lights on.<text:line-break/>The river does not return.",
                "A second paragraph.",
            ),
        )
        self.assertIn("The city kept its lights on.", odt)
        self.assertIn("The river does not return.", odt)
        self.assertIn("A second paragraph.", odt)
        rtf = (
            r"{\rtf1\ansi\deff0 {\fonttbl{\f0 Times;}} "
            r"\pard The caf\'e9 kept its lights on.\par "
            r"The river\u8217?s edge.\par}"
        ).encode("latin-1")
        text = manuscript_text("story.rtf", rtf)
        self.assertIn("The café kept its lights on.", text)
        self.assertIn("The river’s edge.", text)
        self.assertNotIn("Times", text)
        self.assertNotIn("?", text)

    def test_epub_follows_spine_order(self):
        book = _epub(
            [
                ("b.xhtml", "<p>Second harbor.</p><script>nope</script>"),
                ("a.xhtml", "<p>First lights.</p>"),
            ]
        )
        text = manuscript_text("story.epub", book)
        self.assertLess(text.index("Second harbor."), text.index("First lights."))
        self.assertNotIn("nope", text)

    def test_pdf_text_and_empty_pdf(self):
        stream = (
            b"BT /F1 12 Tf 72 720 Td (The city kept its lights on.) Tj "
            b"0 -24 Td (The river does not return.) Tj ET"
        )
        pdf = _pdf(stream)
        text = manuscript_text("story.pdf", pdf)
        self.assertIn("The city kept its lights on.", text)
        self.assertIn("The river does not return.", text)
        builtin = _pdf_builtin(pdf)
        self.assertIn("The city kept its lights on.", builtin)
        self.assertIn("The river does not return.", builtin)
        array_pdf = _pdf(b"BT [(The harbor counted every bell.)] TJ ET")
        self.assertIn("The harbor counted every bell.", _pdf_builtin(array_pdf))
        with self.assertRaises(ValueError) as caught:
            manuscript_text("scan.pdf", _pdf(b"BT ET"))
        self.assertIn("no extractable text", str(caught.exception))

    def test_rejects_empty_unknown_and_oversized_files(self):
        with self.assertRaises(ValueError) as caught:
            manuscript_text("story.txt", b"")
        self.assertIn("upload a manuscript file", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            manuscript_text("story.exe", b"hello")
        self.assertIn(".docx", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            manuscript_text("story.txt", b" ")
        self.assertIn("no manuscript text", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            manuscript_text("story.txt", b"a" * (MAX_BYTES + 1))
        self.assertIn("over 8 MB", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            manuscript_text("story.docx", b"not a zip")
        self.assertIn("could not be read", str(caught.exception))

    def test_doc_without_libreoffice_asks_for_docx(self):
        with mock.patch("app.manuscript.shutil.which", return_value=None):
            with self.assertRaises(ValueError) as caught:
                manuscript_text("story.doc", b"not a document")
        self.assertIn("save .doc files as .docx", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
