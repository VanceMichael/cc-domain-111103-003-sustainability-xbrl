from pathlib import Path
from app.xbrl import inspect_report
print(inspect_report(Path("fixtures/report.xml").read_bytes())["facts"])
