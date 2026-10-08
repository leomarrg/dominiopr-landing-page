"""
Text extraction for uploaded PDF/DOCX files, run in a CHILD process.

pypdf and zip/XML parsing of attacker-supplied files are the part of the
upload that can burn CPU or memory. The web worker (gunicorn, 512MB box)
never runs them itself: landing.ingest starts this file as

    python -I /path/to/landing/extract_worker.py pdf|docx   < file bytes

with a hard wall-clock timeout, and on Linux the child caps its own address
space and CPU time before touching the data. Exit codes:
    0  text on stdout (UTF-8)
    2  ExtractError: a message meant for the tenant on stderr (UTF-8)
    3  anything else (no detail; the parent shows a generic message)

This module must stay importable WITHOUT Django and without the `landing`
package (it is run by path under -I, so the project is not on sys.path):
stdlib + pypdf only. landing.ingest imports the same functions for the XML
guard used on sitemaps.
"""
import io
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from xml.parsers import expat

MAX_RAW_CHARS = 200_000          # same ceiling the parent keeps
MAX_OUTPUT_CHARS = MAX_RAW_CHARS * 2  # before cleaning; the parent truncates after
DOCX_MAX_UNCOMPRESSED = 10 * 1024 * 1024  # sum of declared member sizes
DOCX_DOCUMENT_MAX = 4 * 1024 * 1024       # word/document.xml actually inflated
DOCX_MAX_MEMBERS = 1000
PDF_MAX_PAGES = 100
PDF_TIME_BUDGET = 20             # soft per-file budget; the parent's timeout is the hard one
PDF_STREAM_LIMIT = 20_000_000    # per-stream decompression ceiling (pypdf default 75MB)

CHILD_MEMORY_LIMIT = 350 * 1024 * 1024
CHILD_CPU_SECONDS = 30


class ExtractError(ValueError):
    """A failure whose message is safe and useful to show the tenant."""


class _DTDRefused(Exception):
    pass


def _refuse_dtd(*_args):
    raise _DTDRefused()


def parse_xml(raw):
    """ElementTree root, or None for malformed XML or ANY document type
    declaration. Sitemaps and .docx never need a DTD, and an internal DTD is
    the vehicle for entity-expansion bombs. The guard is the expat handler
    itself, fired when the parser reaches '<!DOCTYPE' in whatever encoding the
    document declares (UTF-16 included) and before any entity is defined or
    expanded; a byte regex over the raw input cannot see a UTF-16 DOCTYPE."""
    builder = ET.TreeBuilder()
    parser = expat.ParserCreate(namespace_separator='}')
    parser.StartDoctypeDeclHandler = _refuse_dtd
    parser.EntityDeclHandler = _refuse_dtd
    parser.ExternalEntityRefHandler = _refuse_dtd

    def name(tag):
        # expat gives 'uri}local'; ElementTree's convention is '{uri}local'.
        return '{' + tag if '}' in tag else tag

    parser.StartElementHandler = lambda tag, attrs: builder.start(
        name(tag), {name(k): v for k, v in attrs.items()})
    parser.EndElementHandler = lambda tag: builder.end(name(tag))
    parser.CharacterDataHandler = builder.data
    try:
        parser.Parse(raw, True)
        return builder.close()
    except (_DTDRefused, expat.ExpatError, ValueError, AssertionError, IndexError):
        return None


_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def text_from_docx(data):
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, ValueError, OSError):
        raise ExtractError('El archivo Word está dañado o no es un .docx.')
    if len(infos) > DOCX_MAX_MEMBERS:
        raise ExtractError('El archivo Word tiene una estructura inválida.')
    if sum(i.file_size for i in infos) > DOCX_MAX_UNCOMPRESSED:
        raise ExtractError('El contenido del archivo Word es demasiado grande.')
    try:
        info = zf.getinfo('word/document.xml')
    except KeyError:
        raise ExtractError('El archivo no es un documento de Word (.docx) válido.')
    if info.flag_bits & 0x1:
        raise ExtractError('El archivo Word está protegido con contraseña.')
    if info.file_size > DOCX_DOCUMENT_MAX:
        raise ExtractError('El contenido del archivo Word es demasiado grande.')
    try:
        with zf.open(info) as member:
            # Declared sizes can lie: read at most the cap, decompressing lazily.
            xml_bytes = member.read(DOCX_DOCUMENT_MAX + 1)
    except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError, EOFError):
        raise ExtractError('No se pudo leer el archivo Word.')
    if len(xml_bytes) > DOCX_DOCUMENT_MAX:
        raise ExtractError('El contenido del archivo Word es demasiado grande.')
    root = parse_xml(xml_bytes)
    if root is None:
        raise ExtractError('No se pudo leer el archivo Word.')
    paragraphs = []
    for para in root.iter(_W + 'p'):
        parts = []
        for node in para.iter():
            if node.tag == _W + 't' and node.text:
                parts.append(node.text)
            elif node.tag == _W + 'tab':
                parts.append('\t')
            elif node.tag in (_W + 'br', _W + 'cr'):
                parts.append('\n')
        line = ''.join(parts).strip()
        if line:
            paragraphs.append(line)
    return '\n'.join(paragraphs)


def text_from_pdf(data):
    import pypdf
    from pypdf.errors import PyPdfError

    limits = dict(
        maximum_declared_stream_length=PDF_STREAM_LIMIT,
        zlib_maximum_output_length=PDF_STREAM_LIMIT,
        lzw_maximum_output_length=PDF_STREAM_LIMIT,
        run_length_maximum_output_length=PDF_STREAM_LIMIT,
        array_based_stream_maximum_output_length=PDF_STREAM_LIMIT,
    )
    parts, size = [], 0
    deadline = time.monotonic() + PDF_TIME_BUDGET
    try:
        with pypdf.apply_configuration(**limits):
            reader = pypdf.PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                try:
                    ok = reader.decrypt('')
                except Exception:
                    ok = 0
                if not ok:
                    raise ExtractError('El PDF está protegido con contraseña.')
            for index, page in enumerate(reader.pages):
                if index >= PDF_MAX_PAGES or size > MAX_RAW_CHARS or time.monotonic() > deadline:
                    break
                try:
                    text = page.extract_text() or ''
                except (PyPdfError, ValueError, KeyError, TypeError, RecursionError):
                    continue  # one bad page should not lose the rest
                parts.append(text)
                size += len(text)
    except ExtractError:
        raise
    except (PyPdfError, ValueError, KeyError, TypeError, AttributeError, RecursionError,
            OSError):
        raise ExtractError('No se pudo leer el PDF (¿está dañado?).')
    return '\n\n'.join(parts)


EXTRACTORS = {'pdf': text_from_pdf, 'docx': text_from_docx}


def _limit_resources():
    """Linux: bound the child before it parses anything. Windows (dev) has no
    `resource` module; there the parent's timeout is the only bound."""
    try:
        import resource
    except ImportError:
        return
    for limit, value in ((resource.RLIMIT_AS, CHILD_MEMORY_LIMIT),
                         (resource.RLIMIT_CPU, CHILD_CPU_SECONDS)):
        try:
            resource.setrlimit(limit, (value, value))
        except (ValueError, OSError):
            pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    extractor = EXTRACTORS.get(argv[0] if argv else '')
    if extractor is None:
        sys.stderr.write('bad kind\n')
        return 3
    _limit_resources()
    data = sys.stdin.buffer.read()
    try:
        text = extractor(data)
    except ExtractError as exc:
        sys.stderr.buffer.write(str(exc).encode('utf-8', 'replace'))
        return 2
    except BaseException:  # MemoryError under RLIMIT_AS, anything unexpected
        return 3
    sys.stdout.buffer.write(text[:MAX_OUTPUT_CHARS].encode('utf-8', 'replace'))
    sys.stdout.buffer.flush()
    return 0


if __name__ == '__main__':
    sys.exit(main())
