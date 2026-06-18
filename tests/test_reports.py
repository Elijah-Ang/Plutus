from app.reports import SHEETS, export_excel
from app.storage import Storage
from openpyxl import load_workbook


def test_export_has_expected_sheets(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    path = export_excel(storage, {"mode": "paper"}, tmp_path / "report.xlsx")
    workbook = load_workbook(path, read_only=True)
    assert workbook.sheetnames == [name for name, _ in SHEETS]
