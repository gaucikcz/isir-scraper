"""Extrakce textu z PDF. pdftotext -> OCR fallback. Zadne LLM tokeny (kap. 6)."""
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Tuple

log = logging.getLogger(__name__)

MIN_USEFUL_CHARS = 200
OCR_TIMEOUT = 180
PDFTOTEXT_TIMEOUT = 60


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _run(cmd, timeout):
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout
    )


def _nonempty_len(text: str) -> int:
    return len("".join(text.split()))


def pdftotext(path: str) -> str:
    if not _have("pdftotext"):
        log.warning("pdftotext neni nainstalovan (brew install poppler / apt poppler-utils)")
        return ""
    try:
        proc = _run(["pdftotext", "-layout", "-enc", "UTF-8", path, "-"], PDFTOTEXT_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("pdftotext selhal: %s", exc)
        return ""
    if proc.returncode != 0:
        log.warning("pdftotext navratovy kod %s: %s", proc.returncode,
                    proc.stderr.decode("utf-8", "replace")[:200])
    return proc.stdout.decode("utf-8", "replace")


def ocr(path: str) -> str:
    """Fallback pro skenovane dokumenty: pdftoppm + tesseract -l ces."""
    if not (_have("pdftoppm") and _have("tesseract")):
        log.info("OCR preskoceno - chybi pdftoppm nebo tesseract")
        return ""
    out = []
    tmpdir = tempfile.mkdtemp(prefix="isir-ocr-")
    try:
        prefix = os.path.join(tmpdir, "page")
        try:
            # jen prvnich 8 stran - navrh na zpenezeni byva kratky
            _run(["pdftoppm", "-r", "300", "-png", "-f", "1", "-l", "8", path, prefix],
                 OCR_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.warning("pdftoppm selhal: %s", exc)
            return ""
        pages = sorted(f for f in os.listdir(tmpdir) if f.endswith(".png"))
        for page in pages:
            try:
                proc = _run(
                    ["tesseract", os.path.join(tmpdir, page), "stdout", "-l", "ces"],
                    OCR_TIMEOUT,
                )
                out.append(proc.stdout.decode("utf-8", "replace"))
            except (subprocess.TimeoutExpired, OSError) as exc:
                log.warning("tesseract selhal na %s: %s", page, exc)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return "\n".join(out)


def extract_text(pdf_bytes: bytes) -> Tuple[str, str]:
    """Vrati (text, zdroj) kde zdroj je 'pdftotext' | 'ocr' | 'none'."""
    if not pdf_bytes:
        return "", "none"
    fd, path = tempfile.mkstemp(suffix=".pdf", prefix="isir-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(pdf_bytes)

        text = pdftotext(path)
        if _nonempty_len(text) >= MIN_USEFUL_CHARS:
            return text, "pdftotext"

        log.info("pdftotext vratil %d znaku - zkousim OCR (pravdepodobne sken)",
                 _nonempty_len(text))
        ocr_text = ocr(path)
        if _nonempty_len(ocr_text) >= MIN_USEFUL_CHARS:
            return ocr_text, "ocr"

        # vratime aspon to, co mame
        best = text if _nonempty_len(text) >= _nonempty_len(ocr_text) else ocr_text
        return best, ("pdftotext" if best is text and best.strip() else "none")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
