"""Report rendering: markdown always, PDF when WeasyPrint can run."""

from .render import (
    DISCLOSURE,
    PdfUnavailableError,
    render_html,
    render_markdown,
    write_reports,
)

__all__ = [
    "DISCLOSURE",
    "PdfUnavailableError",
    "render_html",
    "render_markdown",
    "write_reports",
]
