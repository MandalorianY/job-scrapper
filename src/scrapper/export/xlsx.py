"""Write Excel workbooks and read application state from existing XLSX files."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from xlsxwriter.format import Format
from xlsxwriter.utility import xl_rowcol_to_cell
from xlsxwriter.worksheet import Worksheet

from scrapper.export.tracker import STATUS_COLORS, STATUS_OPTIONS, TRACKER_WIDTHS, _coerce_checkbox_value, _safe_cell

MAX_EXCEL_ROW_INDEX = 1_048_575


def write_xlsx(path: Path, tracker: pd.DataFrame, raw: pd.DataFrame) -> None:
    with pd.ExcelWriter(path, engine="xlsxwriter", date_format="dd/mm/yyyy", datetime_format="dd/mm/yyyy") as writer:
        tracker.to_excel(writer, index=False, sheet_name="Offres")
        raw.to_excel(writer, index=False, sheet_name="Données brutes")
        _format_jobs_sheet(writer, "Offres", tracker)
        _format_raw_sheet(writer, "Données brutes", raw)


def read_xlsx_tracker(path: Path) -> pd.DataFrame:
    workbook = load_workbook(path, data_only=False, read_only=True)
    try:
        if "Offres" not in workbook.sheetnames:
            raise ValueError(f"No Offres sheet in {path}")
        worksheet = workbook["Offres"]
        rows = worksheet.iter_rows(values_only=True)
        headers = [str(value).strip() if value is not None else "" for value in next(rows, ())]
        headers = [header for header in headers if header]
        if not headers:
            return pd.DataFrame()
        records = []
        for values in rows:
            if not values or all(value is None for value in values):
                continue
            records.append(
                {header: values[index] if index < len(values) else "" for index, header in enumerate(headers)}
            )
        frame = pd.DataFrame(records, dtype=object)
        return frame.where(frame.notna(), "")
    finally:
        workbook.close()


def _format_jobs_sheet(writer: pd.ExcelWriter, sheet_name: str, frame: pd.DataFrame) -> None:
    worksheet = writer.sheets[sheet_name]
    workbook = writer.book
    worksheet.freeze_panes(1, 0)
    worksheet.hide_gridlines(2)

    header_format = workbook.add_format(
        {
            "bold": True,
            "font_color": "#FFFFFF",
            "bg_color": "#1F4E78",
            "align": "center",
            "valign": "vcenter",
            "text_wrap": True,
        }
    )
    checkbox_format = workbook.add_format({"align": "center", "valign": "vcenter", "checkbox": True})
    description_format = workbook.add_format({"text_wrap": True, "valign": "top"})
    salary_format = workbook.add_format({"num_format": '# ##0" €"'})
    date_format = workbook.add_format({"num_format": "dd/mm/yyyy"})
    worksheet.set_row(0, 28, header_format)
    for index, column in enumerate(frame.columns):
        worksheet.write(0, index, column, header_format)

    checkbox_col = _optional_column_index(frame, "Postulé")
    rejected_checkbox_col = _optional_column_index(frame, "Rejeté")
    salary_min_col = _optional_column_index(frame, "Salaire min (€ annuel)")
    salary_max_col = _optional_column_index(frame, "Salaire max (€ annuel)")
    description_col = _optional_column_index(frame, "Description")
    status_col = _optional_column_index(frame, "Statut")

    for row in range(1, len(frame) + 1):
        if checkbox_col:
            value = _coerce_checkbox_value(frame.iloc[row - 1, checkbox_col - 1])
            if status_col:
                status_cell = xl_rowcol_to_cell(row, status_col - 1)
                worksheet.write_formula(row, checkbox_col - 1, f'={status_cell}="Postulé"', checkbox_format, value)
            else:
                worksheet.insert_checkbox(row, checkbox_col - 1, value, checkbox_format)
        if rejected_checkbox_col:
            value = _coerce_checkbox_value(frame.iloc[row - 1, rejected_checkbox_col - 1])
            worksheet.insert_checkbox(row, rejected_checkbox_col - 1, value, checkbox_format)
    if status_col:
        worksheet.data_validation(
            1,
            status_col - 1,
            MAX_EXCEL_ROW_INDEX,
            status_col - 1,
            {
                "validate": "list",
                "source": list(STATUS_OPTIONS),
                "input_title": "Statut",
                "input_message": "Choisir un statut.",
                "error_title": "Statut invalide",
                "error_message": "Choisissez En Attente, Postulé ou Refusé.",
            },
        )
        if len(frame):
            for status, (background, foreground) in STATUS_COLORS.items():
                worksheet.conditional_format(
                    1,
                    status_col - 1,
                    len(frame),
                    status_col - 1,
                    {
                        "type": "cell",
                        "criteria": "==",
                        "value": f'"{status}"',
                        "format": workbook.add_format({"bg_color": background, "font_color": foreground}),
                    },
                )

    column_formats = {}
    if checkbox_col:
        column_formats["Postulé"] = checkbox_format
    if rejected_checkbox_col:
        column_formats["Rejeté"] = checkbox_format
    if salary_min_col:
        column_formats["Salaire min (€ annuel)"] = salary_format
    if salary_max_col:
        column_formats["Salaire max (€ annuel)"] = salary_format
    if description_col:
        column_formats["Description"] = description_format
    for column in ("Publié le", "Vu le", "Dernière vue"):
        if column in frame.columns:
            column_formats[column] = date_format
    _apply_widths(worksheet, frame, TRACKER_WIDTHS, column_formats)
    _add_table(worksheet, "TableOffres", frame)


def _format_raw_sheet(writer: pd.ExcelWriter, sheet_name: str, frame: pd.DataFrame) -> None:
    worksheet = writer.sheets[sheet_name]
    workbook = writer.book
    header_format = workbook.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#595959"})
    worksheet.freeze_panes(1, 0)
    worksheet.set_row(0, None, header_format)
    for index, column in enumerate(frame.columns):
        worksheet.write(0, index, column, header_format)
    _autosize(worksheet, frame)
    _add_table(worksheet, "TableDonneesBrutes", frame)


def _add_table(worksheet: Worksheet, name: str, frame: pd.DataFrame) -> None:
    row_count = len(frame)
    column_count = len(frame.columns)
    if row_count < 1 or column_count < 1:
        return
    worksheet.add_table(
        0,
        0,
        row_count,
        column_count - 1,
        {
            "name": name,
            "style": "Table Style Medium 2",
            "first_column": False,
            "last_column": False,
            "banded_rows": True,
            "banded_columns": False,
            "autofilter": True,
            "columns": [{"header": column} for column in frame.columns],
        },
    )


def _column_index(frame: pd.DataFrame, column: str) -> int:
    return list(frame.columns).index(column) + 1


def _optional_column_index(frame: pd.DataFrame, column: str) -> int | None:
    if column not in frame.columns:
        return None
    return _column_index(frame, column)


def _apply_widths(
    worksheet: Worksheet, frame: pd.DataFrame, widths: Mapping[str, int], formats: Mapping[str, Format] | None = None
) -> None:
    formats = formats or {}
    for index, column in enumerate(frame.columns):
        width = widths.get(column)
        if not width:
            values = [str(column), *[str(_safe_cell(value)) for value in frame[column].head(100)]]
            width = min(max(len(value) for value in values) + 2, 50)
        worksheet.set_column(index, index, width, formats.get(column))


def _autosize(worksheet: Worksheet, frame: pd.DataFrame) -> None:
    for index, column in enumerate(frame.columns):
        values = [str(column), *[str(_safe_cell(value)) for value in frame[column].head(100)]]
        width = min(max(len(value) for value in values) + 2, 80)
        worksheet.set_column(index, index, width)
