from __future__ import print_function, division

import codecs
import datetime
import json
import os

import clr
clr.AddReference('RevitAPI')
from Autodesk.Revit import DB


ERRORS = []


class ProbeFailuresPreprocessor(DB.IFailuresPreprocessor):
    def PreprocessFailures(self, accessor):
        resolved = False
        unresolved = False
        for failure in accessor.GetFailureMessages():
            severity = failure.GetSeverity()
            ERRORS.append({'step': 'transactionFailure', 'severity': unicode(severity),
                           'error': unicode(failure.GetDescriptionText())})
            if severity == DB.FailureSeverity.Warning:
                accessor.DeleteWarning(failure)
            elif failure.HasResolutions():
                accessor.ResolveFailure(failure)
                resolved = True
            else:
                unresolved = True
        if unresolved:
            return DB.FailureProcessingResult.ProceedWithRollBack
        if resolved:
            return DB.FailureProcessingResult.ProceedWithCommit
        return DB.FailureProcessingResult.Continue


def configure_failures(transaction):
    options = transaction.GetFailureHandlingOptions()
    options.SetFailuresPreprocessor(ProbeFailuresPreprocessor())
    options.SetClearAfterRollback(True)
    transaction.SetFailureHandlingOptions(options)


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


def attempt(step, operation, default=None):
    try:
        return operation()
    except Exception as exc:
        ERRORS.append({'step': step, 'error': unicode(exc)})
        print('ERROR {0}: {1}'.format(step, exc))
        return default


def eid(identifier):
    try:
        return int(identifier.Value)
    except AttributeError:
        return int(identifier.IntegerValue)


def mm(value):
    return DB.UnitUtils.ConvertToInternalUnits(value, DB.UnitTypeId.Millimeters)


def point(value):
    return [DB.UnitUtils.ConvertFromInternalUnits(v, DB.UnitTypeId.Millimeters)
            for v in (value.X, value.Y, value.Z)]


def wall_location(wall):
    curve = wall.Location.Curve
    return {'startMm': point(curve.GetEndPoint(0)), 'endMm': point(curve.GetEndPoint(1))}


def first(doc, cls):
    return DB.FilteredElementCollector(doc).OfClass(cls).WhereElementIsNotElementType().FirstElement()


def require(element, label):
    if element is None:
        raise RuntimeError('No ' + label + ' found')
    return element


def comments(element):
    parameter_id = getattr(DB.BuiltInParameter, 'ALL_MODEL_INSTANCE_COMMENTS', None)
    parameter = element.get_Parameter(parameter_id) if parameter_id is not None else None
    if parameter is None:
        parameter = element.LookupParameter('Comments')
    return require(parameter, 'instance comments parameter')


def set_comments(element, value, lookup=False):
    parameter = require(element.LookupParameter('Comments'), 'Comments parameter') if lookup else comments(element)
    if not parameter.Set(value):
        raise RuntimeError('Comments parameter rejected the value')
    return {'elementId': eid(element.Id), 'value': value}


def project_probe(doc, result):
    steps = result['steps']
    wall = attempt('findWall', lambda: next((e for e in DB.FilteredElementCollector(doc).OfClass(DB.Wall)
                                           if isinstance(e.Location, DB.LocationCurve)), None))
    instance = attempt('findFamilyInstance', lambda: first(doc, DB.FamilyInstance))
    def find_toposolid():
        return next((element for element in DB.FilteredElementCollector(doc).WhereElementIsNotElementType()
                     if element.Category is not None and element.Category.Name == 'Toposolid'), None)
    topo = attempt('findToposolid', find_toposolid)
    stamp = 'probe-' + datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%S%fZ')
    transaction = DB.Transaction(doc, 'Edit compatibility probe')
    try:
        transaction.Start()
        configure_failures(transaction)
        def step(name, operation):
            def run():
                sub = DB.SubTransaction(doc)
                try:
                    sub.Start()
                    value = operation()
                    if sub.Commit() != DB.TransactionStatus.Committed:
                        raise RuntimeError('Step did not commit')
                    return dict({'succeeded': True}, **value)
                finally:
                    if sub.GetStatus() == DB.TransactionStatus.Started:
                        sub.RollBack()
                    sub.Dispose()
            steps[name] = attempt(name, run, {'succeeded': False})
        def move_wall():
            require(wall, 'wall with LocationCurve')
            DB.ElementTransformUtils.MoveElement(doc, wall.Id, DB.XYZ(mm(100), 0, 0))
            doc.Regenerate()
            return {'elementId': eid(wall.Id), 'location': wall_location(wall)}
        step('moveWall', move_wall)
        def move_toposolid():
            require(topo, 'toposolid')
            box = require(topo.get_BoundingBox(None), 'toposolid bounding box')
            before = point(box.Min)
            DB.ElementTransformUtils.MoveElement(doc, topo.Id, DB.XYZ(mm(100), 0, 0))
            doc.Regenerate()
            after = point(require(topo.get_BoundingBox(None), 'moved toposolid bounding box').Min)
            if any(abs(after[i] - before[i] - (100 if i == 0 else 0)) > 0.001 for i in range(3)):
                raise RuntimeError('Toposolid bounding box did not move by 100 mm in X')
            return {'elementId': eid(topo.Id), 'bboxMinBeforeMm': before, 'bboxMinAfterMm': after}
        step('moveToposolid', move_toposolid)
        for label, element in [('wallComments', wall), ('toposolidComments', topo), ('instanceComments', instance)]:
            step(label, lambda e=element, name=label: set_comments(
                require(e, name), stamp, lookup=name == 'toposolidComments'))
        def create_wall():
            level = require(first(doc, DB.Level), 'level')
            p0 = DB.XYZ(mm(100000), 0, level.Elevation)
            p1 = DB.XYZ(mm(103000), 0, level.Elevation)
            created = DB.Wall.Create(doc, DB.Line.CreateBound(p0, p1), level.Id, False)
            return {'elementId': eid(created.Id)}
        step('createWall', create_wall)
        def copy_instance():
            require(instance, 'family instance')
            ids = DB.ElementTransformUtils.CopyElement(doc, instance.Id, DB.XYZ(mm(5000), 0, 0))
            return {'elementIds': [eid(i) for i in ids], 'sourceId': eid(instance.Id)}
        step('copyInstance', copy_instance)
        result['transactionCommitted'] = transaction.Commit() == DB.TransactionStatus.Committed
    except Exception as exc:
        attempt('projectTransaction', lambda: raise_error(exc))
    finally:
        if transaction.GetStatus() == DB.TransactionStatus.Started:
            attempt('projectRollback', lambda: transaction.RollBack())
        transaction.Dispose()
    result['warningsCount'] = attempt('warnings', lambda: len(list(doc.GetWarnings())))
    if wall is not None:
        result['movedWallLocation'] = attempt('wallLocation', lambda: wall_location(wall))


def raise_error(exc):
    raise exc


def geometry_volume(geometry):
    if geometry is None:
        return 0.0
    volume = 0.0
    for item in geometry:
        if isinstance(item, DB.Solid):
            volume += item.Volume
        elif isinstance(item, DB.GeometryInstance):
            nested = item.GetInstanceGeometry()
            try:
                volume += geometry_volume(nested)
            finally:
                if nested is not None:
                    nested.Dispose()
    return volume


def family_volume(doc):
    options = DB.Options()
    options.DetailLevel = DB.ViewDetailLevel.Fine
    volume = 0.0
    try:
        for element in DB.FilteredElementCollector(doc).WhereElementIsNotElementType():
            def measure():
                geometry = element.get_Geometry(options)
                try:
                    return geometry_volume(geometry)
                finally:
                    if geometry is not None:
                        geometry.Dispose()
            volume += attempt('geometry:' + unicode(eid(element.Id)), measure, 0.0)
    finally:
        options.Dispose()
    return DB.UnitUtils.ConvertFromInternalUnits(volume, DB.UnitTypeId.CubicMeters)


def family_values(typ, parameters):
    values = {}
    for parameter in parameters:
        key = unicode(parameter.Definition.Name)
        def read():
            formatted = attempt('parameterValueString:' + key, lambda: typ.AsValueString(parameter))
            raw = None
            if typ.HasValue(parameter):
                if parameter.StorageType == DB.StorageType.Double:
                    raw = typ.AsDouble(parameter)
                elif parameter.StorageType == DB.StorageType.Integer:
                    raw = typ.AsInteger(parameter)
                elif parameter.StorageType == DB.StorageType.String:
                    raw = typ.AsString(parameter)
                elif parameter.StorageType == DB.StorageType.ElementId:
                    raw = eid(typ.AsElementId(parameter))
            return {'valueString': None if formatted is None else unicode(formatted), 'value': raw}
        values[key] = attempt('parameter:' + key, read)
    return values


def family_probe(doc, result):
    manager = doc.FamilyManager
    types = list(manager.Types)
    parameters = list(manager.Parameters)
    current_type = manager.CurrentType
    result['types'] = {}
    transaction = DB.Transaction(doc, 'Inspect family compatibility')
    try:
        transaction.Start()
        configure_failures(transaction)
        for typ in types:
            def inspect():
                manager.CurrentType = typ
                doc.Regenerate()
                return {'parameters': family_values(typ, parameters), 'volumeM3': family_volume(doc)}
            result['types'][unicode(typ.Name)] = attempt('familyType:' + unicode(typ.Name), inspect)
        def perturb():
            if not types:
                raise RuntimeError('No family types found')
            typ = current_type if current_type is not None else types[0]
            manager.CurrentType = typ
            doc.Regenerate()
            parameter = None
            requested_name = os.environ.get('RVT_PROBE_PARAM')
            if requested_name:
                requested = next((p for p in parameters
                                  if unicode(p.Definition.Name) == requested_name), None)
                if requested is None:
                    ERRORS.append({'step': 'lengthChangeParameter',
                                   'error': 'Family parameter not found: ' + requested_name})
                elif requested.Formula:
                    ERRORS.append({'step': 'lengthChangeParameter',
                                   'error': 'Family parameter has a formula: ' + requested_name})
                elif (requested.IsReadOnly or requested.IsReporting
                      or requested.StorageType != DB.StorageType.Double
                      or requested.Definition.GetDataType() != DB.SpecTypeId.Length):
                    ERRORS.append({'step': 'lengthChangeParameter',
                                   'error': 'Family parameter is not an editable length: ' + requested_name})
                else:
                    parameter = requested
            if parameter is None:
                parameter = next((p for p in parameters if not p.IsReporting and not p.Formula
                                  and not p.IsReadOnly and p.StorageType == DB.StorageType.Double
                                  and p.Definition.GetDataType() == DB.SpecTypeId.Length), None)
            require(parameter, 'editable non-formula length parameter')
            before = family_values(typ, parameters)
            volume_before = family_volume(doc)
            old_value = typ.AsDouble(parameter)
            requested_value = os.environ.get('RVT_PROBE_VALUE_MM')
            new_value = mm(float(requested_value)) if requested_value else old_value * 1.1
            manager.Set(parameter, new_value)
            doc.Regenerate()
            after = family_values(typ, parameters)
            changed = {}
            for dependent in parameters:
                key = unicode(dependent.Definition.Name)
                before_value = before[key]
                after_value = after[key]
                if (before_value is not None and after_value is not None
                        and before_value['valueString'] != after_value['valueString']):
                    changed[key] = {'name': key, 'before': before_value['valueString'],
                                    'after': after_value['valueString'],
                                    'formula': unicode(dependent.Formula) if dependent.Formula else None}
            volume_after = family_volume(doc)
            return {'typeName': unicode(typ.Name), 'parameterName': unicode(parameter.Definition.Name),
                    'before': old_value, 'after': typ.AsDouble(parameter),
                    'dependentParametersChanged': changed, 'volumeBeforeM3': volume_before,
                    'volumeAfterM3': volume_after, 'volumeDeltaM3': volume_after - volume_before}
        result['lengthChange'] = attempt('lengthChange', perturb)
    except Exception as exc:
        attempt('familyTransaction', lambda: raise_error(exc))
    finally:
        if transaction.GetStatus() == DB.TransactionStatus.Started:
            result['inspectionRolledBack'] = attempt('familyRollback', lambda: transaction.RollBack()) == DB.TransactionStatus.RolledBack
        transaction.Dispose()
    def tables():
        table_manager = DB.FamilySizeTableManager.GetFamilySizeTableManager(doc, doc.OwnerFamily.Id)
        if table_manager is None:
            return []
        rows = []
        try:
            for name in table_manager.GetAllSizeTableNames():
                def read_table():
                    table = table_manager.GetSizeTable(name)
                    try:
                        return {'name': unicode(name), 'numberOfRows': table.NumberOfRows,
                                'numberOfColumns': table.NumberOfColumns}
                    finally:
                        table.Dispose()
                row = attempt('lookupTable:' + unicode(name), read_table)
                if row is not None:
                    rows.append(row)
        finally:
            table_manager.Dispose()
        return rows
    result['lookupTables'] = attempt('lookupTables', tables, [])


def check_persistence(doc, result):
    persisted = result['persistedAfterReopen']
    for name, step in result['steps'].items():
        def check():
            if not step.get('succeeded') or not result.get('transactionCommitted'):
                return False
            if name == 'copyInstance':
                return bool(step['elementIds']) and all(doc.GetElement(DB.ElementId(i)) is not None
                                                       for i in step['elementIds'])
            element = doc.GetElement(DB.ElementId(step['elementId']))
            if element is None:
                return False
            if name == 'moveWall':
                location = wall_location(element)
                result['reopenedWallLocation'] = location
                expected = step['location']
                return all(abs(a - b) < 0.001 for key in ('startMm', 'endMm')
                           for a, b in zip(location[key], expected[key]))
            if name == 'moveToposolid':
                location = point(require(element.get_BoundingBox(None), 'reopened toposolid bounding box').Min)
                result['reopenedToposolidBboxMinMm'] = location
                return all(abs(a - b) < 0.001 for a, b in zip(location, step['bboxMinAfterMm']))
            if name.endswith('Comments'):
                parameter = (require(element.LookupParameter('Comments'), 'Comments parameter')
                             if name == 'toposolidComments' else comments(element))
                actual = parameter.AsString()
                result.setdefault('reopenedComments', {})[name] = actual
                return actual == step['value']
            return True
        persisted[name] = attempt('reopen:' + name, check, False)


def main():
    out_dir = os.environ['RVT_PROBE_OUT']
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    result = {'steps': {}, 'errors': ERRORS, 'persistedAfterReopen': {}, 'reopened': False}
    doc = attempt('open', get_document)
    if doc is None:
        title = 'model'
    else:
        title = unicode(doc.Title)
    stem, extension = os.path.splitext(title)
    if extension.lower() not in ('.rvt', '.rfa', '.rft', '.rte'):
        stem = title
    stem = ''.join('_' if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in stem).rstrip(' .') or 'model'
    reopened = None
    owned = bool(globals().get('__models__') or os.environ.get('RVT_DUMP_MODEL'))
    try:
        if doc is not None:
            family = doc.IsFamilyDocument
            result['isFamilyDocument'] = family
            attempt('probe', lambda: family_probe(doc, result) if family else project_probe(doc, result))
            saved_path = os.path.join(out_dir, stem + '__probe' + ('.rfa' if family else '.rvt'))
            def save():
                regeneration = DB.Transaction(doc, 'Regenerate before probe save')
                try:
                    regeneration.Start()
                    configure_failures(regeneration)
                    doc.Regenerate()
                    if regeneration.Commit() != DB.TransactionStatus.Committed:
                        raise RuntimeError('Pre-save regeneration did not commit')
                finally:
                    if regeneration.GetStatus() == DB.TransactionStatus.Started:
                        regeneration.RollBack()
                    regeneration.Dispose()
                options = DB.SaveAsOptions()
                try:
                    options.OverwriteExistingFile = True
                    if doc.IsWorkshared:
                        worksharing = DB.WorksharingSaveAsOptions()
                        try:
                            worksharing.SaveAsCentral = True
                            options.SetWorksharingOptions(worksharing)
                        finally:
                            worksharing.Dispose()
                    doc.SaveAs(saved_path, options)
                    return True
                finally:
                    options.Dispose()
            result['saved'] = attempt('saveAs', save, False)
            if result['saved']:
                result['savedFile'] = os.path.basename(saved_path)
                result['reopenSkipped'] = os.environ.get('RVT_PROBE_SKIP_REOPEN', '0') == '1'
                if not result['reopenSkipped']:
                    closed = attempt('closeSaved', lambda: doc.Close(False), False)
                    if closed:
                        doc = None
                        reopened = attempt('reopen', lambda: __revit__.Application.OpenDocumentFile(saved_path))
                        result['reopened'] = reopened is not None
                        if reopened is not None and not family:
                            attempt('persistence', lambda: check_persistence(reopened, result))
            if not family and not result.get('reopenSkipped'):
                for step in result['steps']:
                    result['persistedAfterReopen'].setdefault(step, False)
    finally:
        if reopened is not None:
            attempt('closeReopened', lambda: reopened.Close(False))
        if doc is not None and owned:
            attempt('closeOriginal', lambda: doc.Close(False))
        with codecs.open(os.path.join(out_dir, stem + '__probe.json'), 'w', 'utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    print('PROBE ' + stem + '__probe.json')


main()
