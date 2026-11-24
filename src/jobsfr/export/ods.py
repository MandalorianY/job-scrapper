"""Native OpenDocument controls for the optional LibreOffice tracker."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from math import isfinite
from numbers import Real
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pandas as pd
from odf import config, draw, form, office, style, table, text
from odf.element import Element
from odf.namespaces import OFNS, OOONS, XMLNS
from odf.opendocument import OpenDocumentSpreadsheet, load
from xlsxwriter.utility import xl_col_to_name

from jobsfr.export.tracker import (
    LINK_COLUMNS,
    TRACKER_WIDTHS,
    _coerce_checkbox_value,
    _extract_hyperlink_url,
    _tracker_status_colors,
)

if TYPE_CHECKING:
    from typing import Protocol

    from odf.opendocument import OpenDocument

    class _HasSpreadsheet(Protocol):
        spreadsheet: Element


CALC_EXT = "urn:org:documentfoundation:names:experimental:calc:xmlns:calcext:1.0"


def read_ods_tracker(path: Path) -> pd.DataFrame:
    book = load(str(path))
    spreadsheet = cast("_HasSpreadsheet", book).spreadsheet
    sheet = next(
        (sheet for sheet in spreadsheet.getElementsByType(table.Table) if sheet.getAttribute("name") == "Offres"),
        None,
    )
    if sheet is None:
        raise ValueError(f"No Offres sheet in {path}")
    for cell in sheet.getElementsByType(table.TableCell):
        if cell.getAttribute("valuetype") == "boolean":
            # pandas reads the display text, which Calc localizes (e.g. VRAI).
            cell.setAttribute("valuetype", "float")
            cell.setAttribute("value", int(cell.getAttribute("booleanvalue") in {"true", "1"}))
    return pd.read_excel(book, sheet_name="Offres", engine="odf", dtype=object, keep_default_na=False)


def write_ods(path: Path, tracker: pd.DataFrame, raw: pd.DataFrame, *, keep_reposts: bool = False) -> None:
    book = OpenDocumentSpreadsheet()
    spreadsheet = cast("_HasSpreadsheet", book).spreadsheet
    spreadsheet.get_nsprefix(OFNS)
    spreadsheet.get_nsprefix(OOONS)
    header = style.Style(name="Header", family="table-cell")
    header.addElement(style.TableCellProperties(backgroundcolor="#1F4E78", wrapoption="wrap"))
    header.addElement(style.TextProperties(color="#FFFFFF", fontweight="bold"))
    book.styles.addElement(header)

    columns = list(tracker.columns)
    statuses = _tracker_status_colors(keep_reposts=keep_reposts)
    status_column = xl_col_to_name(columns.index("Statut")) if "Statut" in columns else ""
    if status_column and not tracker.empty:
        _add_status_validation(spreadsheet, status_column, tuple(statuses))
    ranges = table.DatabaseRanges()
    for name, range_name, frame in (
        ("Offres", "TableOffres", tracker),
        ("Données brutes", "TableDonneesBrutes", raw),
    ):
        sheet = table.Table(name=name, parent=spreadsheet)
        if name == "Offres":
            controls = _checkbox_form(sheet, columns, status_column)
            _add_columns(book, sheet, tracker.columns, TRACKER_WIDTHS, header)
            _add_tracker_rows(sheet, tracker, controls, status_column)
            if status_column and not tracker.empty:
                _add_status_colors(book, sheet, status_column, len(frame) + 1, statuses)
        else:
            _add_columns(book, sheet, raw.columns, {}, header)
            for values in raw.itertuples(index=False, name=None):
                row = table.TableRow(parent=sheet)
                for value in values:
                    row.addElement(_cell(value))
        if not frame.empty:
            end_cell = f"{xl_col_to_name(len(frame.columns) - 1)}{len(frame) + 1}"
            table.DatabaseRange(
                name=range_name,
                targetrangeaddress=f"'{name}'.A1:'{name}'.{end_cell}",
                displayfilterbuttons="true",
                parent=ranges,
            )
    if ranges.hasChildNodes():
        spreadsheet.addElement(ranges)
    _freeze_headers(book, ("Offres", "Données brutes"))
    book.save(str(path))


def _add_columns(
    book: OpenDocument,
    sheet: Element,
    columns: pd.Index,
    widths: Mapping[str, int],
    header_style: Element,
) -> None:
    for column in columns:
        width = widths.get(str(column)) or min(45, max(12, len(str(column)) + 2))
        column_style = style.Style(name=f"Column{len(book.automaticstyles.childNodes)}", family="table-column")
        column_style.addElement(style.TableColumnProperties(columnwidth=f"{width * 0.19:.2f}cm"))
        book.automaticstyles.addElement(column_style)
        table.TableColumn(stylename=column_style, parent=sheet)
    header = table.TableRow(parent=sheet)
    for column in columns:
        cell = _cell(column)
        cell.setAttribute("stylename", header_style)
        header.addElement(cell)


def _cell(value: object, *, hyperlink: bool = False) -> Element:
    if value is None or value is pd.NA or value is pd.NaT:
        return table.TableCell()
    if isinstance(value, bool):
        cell = table.TableCell(valuetype="boolean", booleanvalue=str(value).lower())
        cell.addElement(text.P(text=str(value).upper()))
        return cell
    if isinstance(value, Real):
        return table.TableCell(valuetype="float", value=str(value)) if isfinite(value) else table.TableCell()
    if isinstance(value, date):
        return table.TableCell(valuetype="date", datevalue=value.isoformat())
    value = (_extract_hyperlink_url(value) if hyperlink else None) or str(value)
    if not value:
        return table.TableCell()
    cell = table.TableCell(valuetype="string")
    paragraph = text.P(parent=cell)
    if hyperlink and value.startswith(("https://", "http://")):
        text.A(href=value, text=value, parent=paragraph)
    else:
        for line in value.split("\n"):
            if paragraph.hasChildNodes():
                paragraph.addElement(text.LineBreak())
            paragraph.addText(line)
    return cell


def _freeze_headers(book: OpenDocument, sheet_names: Sequence[str]) -> None:
    views = config.ConfigItemSet(name="ooo:view-settings")
    book.settings.addElement(views)
    indexed = config.ConfigItemMapIndexed(name="Views", parent=views)
    entry = config.ConfigItemMapEntry(parent=indexed)
    config.ConfigItem(name="ActiveTable", type="string", text=sheet_names[0], parent=entry)
    sheets = config.ConfigItemMapNamed(name="Tables", parent=entry)
    for name in sheet_names:
        sheet = config.ConfigItemMapEntry(name=name, parent=sheets)
        for field, kind, value in (
            ("VerticalSplitMode", "short", "2"),
            ("VerticalSplitPosition", "int", "1"),
            ("ActiveSplitRange", "short", "2"),
            ("PositionBottom", "int", "1"),
        ):
            config.ConfigItem(name=field, type=kind, text=value, parent=sheet)


def _checkbox_form(sheet: Element, columns: Sequence[str], status_column: str) -> Element | None:
    if "Rejeté" in columns or ("Postulé" in columns and not status_column):
        forms = office.Forms(applydesignmode="false", automaticfocus="false", parent=sheet)
        return form.Form(name="Suivi", controlimplementation="ooo:com.sun.star.form.component.Form", parent=forms)
    return None


def _add_tracker_rows(
    sheet: Element,
    tracker: pd.DataFrame,
    controls: Element | None,
    status_column: str,
) -> None:
    for row_number, values in enumerate(tracker.itertuples(index=False, name=None), 2):
        row = table.TableRow(parent=sheet)
        for index, (column, value) in enumerate(zip(tracker.columns, values, strict=True)):
            if column in ("Postulé", "Rejeté"):
                value = _coerce_checkbox_value(value)
            cell = _cell(value, hyperlink=column in (*LINK_COLUMNS, "URL entreprise"))
            row.addElement(cell)
            if column == "Statut":
                cell.setAttribute("contentvalidationname", "Statut")
            elif column == "Postulé" and status_column:
                cell.setAttribute("formula", f'of:=[.{status_column}{row_number}]="Postulé"')
            elif controls is not None and column in ("Postulé", "Rejeté"):
                _add_checkbox(controls, cell, f"{xl_col_to_name(index)}{row_number}", bool(value))


def _add_checkbox(controls: Element, cell: Element, address: str, checked: bool) -> None:
    state = "checked" if checked else "unchecked"
    checkbox = form.Checkbox(
        name=address,
        id=f"checkbox{address}",
        linkedcell=f"Offres.{address}",
        currentstate=state,
        controlimplementation="ooo:com.sun.star.form.component.CheckBox",
        parent=controls,
    )
    checkbox.setAttrNS(XMLNS, "id", f"checkbox{address}")
    cell.addElement(
        draw.Control(
            control=f"checkbox{address}",
            anchortype="paragraph",
            x="0.07cm",
            y="0cm",
            width="0.38cm",
            height="0.38cm",
        )
    )


def _add_status_validation(spreadsheet: Element, column: str, statuses: Sequence[str]) -> None:
    choices = ";".join(f'"{status}"' for status in statuses)
    validations = table.ContentValidations(parent=spreadsheet)
    validation = table.ContentValidation(
        name="Statut",
        condition=f"of:cell-content-is-in-list({choices})",
        basecelladdress=f"Offres.{column}2",
        displaylist="unsorted",
        allowemptycell="true",
        parent=validations,
    )
    error = table.ErrorMessage(messagetype="stop", title="Statut invalide", display="true", parent=validation)
    error.addElement(text.P(text="Choisissez un statut dans la liste."))


def _add_status_colors(
    book: OpenDocument,
    sheet: Element,
    column: str,
    last_row: int,
    statuses: Mapping[str, tuple[str, str]],
) -> None:
    first_cell = f"Offres.{column}2"
    formats = Element(qname=(CALC_EXT, "conditional-formats"))
    condition_range = Element(
        qname=(CALC_EXT, "conditional-format"),
        qattributes={(CALC_EXT, "target-range-address"): f"{first_cell}:Offres.{column}{last_row}"},
    )
    formats.addElement(condition_range, check_grammar=False)
    sheet.addElement(formats, check_grammar=False)
    for index, (status, (background, foreground)) in enumerate(statuses.items()):
        status_style = style.Style(name=f"Status{index}", family="table-cell")
        status_style.addElement(style.TableCellProperties(backgroundcolor=background))
        status_style.addElement(style.TextProperties(color=foreground))
        book.styles.addElement(status_style)
        condition_range.addElement(
            Element(
                qname=(CALC_EXT, "condition"),
                qattributes={
                    (CALC_EXT, "apply-style-name"): status_style.getAttribute("name"),
                    (CALC_EXT, "value"): f'="{status}"',
                    (CALC_EXT, "base-cell-address"): first_cell,
                },
            ),
            check_grammar=False,
        )
