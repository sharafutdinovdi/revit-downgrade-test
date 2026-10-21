from __future__ import print_function, division

import codecs
import datetime
import json
import math
import os
import time
from collections import Counter, defaultdict

import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
from Autodesk.Revit import DB
from System import Enum


GEOMETRY = True
GEOMETRY = GEOMETRY and os.environ.get('RVT_DUMP_GEOMETRY', '1') != '0'
TRACE = os.environ.get('RVT_DUMP_TRACE', '0') == '1'
SKIP_GEOMETRY_CATEGORIES = set(s.strip() for s in os.environ.get('RVT_DUMP_SKIP_GEOMETRY_CATEGORIES', '').split(',') if s.strip())
SKIP_PARAM_CATEGORIES = set(s.strip() for s in os.environ.get('RVT_DUMP_SKIP_PARAM_CATEGORIES', '').split(',') if s.strip())
FIELD_ERRORS = Counter()
ERRORS = []
LOG = None
DOC = None
ELEMENTS = {}
LEVELS = {}
PHASES = {}
OPTIONS = {}
WORKSETS = {}
TYPES = {}
FAMILIES = {}
NAME_CACHE = {}
GEOM_OPTIONS = DB.Options()
GEOM_OPTIONS.DetailLevel = DB.ViewDetailLevel.Fine
GEOM_OPTIONS.IncludeNonVisibleObjects = False
GEOM_OPTIONS.ComputeReferences = False


def text(value):
    return None if value is None else unicode(value)


def eid(value):
    if value is None:
        return None
    try:
        return int(value.Value)
    except AttributeError:
        return int(value.IntegerValue)


def log(message):
    if LOG is not None:
        LOG.write(text(message) + '\n')
        LOG.flush()


def error(identifier, message):
    entry = {'id': identifier, 'message': text(message)}
    ERRORS.append(entry)
    log('ERROR {0}: {1}'.format(identifier, message))


def attempt(identifier, operation, default=None):
    try:
        return operation()
    except Exception as exc:
        error(identifier, exc)
        return default


def field_attempt(field, operation, default=None):
    try:
        return operation()
    except Exception as exc:
        FIELD_ERRORS['{0}: {1}'.format(field, text(exc))] += 1
        return default


def prop(obj, name, default=None):
    # Some API properties exist on a class but are invalid for a particular element.
    try:
        return getattr(obj, name, default)
    except Exception as exc:
        FIELD_ERRORS['{0}: {1}'.format(name, text(exc))] += 1
        return default


def name(obj):
    if obj is None:
        return None
    try:
        return text(DB.Element.Name.GetValue(obj))
    except Exception:
        return text(prop(obj, 'Name'))


def ref_name(identifier):
    key = eid(identifier)
    if key is None or key < 0:
        return None
    if key not in NAME_CACHE:
        NAME_CACHE[key] = name(ELEMENTS.get(key) or DOC.GetElement(identifier))
    return NAME_CACHE[key]


def enum_name(enum_type, value):
    try:
        return text(Enum.GetName(enum_type, Enum.ToObject(enum_type, value)))
    except Exception:
        return None


def bic(category):
    if category is None:
        return None
    return enum_name(DB.BuiltInCategory, eid(category.Id))


def category_name(element):
    category = prop(element, 'Category')
    return field_attempt('categoryName', lambda: text(category.Name)) if category is not None else '<none>'


def convert(value, unit):
    return round(float(DB.UnitUtils.ConvertFromInternalUnits(value, unit)), 3)


def xyz(point):
    return [convert(v, DB.UnitTypeId.Millimeters) for v in (point.X, point.Y, point.Z)]


def identity(element):
    return {'id': eid(element.Id), 'uniqueId': text(element.UniqueId), 'name': name(element)}


def double_value(parameter, value):
    spec = parameter.Definition.GetDataType()
    if DB.UnitUtils.IsMeasurableSpec(spec):
        display_unit = parameter.GetUnitTypeId()
        for source, target in ((DB.SpecTypeId.Length, DB.UnitTypeId.Millimeters),
                               (DB.SpecTypeId.Area, DB.UnitTypeId.SquareMeters),
                               (DB.SpecTypeId.Volume, DB.UnitTypeId.CubicMeters),
                               (DB.SpecTypeId.Angle, DB.UnitTypeId.Degrees)):
            if spec == source or display_unit == target:
                return convert(value, target)
    return round(float(value), 6)


def prop_value_string(parameter):
    if parameter.StorageType in (DB.StorageType.Double, DB.StorageType.Integer, DB.StorageType.ElementId):
        return parameter.AsValueString()
    return None


def parameter_data(parameter, definition=None, pid=None):
    if definition is None:
        definition = parameter.Definition
    if definition is None:
        return None
    if pid is None:
        pid = eid(parameter.Id)
    shared = bool(parameter.IsShared)
    storage = text(parameter.StorageType)
    identifier = text(parameter.GUID) if shared else (enum_name(DB.BuiltInParameter, pid) if pid < 0 else None)
    value_string = prop_value_string(parameter)
    value = None
    if parameter.HasValue:
        if storage == 'Double':
            value = double_value(parameter, parameter.AsDouble())
        elif storage == 'Integer':
            value = int(parameter.AsInteger())
        elif storage == 'String':
            value = text(parameter.AsString())
        elif storage == 'ElementId':
            target = parameter.AsElementId()
            value = eid(target)
            if value_string is None:
                value_string = ref_name(target)
    return {'name': text(definition.Name), 'id': identifier or text(pid),
            'isShared': shared, 'isReadOnly': bool(parameter.IsReadOnly),
            'storageType': storage, 'value': value,
            'valueString': text(value_string) if value_string is not None else text(value)}


def parameters(element):
    if category_name(element) in SKIP_PARAM_CATEGORIES:
        return None
    result = []
    for parameter in element.Parameters:
        def read():
            # Validate the definition and ID before entering unsafe native value access.
            definition = parameter.Definition
            if definition is None:
                return None
            pid = eid(parameter.Id)
            if TRACE:
                log('TRACEP {0} {1}'.format(eid(element.Id), text(definition.Name)))
            return parameter_data(parameter, definition, pid)
        value = field_attempt('parameters', read)
        if value is not None:
            result.append(value)
    return sorted(result, key=lambda p: (p['name'], p['id']))


def location(element):
    loc = element.Location
    if isinstance(loc, DB.LocationPoint):
        return {'kind': 'point', 'point': field_attempt('location.point', lambda: xyz(loc.Point)),
                'rotationDeg': field_attempt('rotationDeg', lambda: round(float(loc.Rotation) * 180 / math.pi, 3))}
    if isinstance(loc, DB.LocationCurve):
        curve = loc.Curve
        return {'kind': 'curve',
                'start': field_attempt('location.start', lambda: xyz(curve.GetEndPoint(0))),
                'end': field_attempt('location.end', lambda: xyz(curve.GetEndPoint(1)))}
    return {'kind': 'none'}


def bbox(element):
    box = element.get_BoundingBox(None)
    if box is None:
        return None
    # Respect transformed bounds instead of assuming an identity transform.
    points = [box.Transform.OfPoint(DB.XYZ(x, y, z))
              for x in (box.Min.X, box.Max.X) for y in (box.Min.Y, box.Max.Y)
              for z in (box.Min.Z, box.Max.Z)]
    return {'min': xyz(DB.XYZ(min(p.X for p in points), min(p.Y for p in points), min(p.Z for p in points))),
            'max': xyz(DB.XYZ(max(p.X for p in points), max(p.Y for p in points), max(p.Z for p in points)))}


def geometry_raw(element):
    result = {'solidCount': 0, 'faceCount': 0, 'edgeCount': 0, 'volume': 0.0, 'area': 0.0}
    def visit(items):
        if items is None:
            return
        for item in items:
            if isinstance(item, DB.GeometryInstance):
                visit(item.GetInstanceGeometry())
            elif isinstance(item, DB.Solid) and item.Volume > 0:
                result['solidCount'] += 1
                result['faceCount'] += int(item.Faces.Size)
                result['edgeCount'] += int(item.Edges.Size)
                result['volume'] += float(item.Volume)
                result['area'] += float(item.SurfaceArea)
    visit(element.get_Geometry(GEOM_OPTIONS))
    return result


def geometry(element):
    raw = geometry_raw(element)
    return {'solidCount': raw['solidCount'], 'faceCount': raw['faceCount'], 'edgeCount': raw['edgeCount'],
            'volumeM3': convert(raw['volume'], DB.UnitTypeId.CubicMeters),
            'areaM2': convert(raw['area'], DB.UnitTypeId.SquareMeters)}


def connector_data(connector):
    origin = prop(connector, 'Origin')
    if origin is None:
        cs = prop(connector, 'CoordinateSystem')
        origin = cs.Origin if cs is not None else None
    domain = text(prop(connector, 'Domain'))
    system = None
    for field in ('PipeSystemType', 'DuctSystemType', 'ElectricalSystemType', 'SystemType'):
        value = prop(connector, field)
        if value is not None:
            system = text(value)
            break
    shape = text(prop(connector, 'Shape'))
    result = {'domain': domain, 'systemType': system, 'shape': shape,
              'origin': xyz(origin) if origin is not None else None,
              'isConnected': nullable_bool(connector, 'IsConnected')}
    for field, output in (('Radius', 'radiusMm'), ('Width', 'widthMm'), ('Height', 'heightMm')):
        value = prop(connector, field)
        if value is not None:
            result[output] = convert(value, DB.UnitTypeId.Millimeters)
    return result


def connectors(element):
    if isinstance(element, DB.ConnectorElement):
        return [connector_data(element)]
    mep = getattr(element, 'MEPModel', None)
    manager = getattr(mep, 'ConnectorManager', None) if mep is not None else None
    if manager is None:
        return []
    return sorted([connector_data(c) for c in manager.Connectors],
                  key=lambda c: json.dumps(c, sort_keys=True))


def nullable_bool(element, field):
    value = prop(element, field)
    return bool(value) if value is not None else None


def instance(element):
    key = eid(element.Id)
    typ = field_attempt('type', lambda: TYPES.get(eid(element.GetTypeId())))
    option = prop(element, 'DesignOption')
    host = prop(element, 'Host')
    parent = prop(element, 'SuperComponent')
    category = category_name(element)
    return {'id': key, 'uniqueId': field_attempt('uniqueId', lambda: text(element.UniqueId)), 'categoryName': category,
            'builtInCategory': field_attempt('builtInCategory', lambda: bic(element.Category)),
            'familyName': text(prop(typ, 'FamilyName')) if typ is not None else None,
            'typeName': name(typ), 'typeUniqueId': field_attempt('typeUniqueId', lambda: text(typ.UniqueId)) if typ is not None else None,
            'levelName': field_attempt('levelName', lambda: LEVELS.get(eid(element.LevelId))),
            'worksetName': field_attempt('worksetName', lambda: WORKSETS.get(eid(element.WorksetId))),
            'phaseCreated': field_attempt('phaseCreated', lambda: PHASES.get(eid(element.CreatedPhaseId))),
            'phaseDemolished': field_attempt('phaseDemolished', lambda: PHASES.get(eid(element.DemolishedPhaseId))),
            'designOption': field_attempt('designOption', lambda: OPTIONS.get(eid(option.Id))) if option is not None else None,
            'groupId': field_attempt('groupId', lambda: eid(element.GroupId)),
            'ownerViewId': field_attempt('ownerViewId', lambda: eid(element.OwnerViewId)),
            'location': field_attempt('location', lambda: location(element)),
            'bbox': field_attempt('bbox', lambda: bbox(element)),
            'geometry': field_attempt('geometry', lambda: geometry(element)) if GEOMETRY and category not in SKIP_GEOMETRY_CATEGORIES else None,
            'hostId': field_attempt('hostId', lambda: eid(host.Id)) if host is not None else None,
            'superComponentId': field_attempt('superComponentId', lambda: eid(parent.Id)) if parent is not None else None,
            'mirrored': field_attempt('mirrored', lambda: nullable_bool(element, 'Mirrored')),
            'facingFlipped': field_attempt('facingFlipped', lambda: nullable_bool(element, 'FacingFlipped')),
            'handFlipped': field_attempt('handFlipped', lambda: nullable_bool(element, 'HandFlipped')),
            'connectors': field_attempt('connectors', lambda: connectors(element)),
            'parameters': field_attempt('parameters', lambda: parameters(element))}


def type_data(element, counts):
    return {'id': eid(element.Id), 'uniqueId': text(element.UniqueId), 'categoryName': category_name(element),
            'familyName': text(prop(element, 'FamilyName')), 'name': name(element),
            'parameters': field_attempt('parameters', lambda: parameters(element)), 'instanceCount': counts[eid(element.Id)]}


def project_data(all_elements, worksets):
    info = DOC.ProjectInformation if not DOC.IsFamilyDocument else None
    project_info = {}
    for key, field in (('name', 'Name'), ('number', 'Number'), ('client', 'ClientName'), ('address', 'Address'),
                       ('status', 'Status'), ('author', 'Author'), ('buildingName', 'BuildingName'),
                       ('organizationName', 'OrganizationName')):
        project_info[key] = text(prop(info, field)) if info is not None else None
    levels, options = [], []
    for element in all_elements:
        def collect():
            if isinstance(element, DB.Level):
                row = identity(element)
                row['elevationMm'] = convert(element.Elevation, DB.UnitTypeId.Millimeters)
                levels.append(row)
            elif isinstance(element, DB.DesignOption):
                parameter = element.get_Parameter(DB.BuiltInParameter.OPTION_SET_ID)
                if parameter is not None:
                    if parameter.Definition is None:
                        parameter = None
                    else:
                        eid(parameter.Id)
                options.append({'id': eid(element.Id), 'name': name(element),
                                'setId': eid(parameter.AsElementId()) if parameter is not None else None})
        attempt(eid(element.Id), collect)
    unit = attempt('units', lambda: text(DOC.GetUnits().GetFormatOptions(DB.SpecTypeId.Length).GetUnitTypeId().TypeId))
    path = None
    if DOC.IsWorkshared:
        path = attempt('worksharingPath', lambda: text(DB.ModelPathUtils.ConvertModelPathToUserVisiblePath(DOC.GetWorksharingCentralModelPath())))
    return {'projectInfo': project_info, 'units': {'lengthDisplayUnit': unit}, 'levels': levels,
            'phases': [{'id': eid(p.Id), 'name': name(p)} for p in DOC.Phases], 'designOptions': options,
            'worksets': [{'id': eid(w.Id), 'name': text(w.Name), 'kind': text(w.Kind), 'isOpen': bool(w.IsOpen)} for w in worksets],
            'isWorkshared': bool(DOC.IsWorkshared), 'worksharingPath': path}


def family_data(element):
    symbols = [TYPES.get(eid(i)) for i in element.GetFamilySymbolIds()]
    names = sorted(name(t) for t in symbols if t is not None)
    row = identity(element)
    row.update(categoryName=text(element.FamilyCategory.Name) if element.FamilyCategory is not None else None,
               isInPlace=bool(element.IsInPlace), typeNames=names, typeCount=len(names))
    return row


def material_data(element):
    row = identity(element)
    color = element.Color
    row.update(materialClass=text(element.MaterialClass),
               color=[int(color.Red), int(color.Green), int(color.Blue)] if color.IsValid else None)
    return row


def view_data(element, annotations):
    row = identity(element)
    row.update(viewType=text(element.ViewType), isTemplate=bool(element.IsTemplate),
               viewTemplateId=eid(element.ViewTemplateId), scale=int(prop(element, 'Scale', 0)),
               detailLevel=text(element.DetailLevel), discipline=text(prop(element, 'Discipline')),
               sheetNumber=text(prop(element, 'SheetNumber')), annotationCountsByCategory=dict(annotations[eid(element.Id)]))
    return row


def sheet_data(element, titleblocks, viewports, schedules):
    row = identity(element)
    row.update(number=text(element.SheetNumber), titleBlockType=titleblocks.get(eid(element.Id)),
               viewportViewIds=sorted(viewports[eid(element.Id)]), scheduleIds=sorted(schedules[eid(element.Id)]))
    return row


def schedule_category(definition):
    identifier = definition.CategoryId
    if eid(identifier) == -1:
        return None
    category = DB.Category.GetCategory(DOC, identifier)
    return text(category.Name) if category is not None else None


def schedule_data(element):
    definition = element.Definition
    row = identity(element)
    section = element.GetTableData().GetSectionData(DB.SectionType.Body)
    row.update(categoryName=schedule_category(definition),
               fieldNames=[text(definition.GetField(i).GetName()) for i in definition.GetFieldOrder()],
               filterCount=int(definition.GetFilterCount()), sortGroupFieldCount=int(definition.GetSortGroupFieldCount()),
               isKeySchedule=bool(definition.IsKeySchedule), rowCount=int(section.NumberOfRows), columnCount=int(section.NumberOfColumns))
    return row


def link_data(element):
    if isinstance(element, DB.RevitLinkType):
        kind = 'rvt'
    elif isinstance(element, DB.ImageType):
        kind = 'image'
    elif isinstance(element, DB.CADLinkType):
        kind = 'cad'
    else:
        return None
    external = DB.ExternalFileUtils.GetExternalFileReference(DOC, element.Id) if DB.ExternalFileUtils.IsExternalFileReference(DOC, element.Id) else None
    status = text(external.GetLinkedFileStatus()) if external is not None else ('Imported' if kind == 'cad' else None)
    if kind == 'image':
        status = text(prop(element, 'Status')) or text(prop(element, 'Source'))
    loaded = DB.RevitLinkType.IsLoaded(DOC, element.Id) if kind == 'rvt' else status in ('Loaded', 'Imported', 'Import')
    return {'id': eid(element.Id), 'name': name(element), 'kind': kind, 'status': status,
            'pathType': text(external.PathType) if external is not None else text(prop(element, 'PathType')), 'isLoaded': bool(loaded)}


def group_data(element):
    row = identity(element)
    row.update(kind='detail' if bic(element.Category) == 'OST_IOSDetailGroups' else 'model',
               memberCount=len(list(element.GetMemberIds())))
    return row


def warnings_data():
    grouped = {}
    for warning in DOC.GetWarnings():
        description = text(warning.GetDescriptionText())
        row = grouped.setdefault(description, {'description': description, 'severity': text(warning.GetSeverity()),
                                               'elementIds': set(), 'count': 0})
        row['count'] += 1
        row['elementIds'].update(eid(i) for i in warning.GetFailingElements())
        row['elementIds'].update(eid(i) for i in warning.GetAdditionalElements())
    for row in grouped.values():
        row['elementIds'] = sorted(row['elementIds'])
    return sorted(grouped.values(), key=lambda r: r['description'])


def project_parameters(all_elements):
    if DOC.IsFamilyDocument:
        return []
    definitions = {eid(e.Id): e for e in all_elements if isinstance(e, DB.ParameterElement)}
    result = []
    iterator = DOC.ParameterBindings.ForwardIterator()
    iterator.Reset()
    while iterator.MoveNext():
        definition, binding = iterator.Key, iterator.Current
        def build():
            pe = definitions.get(eid(definition.Id))
            shared = isinstance(pe, DB.SharedParameterElement)
            # StorageType comes from a bound parameter when available, or its spec.
            storage = None
            for category in binding.Categories:
                storage = BOUND_STORAGE.get((text(definition.Name), eid(category.Id)))
                if storage is not None:
                    break
            if storage is None:
                spec = definition.GetDataType()
                if DB.UnitUtils.IsMeasurableSpec(spec):
                    storage = 'Double'
                elif spec == DB.SpecTypeId.Boolean.YesNo or spec == DB.SpecTypeId.Int.Integer:
                    storage = 'Integer'
                elif spec == DB.SpecTypeId.String.Text or spec == DB.SpecTypeId.String.MultilineText or spec == DB.SpecTypeId.String.Url:
                    storage = 'String'
                else:
                    storage = 'ElementId'
            return {'name': text(definition.Name), 'isShared': shared,
                    'guid': text(pe.GuidValue) if shared else None, 'storageType': storage,
                    'categories': sorted(text(c.Name) for c in binding.Categories),
                    'isInstance': isinstance(binding, DB.InstanceBinding)}
        value = attempt('projectParameter:' + text(definition.Name), build)
        if value is not None:
            result.append(value)
    return sorted(result, key=lambda p: p['name'])


BOUND_STORAGE = {}


def family_parameter(parameter, family_types):
    definition = parameter.Definition
    if definition is None:
        return None
    eid(parameter.Id)
    if TRACE:
        log('TRACEP {0} {1}'.format(eid(DOC.OwnerFamily.Id), text(definition.Name)))
    values = {}
    for typ in family_types:
        storage = text(parameter.StorageType)
        value = None
        if typ.HasValue(parameter):
            if storage == 'Double':
                value = typ.AsValueString(parameter)
                if value is None:
                    value = text(double_value(parameter, typ.AsDouble(parameter)))
            elif storage == 'Integer':
                value = text(typ.AsInteger(parameter))
            elif storage == 'String':
                value = text(typ.AsString(parameter))
            elif storage == 'ElementId':
                target = typ.AsElementId(parameter)
                value = ref_name(target) or text(eid(target))
        values[text(typ.Name)] = text(value)
    return {'name': text(parameter.Definition.Name), 'isShared': bool(parameter.IsShared),
            'guid': text(parameter.GUID) if parameter.IsShared else None,
            'isInstance': bool(parameter.IsInstance), 'storageType': text(parameter.StorageType),
            'formula': text(parameter.Formula), 'isReporting': bool(parameter.IsReporting),
            'isDeterminedByFormula': bool(parameter.IsDeterminedByFormula),
            'groupName': parameter_group(parameter.Definition),
            'valuesByType': values}


def parameter_group(definition):
    if hasattr(definition, 'GetGroupTypeId'):
        group = definition.GetGroupTypeId()
        return text(DB.LabelUtils.GetLabelForGroup(group)) if group.TypeId else None
    return text(DB.LabelUtils.GetLabelFor(definition.ParameterGroup))


def family_document(all_elements, instance_counts):
    manager, owner = DOC.FamilyManager, DOC.OwnerFamily
    family_types = sorted(list(manager.Types), key=lambda t: text(t.Name))
    result = {'categoryName': text(owner.FamilyCategory.Name) if owner.FamilyCategory is not None else None,
              'familyName': name(owner), 'isParametric': bool(prop(owner, 'IsParametric', True)),
              'parameters': [], 'typeNames': [text(t.Name) for t in family_types], 'nestedFamilies': [],
              'referencePlaneCount': sum(isinstance(e, DB.ReferencePlane) for e in all_elements),
              'dimensionCount': sum(isinstance(e, DB.Dimension) for e in all_elements),
              'lookupTables': [], 'connectors': [], 'geometryByType': {}}
    for parameter in manager.Parameters:
        row = attempt('familyParameter:' + text(parameter.Definition.Name), lambda: family_parameter(parameter, family_types))
        if row is not None:
            result['parameters'].append(row)
    result['parameters'].sort(key=lambda p: p['name'])
    for fam in FAMILIES.values():
        if eid(fam.Id) == eid(owner.Id):
            continue
        def nested():
            def read_shared():
                shared = fam.get_Parameter(DB.BuiltInParameter.FAMILY_SHARED)
                if shared is None or shared.Definition is None:
                    return None
                eid(shared.Id)
                return shared.AsInteger() == 1
            return {'name': name(fam), 'isShared': field_attempt('isShared', read_shared),
                    'categoryName': text(fam.FamilyCategory.Name) if fam.FamilyCategory is not None else None,
                    'instanceCount': sum(instance_counts[eid(i)] for i in fam.GetFamilySymbolIds())}
        row = attempt(eid(fam.Id), nested)
        if row is not None:
            result['nestedFamilies'].append(row)
    def lookup():
        tables = DB.FamilySizeTableManager.GetFamilySizeTableManager(DOC, owner.Id)
        return sorted(text(n) for n in tables.GetAllSizeTableNames()) if tables is not None else []
    result['lookupTables'] = attempt('familyLookupTables', lookup, [])
    for element in all_elements:
        if isinstance(element, DB.ConnectorElement):
            result['connectors'].extend(field_attempt('connectors', lambda: connectors(element), []))
    if GEOMETRY:
        transaction = DB.Transaction(DOC, 'Inspect family type geometry')
        started = False
        try:
            transaction.Start()
            started = True
            for typ in family_types:
                def inspect():
                    manager.CurrentType = typ
                    DOC.Regenerate()
                    totals = {'solidCount': 0, 'faceCount': 0, 'volume': 0.0}
                    # Type changes can recreate geometry elements, so refresh this collector.
                    for element in DB.FilteredElementCollector(DOC).WhereElementIsNotElementType():
                        if isinstance(element, (DB.View, DB.Family, DB.Material, DB.Level)):
                            continue
                        if TRACE:
                            log('TRACE {0} {1}'.format(eid(element.Id), category_name(element)))
                        if category_name(element) in SKIP_GEOMETRY_CATEGORIES:
                            continue
                        raw = field_attempt('geometry', lambda: geometry_raw(element))
                        if raw is not None:
                            for field in totals:
                                totals[field] += raw[field]
                    return {'solidCount': totals['solidCount'], 'faceCount': totals['faceCount'],
                            'volumeM3': convert(totals['volume'], DB.UnitTypeId.CubicMeters)}
                row = attempt('familyType:' + text(typ.Name), inspect)
                if row is not None:
                    result['geometryByType'][text(typ.Name)] = row
        except Exception as exc:
            error('familyGeometry', exc)
        finally:
            if started:
                attempt('familyGeometryRollback', lambda: transaction.RollBack())
            transaction.Dispose()
    return result


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


def main():
    global DOC, LOG, ELEMENTS, TYPES, FAMILIES, LEVELS, PHASES, OPTIONS, WORKSETS
    started = time.time()
    started_utc = datetime.datetime.utcnow().isoformat() + 'Z'
    DOC = get_document()
    app = __revit__.Application
    out_dir = os.environ.get('RVT_DUMP_OUT') or os.path.join(os.environ['LOCALAPPDATA'], 'revit-downgrade-test', 'dumps')
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    title = text(DOC.Title)
    stem, extension = os.path.splitext(title)
    if extension.lower() not in ('.rvt', '.rfa', '.rft', '.rte'):
        stem = title
    stem = ''.join('_' if c in '<>:"/\\|?*' else c for c in stem)
    filename = stem + '__' + text(app.VersionNumber)
    LOG = codecs.open(os.path.join(out_dir, filename + '.log'), 'w', 'utf-8')
    try:
        log('Started UTC: ' + started_utc)
        all_elements = list(DB.FilteredElementCollector(DOC).WhereElementIsNotElementType())
        all_types = list(DB.FilteredElementCollector(DOC).WhereElementIsElementType())
        ELEMENTS = {eid(e.Id): e for e in all_elements + all_types}
        TYPES = {eid(e.Id): e for e in all_types}
        FAMILIES = {eid(e.Id): e for e in all_elements if isinstance(e, DB.Family)}
        LEVELS = {eid(e.Id): name(e) for e in all_elements if isinstance(e, DB.Level)}
        PHASES = {eid(e.Id): name(e) for e in DOC.Phases}
        OPTIONS = {eid(e.Id): name(e) for e in all_elements if isinstance(e, DB.DesignOption)}
        workset_table = DOC.GetWorksetTable()
        worksets = [workset_table.GetWorkset(w.Id) for w in DB.FilteredWorksetCollector(DOC)] if DOC.IsWorkshared else []
        WORKSETS = {eid(w.Id): text(w.Name) for w in worksets}
        log('Collected {0} instances and {1} types in {2:.3f}s'.format(len(all_elements), len(all_types), time.time() - started))
        counts, annotations = Counter(), defaultdict(Counter)
        titleblocks, viewports, schedules_on_sheet = {}, defaultdict(list), defaultdict(list)
        for element in all_elements:
            key = eid(element.Id)
            def index():
                counts[eid(element.GetTypeId())] += 1
                owner_id = eid(element.OwnerViewId)
                category = prop(element, 'Category')
                if category is not None and category.CategoryType == DB.CategoryType.Annotation:
                    annotations[owner_id][category_name(element)] += 1
                if bic(category) == 'OST_TitleBlocks':
                    titleblocks[owner_id] = name(TYPES.get(eid(element.GetTypeId())))
                if isinstance(element, DB.Viewport):
                    viewports[eid(element.SheetId)].append(eid(element.ViewId))
                elif isinstance(element, DB.ScheduleSheetInstance):
                    schedules_on_sheet[owner_id].append(eid(element.ScheduleId))
            field_attempt('index', index)
        result = {'meta': {'schema': 1, 'title': title, 'pathName': text(DOC.PathName),
                           'isFamilyDocument': bool(DOC.IsFamilyDocument), 'revitVersionNumber': text(app.VersionNumber),
                           'revitVersionBuild': text(app.VersionBuild), 'revitVersionName': text(app.VersionName),
                           'dumpStartedUtc': started_utc, 'dumpSeconds': 0, 'elementCount': 0, 'typeCount': 0,
                           'errors': ERRORS, 'fieldErrors': FIELD_ERRORS, 'unitsLength': 'mm'},
                  'project': project_data(all_elements, worksets), 'categories': {}, 'elements': [], 'types': [],
                  'families': [], 'materials': [], 'views': [], 'sheets': [], 'schedules': [],
                  'links': [], 'groups': [], 'warnings': attempt('warnings', warnings_data, []), 'projectParameters': []}
        def add(section, element, operation):
            row = attempt(eid(element.Id), operation)
            if row is not None:
                result[section].append(row)
        for index, element in enumerate(all_elements):
            if TRACE:
                log('TRACE {0} {1}'.format(eid(element.Id), category_name(element)))
            if (index + 1) % 5000 == 0:
                log('Progress {0}/{1}, elapsed {2:.3f}s, failures {3}'.format(index + 1, len(all_elements), time.time() - started, len(ERRORS)))
            try:
                if isinstance(element, DB.View):
                    add('views', element, lambda: view_data(element, annotations))
                    if isinstance(element, DB.ViewSheet):
                        add('sheets', element, lambda: sheet_data(element, titleblocks, viewports, schedules_on_sheet))
                    elif isinstance(element, DB.ViewSchedule):
                        add('schedules', element, lambda: schedule_data(element))
                elif isinstance(element, DB.Material):
                    add('materials', element, lambda: material_data(element))
                elif isinstance(element, DB.Family):
                    add('families', element, lambda: family_data(element))
                elif not isinstance(element, (DB.Level, DB.ElementType)):
                    row = attempt(eid(element.Id), lambda: instance(element))
                    if row is not None:
                        result['elements'].append(row)
                        for parameter in row['parameters'] or []:
                            BOUND_STORAGE[(parameter['name'], field_attempt('categoryId', lambda: eid(element.Category.Id)))] = parameter['storageType']
                if isinstance(element, DB.Group):
                    add('groups', element, lambda: group_data(element))
            except Exception as exc:
                error(eid(element.Id), exc)
        for element in all_types:
            if TRACE:
                log('TRACE {0} {1}'.format(eid(element.Id), category_name(element)))
            add('types', element, lambda: type_data(element, counts))
            if isinstance(element, (DB.RevitLinkType, DB.CADLinkType, DB.ImageType)):
                add('links', element, lambda: link_data(element))
        for section, field in (('elements', 'instances'), ('types', 'types')):
            for element in result[section]:
                category = element['categoryName']
                entry = result['categories'].setdefault(category, {'builtInCategory': bic(prop(ELEMENTS[element['id']], 'Category')), 'instances': 0, 'types': 0})
                entry[field] += 1
        result['projectParameters'] = attempt('projectParameters', lambda: project_parameters(all_elements), [])
        if DOC.IsFamilyDocument:
            result['family'] = family_document(all_elements, counts)
        result['meta']['elementCount'] = len(result['elements'])
        result['meta']['typeCount'] = len(result['types'])
        result['meta']['dumpSeconds'] = round(time.time() - started, 3)
        json_started = time.time()
        with codecs.open(os.path.join(out_dir, filename + '.json'), 'w', 'utf-8') as stream:
            json.dump(result, stream, indent=None, sort_keys=True, ensure_ascii=False)
        log('Serialization seconds: {0:.3f}'.format(time.time() - json_started))
        log('Completed in {0:.3f}s with {1} failures'.format(time.time() - started, len(ERRORS)))
        print('Dump complete: {0}; failures: {1}'.format(filename + '.json', len(ERRORS)))
    finally:
        LOG.close()
        LOG = None


if __name__ == '__main__':
    main()
