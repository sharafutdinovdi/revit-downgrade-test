"""Compare model snapshots without Revit or third-party dependencies."""
import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path


def table(headers, rows):
    def cell(value):
        if isinstance(value, (dict, list, tuple)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', '')
    return '\n'.join(['| ' + ' | '.join(map(cell, headers)) + ' |',
                      '| ' + ' | '.join(['---'] * len(headers)) + ' |'] +
                     ['| ' + ' | '.join(map(cell, row)) + ' |' for row in rows]) + '\n'


def position(element):
    loc = element.get('location') or {}
    if loc.get('kind') == 'point':
        return loc.get('point')
    if loc.get('kind') == 'curve' and loc.get('start') and loc.get('end'):
        return [(a + b) / 2 for a, b in zip(loc['start'], loc['end'])]
    return None


def distance(a, b):
    return math.dist(a, b) if a is not None and b is not None else None


def match(before, after, key, unique=False):
    """Use queues to preserve duplicate keys without losing records."""
    remaining = defaultdict(deque)
    for j, item in enumerate(after):
        value = key(item)
        if value is not None and (not unique or value):
            remaining[value].append(j)
    pairs, missing, used = [], [], set()
    for item in before:
        value = key(item)
        if remaining[value]:
            j = remaining[value].popleft()
            pairs.append((item, after[j]))
            used.add(j)
        else:
            missing.append(item)
    return pairs, missing, [item for j, item in enumerate(after) if j not in used]


def match_elements(before, after, tol):
    pairs, missing, added = match(before, after, lambda e: e.get('uniqueId'), True)
    uid_count = len(pairs)
    # A spatial hash bounds fallback searches on large model dumps.
    size = max(tol, 1e-9)
    buckets = defaultdict(list)
    def identity(e):
        return tuple(e.get(k) for k in ('categoryName', 'familyName', 'typeName'))
    def grid(p):
        return tuple(math.floor(v / size) for v in p)
    for j, e in enumerate(added):
        p = position(e)
        if p is not None:
            buckets[(identity(e), grid(p))].append(j)
    used, still_missing = set(), []
    for e in missing:
        p = position(e)
        best = None
        if p is not None:
            x, y, z = grid(p)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for j in buckets.get((identity(e), (x+dx, y+dy, z+dz)), ()):
                            if j not in used:
                                d = distance(p, position(added[j]))
                                if d <= tol and (best is None or (d, j) < best):
                                    best = (d, j)
        if best is None:
            still_missing.append(e)
        else:
            used.add(best[1])
            pairs.append((e, added[best[1]]))
    return pairs, still_missing, [e for j, e in enumerate(added) if j not in used], uid_count


def numeric_changed(a, b, pct, absolute=0):
    if a == b:
        return False
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (a, b)):
        return True
    return abs(a - b) > max(absolute, abs(a) * pct / 100)


def parameter_key(p):
    return (p.get('id') or p.get('name'), p.get('name'))


def is_mm(p):
    # Schema 1 has no unit tag; use the formatted unit or a length parameter name.
    import re
    return bool(re.search(r'\bmm\b', p.get('valueString') or '', re.I) or
                re.search(r'length|width|height|offset|elevation|radius|diameter|thickness|depth',
                          p.get('name', ''), re.I))


def parameter_changes(a, b, tol, pct):
    pairs, missing, added = match(a, b, parameter_key)
    extra, missing, added = match(missing, added, lambda p: p.get('name'))
    pairs += extra
    changes = [(p['name'], p.get('value'), None, 'missing') for p in missing]
    changes += [(p['name'], None, p.get('value'), 'added') for p in added]
    for p, q in pairs:
        if p.get('storageType') != q.get('storageType'):
            changes.append((p['name'], p.get('storageType'), q.get('storageType'), 'storageType'))
        if p.get('isShared') != q.get('isShared'):
            changes.append((p['name'], {'isShared': p.get('isShared')}, {'isShared': q.get('isShared')}, 'changed'))
        x, y = p.get('value'), q.get('value')
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            changed = numeric_changed(x, y, pct, tol if is_mm(p) else 0)
        else:
            changed = x != y
        if changed:
            changes.append((p['name'], x, y, 'changed'))
    return changes


def geometry_changed(a, b, pct):
    if a is None or b is None:
        return a != b
    return any(a.get(k) != b.get(k) for k in ('solidCount', 'faceCount', 'edgeCount')) or any(
        numeric_changed(a.get(k), b.get(k), pct) for k in ('volumeM3', 'areaM2'))


def connector_signature(items):
    return Counter((c.get('domain'), c.get('systemType'), c.get('shape'), c.get('isConnected'))
                   for c in items)


def compare_element(a, b, tol, pct):
    kinds = []
    for kind, keys in [('category', ['categoryName']), ('type', ['familyName', 'typeName']),
                       ('level', ['levelName']), ('workset', ['worksetName']),
                       ('phase', ['phaseCreated', 'phaseDemolished']), ('designOption', ['designOption']),
                       ('orientation', ['mirrored', 'facingFlipped', 'handFlipped'])]:
        if any(a.get(k) != b.get(k) for k in keys):
            kinds.append(kind)
    d = distance(position(a), position(b))
    if (d is not None and d > tol) or ((a.get('location') or {}).get('kind') != (b.get('location') or {}).get('kind')):
        kinds.append('moved')
    box_a, box_b = a.get('bbox'), b.get('bbox')
    if box_a is None or box_b is None:
        box_changed = box_a != box_b
    else:
        box_changed = any(abs((box_a['max'][i]-box_a['min'][i]) -
                              (box_b['max'][i]-box_b['min'][i])) > tol for i in range(3))
    if box_changed:
        kinds.append('bbox')
    if geometry_changed(a.get('geometry'), b.get('geometry'), pct):
        kinds.append('geometry')
    if connector_signature(a.get('connectors', [])) != connector_signature(b.get('connectors', [])):
        kinds.append('connectors')
    params = parameter_changes(a.get('parameters', []), b.get('parameters', []), tol, pct)
    if params:
        kinds.append('parameters')
    return kinds, params, d


def semantic(value):
    if isinstance(value, dict):
        return {k: semantic(v) for k, v in value.items() if k not in
                ('id', 'uniqueId', 'viewTemplateId', 'viewportViewIds', 'scheduleIds', 'elementIds')}
    if isinstance(value, list):
        return [semantic(v) for v in value]
    return value


def documentation(before, after, labels, tol, pct):
    counts, rows = {}, []
    specs = [('types', lambda e: (e.get('categoryName'), e.get('familyName'), e.get('name'))),
             ('families', lambda e: e.get('name')),
             ('views', lambda e: (e.get('viewType'), e.get('name'))),
             ('sheets', lambda e: (e.get('number'), e.get('name')))]
    specs += [(k, lambda e: e.get('name')) for k in
              ('schedules', 'materials', 'levels', 'worksets', 'links', 'groups', 'projectParameters')]
    for section, key in specs:
        source_a = before.get('project', {}).get(section, []) if section in ('levels', 'worksets') else before.get(section, [])
        source_b = after.get('project', {}).get(section, []) if section in ('levels', 'worksets') else after.get(section, [])
        if section == 'types':
            pairs, missing, added = match(source_a, source_b, lambda e: e.get('uniqueId'), True)
            extra, missing, added = match(missing, added, key)
            pairs += extra
        else:
            pairs, missing, added = match(source_a, source_b, key)
        counts.update({section + '_before': len(source_a), section + '_after': len(source_b),
                       section + '_missing': len(missing)})
        for state, items in [('missing', missing), ('added', added)]:
            for e in items:
                detail = semantic(e)
                if section == 'sheets':
                    detail['viewportCount'] = len(e.get('viewportViewIds', []))
                rows.append([section, e.get('name', ''), state,
                             detail if state == 'missing' else '', detail if state == 'added' else ''])
        for a, b in pairs:
            sa, sb = semantic(a), semantic(b)
            if section == 'sheets':
                sa['viewportCount'] = len(a.get('viewportViewIds', []))
                sb['viewportCount'] = len(b.get('viewportViewIds', []))
                sa['scheduleCount'] = len(a.get('scheduleIds', []))
                sb['scheduleCount'] = len(b.get('scheduleIds', []))
            if section == 'types':
                changes = parameter_changes(a.get('parameters', []), b.get('parameters', []), tol, pct)
                sa.pop('parameters', None)
                sb.pop('parameters', None)
                if changes:
                    rows.append([section, a.get('name'), 'parameters', [c[:2] for c in changes],
                                 [[c[0], c[2], c[3]] for c in changes]])
            delta = [k for k in sorted(set(sa) | set(sb)) if sa.get(k) != sb.get(k)]
            if delta:
                rows.append([section, a.get('name', ''), 'changed',
                             {k: sa.get(k) for k in delta}, {k: sb.get(k) for k in delta}])
    headers = ['Section', 'Name', 'Kind', *labels]
    return counts, table(headers, rows), table(headers, rows[:30])


def family_report(a, b, labels, pct):
    rows = []
    pairs, missing, added = match(a.get('parameters', []), b.get('parameters', []), lambda p: p['name'])
    for kind, params in [('missing', missing), ('added', added)]:
        for p in params:
            rows.append(['parameter', p['name'], kind, p if kind == 'missing' else '', p if kind == 'added' else ''])
    for p, q in pairs:
        for field in sorted((set(p) | set(q)) - {'name'}):
            if p.get(field) != q.get(field):
                rows.append(['parameter', p['name'], field, p.get(field), q.get(field)])
    for field in ('nestedFamilies', 'lookupTables', 'connectors', 'typeNames', 'categoryName',
                  'familyName', 'isParametric', 'referencePlaneCount', 'dimensionCount'):
        if semantic(a.get(field)) != semantic(b.get(field)):
            rows.append([field, '', 'changed', a.get(field), b.get(field)])
    ga, gb = a.get('geometryByType', {}), b.get('geometryByType', {})
    for name in sorted(set(ga) | set(gb)):
        if geometry_changed(ga.get(name), gb.get(name), pct):
            rows.append(['geometryByType', name, 'changed', ga.get(name), gb.get(name)])
    return table(['Section', 'Name', 'Kind', *labels], rows)


def write_csv(out, name, headers, rows):
    with (out / name).open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([json.dumps(v, ensure_ascii=False, sort_keys=True) if isinstance(v, (list, dict)) else v for v in row])


def run(before, after, out, tol=1.0, pct=0.1, labels=('2027', '2023')):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    a, b = before.get('elements', []), after.get('elements', [])
    pairs, missing, added, uid = match_elements(a, b, tol)
    summary = dict(elements_before=len(a), elements_after=len(b), matched_by_uniqueId=uid,
                   matched_by_fallback=len(pairs)-uid, missing=len(missing), added=len(added))
    for key in ('category_changed', 'type_changed', 'moved', 'geometry_changed',
                'elements_with_parameter_changes', 'total_parameter_values_changed', 'parameters_missing', 'parameters_added'):
        summary[key] = 0
    cats = defaultdict(Counter)
    for field, items in [('before', a), ('after', b), ('missing', missing), ('added', added)]:
        for e in items:
            cats[e.get('categoryName', '<none>')][field] += 1
    changed_rows, param_rows = [], []
    for x, y in pairs:
        cat = cats[x.get('categoryName', '<none>')]
        cat['matched'] += 1
        kinds, changes, d = compare_element(x, y, tol, pct)
        for kind, key in [('category', 'category_changed'), ('type', 'type_changed'),
                          ('moved', 'moved'), ('geometry', 'geometry_changed'),
                          ('parameters', 'elements_with_parameter_changes')]:
            summary[key] += int(kind in kinds)
        cat['geometryChanged'] += int('geometry' in kinds)
        cat['paramChanged'] += int(bool(changes))
        base = [x.get('id'), x.get('uniqueId'), x.get('categoryName'), x.get('familyName'), x.get('typeName')]
        for name, prev, curr, kind in changes:
            param_rows.append(base + [name, prev, curr, kind])
            key = {'changed': 'total_parameter_values_changed', 'missing': 'parameters_missing', 'added': 'parameters_added'}.get(kind)
            if key and not (kind == 'changed' and isinstance(prev, dict) and 'isShared' in prev):
                summary[key] += 1
        if kinds:
            gx, gy = x.get('geometry') or {}, y.get('geometry') or {}
            changed_rows.append(base + [','.join(kinds), position(x), position(y), d,
                                       gx.get('volumeM3'), gy.get('volumeM3'), gx.get('faceCount'), gy.get('faceCount')])
    counts, docs, highlights = documentation(before, after, labels, tol, pct)
    summary.update(counts)
    wa = {w['description']: w for w in before.get('warnings', [])}
    wb = {w['description']: w for w in after.get('warnings', [])}
    summary.update(warnings_before=sum(w.get('count', 1) for w in wa.values()),
                   warnings_after=sum(w.get('count', 1) for w in wb.values()),
                   warnings_new=len(wb.keys()-wa.keys()), warnings_gone=len(wa.keys()-wb.keys()))
    warning_rows = [[name, 'new' if name not in wa else 'gone' if name not in wb else 'count changed',
                     wa.get(name, {}).get('count', 0), wb.get(name, {}).get('count', 0)]
                    for name in sorted(wa.keys() | wb.keys())
                    if name not in wa or name not in wb or wa[name].get('count') != wb[name].get('count')]
    warnings = table(['Warning', 'Kind', *labels], warning_rows)
    # Paired columns make labels useful in a report pasted into a case README.
    paired = {k[:-7] for k in summary if k.endswith('_before')}
    summary_rows = [[k, summary[k+'_before'], summary.get(k+'_after', '')] for k in sorted(paired)]
    summary_rows += [[k, '', v] for k, v in summary.items() if not k.endswith(('_before', '_after'))]
    summary_md = table(['Metric', *labels], summary_rows)
    cat_headers = ['Category', labels[0], labels[1], 'Matched', 'Missing', 'Added', 'Geometry changed', 'Parameters changed']
    cat_rows = [[name] + [c[k] for k in ('before', 'after', 'matched', 'missing', 'added', 'geometryChanged', 'paramChanged')]
                for name, c in sorted(cats.items())]
    category_md = table(cat_headers, cat_rows)
    aggregated = Counter((r[5], r[8]) for r in param_rows)
    agg_rows = [[name, kind, count] for (name, kind), count in sorted(aggregated.items(), key=lambda item: (-item[1], item[0]))]
    losses = table(['Parameter name', 'Kind', 'Count'], [r for r in agg_rows if r[1] == 'missing'][:20])
    files = {'summary.md': summary_md, 'by_category.md': category_md,
             'documentation.md': docs, 'warnings.md': warnings}
    sections = [('Summary', summary_md), ('By category', category_md), ('Top parameter losses', losses),
                ('Documentation highlights', highlights), ('Warnings', warnings)]
    if 'family' in before and 'family' in after:
        files['family.md'] = family_report(before['family'], after['family'], labels, pct)
        sections.append(('Family', files['family.md']))
    elif (out / 'family.md').exists():
        (out / 'family.md').unlink()
    files['report.md'] = '\n'.join('## ' + heading + '\n\n' + body for heading, body in sections)
    for name, body in files.items():
        (out / name).write_text(body, encoding='utf-8')
    (out / 'summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    write_csv(out, 'by_category.csv', ['category', 'before', 'after', 'matched', 'missing', 'added', 'geometryChanged', 'paramChanged'], cat_rows)
    for filename, items in [('elements_missing.csv', missing), ('elements_added.csv', added)]:
        write_csv(out, filename, ['id', 'uniqueId', 'category', 'family', 'type', 'level', 'location'],
                  [[e.get(k) for k in ('id', 'uniqueId', 'categoryName', 'familyName', 'typeName', 'levelName')] + [e.get('location')] for e in items])
    base_headers = ['elementId', 'uniqueId', 'category', 'family', 'type']
    write_csv(out, 'elements_changed.csv', base_headers + ['changes', 'locationBefore', 'locationAfter', 'locationDistanceMm',
              'volumeM3Before', 'volumeM3After', 'faceCountBefore', 'faceCountAfter'], changed_rows)
    write_csv(out, 'parameters_changed.csv', base_headers + ['parameterName', 'before', 'after', 'kind'], param_rows)
    write_csv(out, 'parameters_by_name.csv', ['parameterName', 'kind', 'count'], agg_rows)
    print(summary_md, end='')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--tol-mm', type=float, default=1.0)
    parser.add_argument('--tol-pct', type=float, default=0.1)
    parser.add_argument('--label-before', default='2027')
    parser.add_argument('--label-after', default='2023')
    args = parser.parse_args()
    if args.tol_mm < 0 or args.tol_pct < 0:
        parser.error('Tolerances must be nonnegative')
    run(json.loads(args.before.read_text(encoding='utf-8')),
        json.loads(args.after.read_text(encoding='utf-8')), args.out, args.tol_mm,
        args.tol_pct, (args.label_before, args.label_after))


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        # This CLI is a report generator with an unconditional zero exit status.
        pass
    except Exception as exc:
        print('Report error: ' + str(exc), file=sys.stderr)
