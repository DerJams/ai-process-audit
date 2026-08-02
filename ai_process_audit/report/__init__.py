"""Report rendering: markdown always, PDF when WeasyPrint can run."""

from .render import (
    DISCLOSURE,
    PdfUnavailableError,
    find_edge,
    render_html,
    render_markdown,
    render_pdf_with_edge,
    write_reports,
)

__all__ = [
    "DISCLOSURE",
    "PdfUnavailableError",
    "find_edge",
    "render_html",
    "render_markdown",
    "render_pdf_with_edge",
    "write_reports",
]
