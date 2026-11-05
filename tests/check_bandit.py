"""Review all Bandit findings, blocking new medium/high issues without hiding low ones."""
import json
from pathlib import Path
import sys

report = json.loads(Path(sys.argv[1]).read_text())
if report.get('errors'):
    raise SystemExit('Bandit failed to analyze one or more files')
if len(report.get('metrics', {})) - 1 != len(list(Path('src').glob('*.py'))):
    raise SystemExit('Bandit did not analyze every production Python file')
failures = []
for item in report['results']:
    name = Path(item['filename']).name
    rule = item['test_id']
    severity = item['issue_severity']
    print(f'{severity}: {rule} in {name}:{item["line_number"]}')
    # B108 flags a forbidden /tmp directory in the path validator; it is not
    # a created temporary file. This precise source location is reviewed.
    source_line = ''
    if name == 'proxmox-resource-check-config.py':
        source_line = Path('src/proxmox-resource-check-config.py').read_text().splitlines()[item['line_number'] - 1].strip()
    justified = (rule == 'B108' and name == 'proxmox-resource-check-config.py'
                 and source_line == "if path in (Path('/'), Path('/tmp'), Path('/var'), Path('/var/log'), Path('/etc'), Path('/usr')):")
    if severity in ('MEDIUM', 'HIGH') and not justified:
        failures.append(item)
if failures:
    raise SystemExit(f'{len(failures)} unreviewed medium/high Bandit finding(s)')
