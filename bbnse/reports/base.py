"""Report building blocks.

Reports read only the normalized tables (Observation / DealObservation /
Alert), never the raw snapshots. That is what makes them regenerable for any
past date long after raw payloads have been pruned.
"""
from __future__ import annotations

import html as html_mod
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..core.config import PROJECT_ROOT
from ..core.logging_setup import get_logger

log = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")


def to_local(dt: datetime | None, tz: ZoneInfo = IST) -> datetime | None:
    """Timestamps are stored in UTC; reports are read in IST.

    SQLite hands back naive datetimes, so attach UTC before converting rather
    than letting a UTC value be displayed under an IST heading.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)

_HTML_SHELL = """<!doctype html>
<meta charset="utf-8">
<title>{title}</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
        max-width: 900px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }}
 table {{ border-collapse: collapse; width: 100%; margin: 1rem 0;
         font-variant-numeric: tabular-nums; }}
 th, td {{ border-bottom: 1px solid #8884; padding: .4rem .6rem;
          text-align: left; }}
 th {{ font-weight: 600; opacity: .75; font-size: .85em;
      text-transform: uppercase; letter-spacing: .03em; }}
 td.num, th.num {{ text-align: right; }}
 h1 {{ font-size: 1.6rem; margin-bottom: .2rem; }}
 h2 {{ font-size: 1.15rem; margin-top: 2rem;
      border-bottom: 2px solid #8883; padding-bottom: .3rem; }}
 .meta {{ opacity: .6; font-size: .9em; }}
 .crit {{ color: #d02; font-weight: 600; }}
 .up {{ color: #0a7; }} .down {{ color: #d34; }}
 .empty {{ opacity: .55; font-style: italic; }}
</style>
{body}
"""


@dataclass
class Section:
    heading: str
    # Table rendering
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    numeric_cols: set[int] = field(default_factory=set)
    # Free text instead of / before a table
    note: str = ""
    empty_text: str = "Nothing to report."

    @property
    def is_empty(self) -> bool:
        return not self.rows and not self.note


@dataclass
class ReportOutput:
    title: str
    markdown: str
    summary: str
    md_path: Path | None = None
    html_path: Path | None = None


def _fmt(val) -> str:
    if val is None:
        return "–"
    if isinstance(val, float):
        return f"{val:,.2f}"
    if isinstance(val, int):
        return f"{val:,}"
    return str(val)


class ReportBuilder:
    """Renders Sections to Markdown and a standalone HTML file."""

    def __init__(self, cfg, title: str, subtitle: str = ""):
        self.cfg = cfg
        self.title = title
        self.subtitle = subtitle
        self.sections: list[Section] = []
        out_dir = cfg.get("app.report_dir", "reports_out")
        self.out_dir = Path(out_dir)
        if not self.out_dir.is_absolute():
            self.out_dir = PROJECT_ROOT / self.out_dir

    def add(self, section: Section) -> None:
        self.sections.append(section)

    # -- rendering -----------------------------------------------------------
    def to_markdown(self) -> str:
        lines = [f"# {self.title}"]
        if self.subtitle:
            lines.append(f"*{self.subtitle}*")
        lines.append("")
        for sec in self.sections:
            lines.append(f"## {sec.heading}")
            lines.append("")
            if sec.note:
                lines.append(sec.note)
                lines.append("")
            if sec.rows:
                lines.append("| " + " | ".join(sec.columns) + " |")
                lines.append("|" + "|".join(
                    " ---: " if i in sec.numeric_cols else " --- "
                    for i in range(len(sec.columns))) + "|")
                for row in sec.rows:
                    lines.append("| " + " | ".join(_fmt(c) for c in row) + " |")
                lines.append("")
            elif not sec.note:
                lines.append(f"*{sec.empty_text}*")
                lines.append("")
        lines.append("---")
        lines.append(f"*Generated {datetime.now().strftime('%d-%b-%Y %H:%M:%S')} "
                     f"IST by Bull Bear NSE Data Harvest.*")
        return "\n".join(lines)

    def to_html(self) -> str:
        esc = html_mod.escape
        body = [f"<h1>{esc(self.title)}</h1>"]
        if self.subtitle:
            body.append(f'<p class="meta">{esc(self.subtitle)}</p>')
        for sec in self.sections:
            body.append(f"<h2>{esc(sec.heading)}</h2>")
            if sec.note:
                body.append(f"<p>{esc(sec.note)}</p>")
            if sec.rows:
                head = "".join(
                    f'<th class="num">{esc(c)}</th>' if i in sec.numeric_cols
                    else f"<th>{esc(c)}</th>"
                    for i, c in enumerate(sec.columns))
                body.append(f"<table><thead><tr>{head}</tr></thead><tbody>")
                for row in sec.rows:
                    cells = "".join(
                        f'<td class="num">{esc(_fmt(c))}</td>'
                        if i in sec.numeric_cols else f"<td>{esc(_fmt(c))}</td>"
                        for i, c in enumerate(row))
                    body.append(f"<tr>{cells}</tr>")
                body.append("</tbody></table>")
            elif not sec.note:
                body.append(f'<p class="empty">{esc(sec.empty_text)}</p>')
        body.append('<p class="meta">Generated '
                    f"{datetime.now().strftime('%d-%b-%Y %H:%M:%S')} IST by "
                    "Bull Bear NSE Data Harvest.</p>")
        return _HTML_SHELL.format(title=esc(self.title),
                                  body="\n".join(body))

    def write(self, slug: str, for_date: date) -> tuple[Path, Path]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{slug}-{for_date.isoformat()}"
        md_path = self.out_dir / f"{stem}.md"
        html_path = self.out_dir / f"{stem}.html"
        md_path.write_text(self.to_markdown(), encoding="utf-8")
        html_path.write_text(self.to_html(), encoding="utf-8")
        log.info("report written", extra={"md": str(md_path),
                                          "html": str(html_path)})
        return md_path, html_path
