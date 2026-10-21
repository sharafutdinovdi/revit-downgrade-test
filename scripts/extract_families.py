from __future__ import print_function, division

import codecs
import json
import os
import re

import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
from Autodesk.Revit import DB


def get_document():
    models = globals().get('__models__')
    path = models[0] if models else os.environ.get('RVT_DUMP_MODEL')
    if path:
        opts = DB.OpenOptions()
        try:
            info = DB.BasicFileInfo.Extract(path)
            try:
                if info.IsWorkshared:
                    opts.DetachFromCentralOption = DB.DetachFromCentralOption.DetachAndPreserveWorksets
            finally:
                info.Dispose()
        except Exception:
            pass
        return __revit__.Application.OpenDocumentFile(
            DB.ModelPathUtils.ConvertUserVisiblePathToModelPath(path), opts)
    if __revit__.ActiveUIDocument is not None:
        return __revit__.ActiveUIDocument.Document
    documents = list(__revit__.Application.Documents)
    if len(documents) == 1:
        return documents[0]
    for document in documents:
        if not document.IsFamilyDocument:
            return document
    raise RuntimeError('No active document or unambiguous fallback document')


def element_id(value):
    try:
        return int(value.Value)
    except AttributeError:
        return int(value.IntegerValue)


def record_error(row, stage, exc, errors):
    entry = {'id': row['id'], 'name': row['name'],
             'stage': stage, 'message': unicode(exc)}
    row['errors'].append(entry)
    errors.append(entry)
    print('ERROR {0} {1}: {2}'.format(row['name'], stage, exc))


def measure(famdoc, row):
    manager = famdoc.FamilyManager
    parameters = list(manager.Parameters)
    row['parameters'] = len(parameters)
    row['formulas'] = sum(1 for param in parameters
                          if param.Formula and unicode(param.Formula).strip())
    row['types'] = manager.Types.Size
    tables = DB.FamilySizeTableManager.GetFamilySizeTableManager(
        famdoc, famdoc.OwnerFamily.Id)
    row['lookupTables'] = len(list(tables.GetAllSizeTableNames())) if tables else 0
    nested = list(DB.FilteredElementCollector(famdoc).OfClass(DB.Family))
    row['nested'] = len(nested)
    shared = 0
    for family in nested:
        parameter = family.get_Parameter(DB.BuiltInParameter.FAMILY_SHARED)
        if parameter is not None and parameter.AsInteger() == 1:
            shared += 1
    row['nestedShared'] = shared
    row['connectors'] = DB.FilteredElementCollector(famdoc).OfClass(
        DB.ConnectorElement).GetElementCount()
    row['score'] = (row['formulas'] * 3 + row['lookupTables'] * 20
                    + shared * 10 + row['connectors'] * 2
                    + min(row['types'], 20))


def pick_families(rows):
    ranked = sorted((row for row in rows if row['score'] is not None),
                    key=lambda row: (-row['score'], row['name'], row['id']))
    picked = []
    by_id = {}

    def add(row, reason):
        if row['id'] in by_id:
            by_id[row['id']]['reason'] += '; ' + reason
        elif len(picked) < 6:
            item = dict(row)
            item['reason'] = reason
            item['file'] = None
            picked.append(item)
            by_id[row['id']] = item

    for index, row in enumerate(ranked[:2]):
        add(row, 'overall score rank {0}'.format(index + 1))
    for predicate, reason in (
            (lambda row: row['lookupTables'] > 0, 'lookup tables'),
            (lambda row: row['connectors'] > 0 and row['formulas'] > 0,
             'connectors and formulas')):
        candidates = [row for row in ranked if predicate(row)]
        if candidates:
            add(candidates[0], reason)
    openings = [row for row in ranked if row['isDoorOrWindow']]
    if openings:
        openings.sort(key=lambda row: (-row['formulas'], -row['score'],
                                       row['name'], row['id']))
        add(openings[0], 'Doors or Windows formula rank 1')
    shared = [row for row in ranked if row['nestedShared'] > 0]
    if shared:
        add(shared[0], 'nested shared families')
    return picked


def safe_filename(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip().rstrip('. ')
    if not name:
        name = 'family'
    stem = name.split('.')[0].upper()
    if stem in ('CON', 'PRN', 'AUX', 'NUL') or re.match(r'^(COM|LPT)[1-9]$', stem):
        name = '_' + name
    return name + '.rfa'


def main():
    out = os.environ.get('RVT_EXTRACT_OUT')
    if not out:
        raise RuntimeError('RVT_EXTRACT_OUT is required')
    out = os.path.abspath(out)
    if not os.path.isdir(out):
        os.makedirs(out)
    doc = get_document()
    if doc.IsFamilyDocument:
        raise RuntimeError('Family extraction requires a project document')
    rows, errors, families = [], [], {}
    opening_ids = (element_id(DB.ElementId(DB.BuiltInCategory.OST_Doors)),
                   element_id(DB.ElementId(DB.BuiltInCategory.OST_Windows)))
    for family in DB.FilteredElementCollector(doc).OfClass(DB.Family):
        row = {'id': element_id(family.Id), 'name': unicode(family.Name),
               'category': None, 'parameters': None, 'formulas': None,
               'types': None, 'lookupTables': None, 'nested': None,
               'nestedShared': None, 'connectors': None, 'score': None,
               'isDoorOrWindow': False, 'errors': []}
        famdoc = None
        try:
            if family.IsInPlace or not family.IsEditable:
                continue
            rows.append(row)
            families[row['id']] = family
            category = family.FamilyCategory
            if category is not None:
                row['category'] = unicode(category.Name)
                row['isDoorOrWindow'] = element_id(category.Id) in opening_ids
            famdoc = doc.EditFamily(family)
            measure(famdoc, row)
        except Exception as exc:
            if row not in rows:
                rows.append(row)
            row['score'] = None
            record_error(row, 'scan', exc, errors)
        finally:
            if famdoc is not None:
                try:
                    famdoc.Close(False)
                except Exception as exc:
                    record_error(row, 'close after scan', exc, errors)

    picked = pick_families(rows)
    used_names = set()
    for item in picked:
        famdoc = None
        try:
            filename = safe_filename(item['name'])
            while filename.lower() in used_names:
                filename = os.path.splitext(filename)[0] + '_' + str(item['id']) + '.rfa'
            used_names.add(filename.lower())
            famdoc = doc.EditFamily(families[item['id']])
            options = DB.SaveAsOptions()
            options.OverwriteExistingFile = True
            famdoc.SaveAs(os.path.join(out, filename), options)
            item['file'] = filename
            print('PICKED {0} {1} formulas={2} lookups={3} nested={4} connectors={5} types={6}'.format(
                item['name'], item['reason'], item['formulas'], item['lookupTables'],
                item['nested'], item['connectors'], item['types']))
        except Exception as exc:
            record_error(item, 'save', exc, errors)
        finally:
            if famdoc is not None:
                try:
                    famdoc.Close(False)
                except Exception as exc:
                    record_error(item, 'close after save', exc, errors)

    result = {'families': rows, 'picked': picked, 'errors': errors}
    with codecs.open(os.path.join(out, 'families_index.json'), 'w', 'utf-8') as stream:
        stream.write(unicode(json.dumps(result, ensure_ascii=False, indent=2)))
    print('SCANNED={0} PICKED={1} SAVED={2} ERRORS={3}'.format(
        len(rows), len(picked), sum(1 for item in picked if item['file']), len(errors)))


main()
