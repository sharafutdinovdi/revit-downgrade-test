import copy
import csv
import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'diff_dumps.py'


def parameter(name, value, storage='Double', **extra):
    return dict(name=name, id=name, value=value, valueString=str(value),
                storageType=storage, isShared=False, **extra)


def element(uid, x, category='Walls', **extra):
    return dict(id=len(uid), uniqueId=uid, categoryName=category, familyName='Basic',
                typeName='Standard', location={'kind': 'point', 'point': [x, 0, 0]},
                geometry={'solidCount': 1, 'faceCount': 6, 'volumeM3': 1, 'areaM2': 6},
                parameters=[], **extra)


def invoke(tmp_path, before, after, *args):
    for name, dump in [('before', before), ('after', after)]:
        (tmp_path / (name + '.json')).write_text(json.dumps(dump), encoding='utf-8')
    out = tmp_path / 'report'
    result = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / 'before.json'),
                             str(tmp_path / 'after.json'), '--out', str(out), *args],
                            capture_output=True, text=True, check=True)
    assert '| Metric |' in result.stdout
    return json.loads((out / 'summary.json').read_text()), out


def test_downgrade_report(tmp_path):
    before = {'elements': [element('stable', 0), element('old-id', 50), element('lost', 100)],
              'views': [{'name': 'Plan', 'viewType': 'FloorPlan'}],
              'sheets': [{'name': 'Sheet', 'number': 'A1', 'viewportViewIds': [4]}],
              'warnings': [{'description': 'Old warning', 'count': 2}],
              'family': {'parameters': [{'name': 'Width', 'formula': 'Height * 2', 'valuesByType': {'A': '10'}}]}}
    before['elements'][0]['parameters'] = [parameter('Width', 100), parameter('Lost parameter', 'text', 'String')]
    after = copy.deepcopy(before)
    after['elements'] = [copy.deepcopy(before['elements'][0]), element('new-id', 50.5), element('added', 200, 'Doors')]
    changed = after['elements'][0]
    changed['location']['point'][0] = 10
    changed['geometry']['volumeM3'] = 1.5
    changed['parameters'] = [parameter('Width', 120)]
    after['views'], after['sheets'] = [], []
    after['warnings'] = [{'description': 'New warning', 'count': 3}]
    after['family']['parameters'][0]['formula'] = 'Height * 3'
    summary, out = invoke(tmp_path, before, after)
    expected = {'elements_before': 3, 'elements_after': 3, 'matched_by_uniqueId': 1,
                'matched_by_fallback': 1, 'missing': 1, 'added': 1, 'moved': 1,
                'geometry_changed': 1, 'elements_with_parameter_changes': 1,
                'total_parameter_values_changed': 1, 'parameters_missing': 1,
                'views_missing': 1, 'sheets_missing': 1, 'warnings_new': 1,
                'warnings_gone': 1, 'warnings_before': 2, 'warnings_after': 3}
    for key, value in expected.items():
        assert summary[key] == value, key
    assert '| Category | 2027 | 2023 |' in (out / 'report.md').read_text()
    assert 'Height * 2' in (out / 'family.md').read_text()
    assert 'Height * 3' in (out / 'family.md').read_text()
    with (out / 'parameters_by_name.csv').open() as stream:
        rows = list(csv.DictReader(stream))
    assert any(r['parameterName'] == 'Lost parameter' and r['kind'] == 'missing' and r['count'] == '1' for r in rows)
    assert {p.name for p in out.iterdir()} == {
        'summary.json', 'summary.md', 'by_category.csv', 'by_category.md',
        'elements_missing.csv', 'elements_added.csv', 'elements_changed.csv',
        'parameters_changed.csv', 'parameters_by_name.csv', 'documentation.md',
        'warnings.md', 'family.md', 'report.md'}


def test_tolerances_and_one_to_one_fallback(tmp_path):
    before = {'elements': [element('a', 0), element('b', 0), element('stable', 20)]}
    before['elements'][2]['parameters'] = [parameter('Width', 100), parameter('Ratio', 100)]
    after = {'elements': [element('c', 0.5), copy.deepcopy(before['elements'][2])]}
    after['elements'][1]['location']['point'][0] += 0.5
    after['elements'][1]['geometry']['volumeM3'] += 0.0005
    after['elements'][1]['parameters'][0]['value'] += 0.5
    after['elements'][1]['parameters'][1]['value'] += 0.05
    summary, out = invoke(tmp_path, before, after, '--label-before', 'Original', '--label-after', 'Converted')
    assert summary['matched_by_fallback'] == 1
    assert summary['missing'] == 1
    assert summary['moved'] == summary['geometry_changed'] == summary['total_parameter_values_changed'] == 0
    assert '| Category | Original | Converted |' in (out / 'report.md').read_text()
    assert not (out / 'family.md').exists()


def test_documentation_and_parameter_metadata(tmp_path):
    first = parameter('Same name', 1)
    second = dict(parameter('Same name', 2), id='another-id')
    before = {'elements': [element('stable', 0)],
              'types': [{'uniqueId': 'old', 'categoryName': 'Walls', 'familyName': 'Basic', 'name': 'Standard', 'parameters': []}],
              'schedules': [{'name': 'Schedule', 'fieldNames': ['A', 'B']}],
              'sheets': [{'name': 'Sheet', 'number': 'A1', 'viewportViewIds': [1, 2]}]}
    before['elements'][0]['parameters'] = [first, second]
    after = copy.deepcopy(before)
    after['types'][0]['uniqueId'] = 'new'
    after['elements'][0]['parameters'][0]['storageType'] = 'Integer'
    after['elements'][0]['parameters'][1]['value'] = 3
    after['elements'][0]['connectors'] = [{'domain': 'Piping', 'isConnected': True}]
    after['schedules'][0]['fieldNames'] = ['A', 'C']
    after['sheets'][0]['viewportViewIds'] = [11]
    summary, out = invoke(tmp_path, before, after)
    assert summary['types_missing'] == 0
    assert summary['total_parameter_values_changed'] == 1
    with (out / 'parameters_changed.csv').open() as stream:
        rows = list(csv.DictReader(stream))
    assert {r['kind'] for r in rows} == {'storageType', 'changed'}
    docs = (out / 'documentation.md').read_text()
    assert 'fieldNames' in docs and 'viewportCount' in docs
    assert 'connectors' in (out / 'elements_changed.csv').read_text()


def test_empty_dumps_and_curve_midpoint_fallback(tmp_path):
    summary, out = invoke(tmp_path, {}, {})
    assert summary['elements_before'] == summary['elements_after'] == 0
    assert summary['matched_by_fallback'] == summary['missing'] == 0
    before = {'elements': [element('old', 0)]}
    before['elements'][0]['location'] = {'kind': 'curve', 'start': [0, 0, 0], 'end': [10, 0, 0]}
    after = copy.deepcopy(before)
    after['elements'][0]['uniqueId'] = 'new'
    summary, out = invoke(tmp_path, before, after, '--tol-mm', '0')
    assert summary['matched_by_fallback'] == 1
    assert summary['missing'] == summary['added'] == 0


def test_shared_parameter_and_orientation_changes(tmp_path):
    before = {'elements': [element('stable', 0, mirrored=False)],
              'family': {'parameters': [{'name': 'Width', 'valuesByType': {'A': '10'}}]}}
    before['elements'][0]['parameters'] = [parameter('Custom', 1)]
    after = copy.deepcopy(before)
    after['elements'][0]['parameters'][0].update(id='shared-guid', isShared=True)
    after['elements'][0]['mirrored'] = True
    after['family']['parameters'][0]['valuesByType']['A'] = '20'
    summary, out = invoke(tmp_path, before, after)
    assert summary['elements_with_parameter_changes'] == 1
    assert summary['total_parameter_values_changed'] == 0
    assert summary['parameters_missing'] == summary['parameters_added'] == 0
    assert 'isShared' in (out / 'parameters_changed.csv').read_text()
    assert 'orientation' in (out / 'elements_changed.csv').read_text()
    assert 'valuesByType' in (out / 'family.md').read_text()
