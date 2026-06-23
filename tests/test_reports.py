from app.reports import SHEETS, export_excel, redact_report_value
from app.storage import Storage
from openpyxl import load_workbook


def test_export_has_expected_sheets(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    path = export_excel(storage, {"mode": "paper"}, tmp_path / "report.xlsx")
    workbook = load_workbook(path, read_only=True)
    assert workbook.sheetnames == [name for name, _ in SHEETS]


def test_report_redacts_telegram_text_and_sender_ids_inside_json_detail():
    detail = '{"raw_command":"sleep","sender_id":"authorized_user_123","nested":{"text":"awake","updated_by":"7"}}'

    redacted = redact_report_value("audit_events", "detail", detail)

    assert "sleep" not in redacted
    assert "awake" not in redacted
    assert "authorized_user_123" not in redacted
    assert '"raw_command":"[REDACTED TELEGRAM TEXT]"' in redacted
    assert '"sender_id":"[REDACTED ID]"' in redacted
    assert '"updated_by":"[REDACTED ID]"' in redacted
