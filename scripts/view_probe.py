from __future__ import print_function, division

import codecs
import json
import math
import os
import uuid
from collections import Counter

import clr
clr.AddReference('RevitAPI')
from Autodesk.Revit import DB
from System.Collections.Generic import List


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


def safe_name(value):
    return ''.join('_' if c in '<>:"/\\|?*' or ord(c) < 32 else c
                   for c in unicode(value)).rstrip(' .') or 'view'


def eid(identifier):
    try:
        return int(identifier.Value)
    except AttributeError:
        return int(identifier.IntegerValue)


def coordinates(point):
    return [point.X, point.Y, point.Z]


def corners(low, high):
    return [DB.XYZ(x, y, z) for x in (low[0], high[0])
            for y in (low[1], high[1]) for z in (low[2], high[2])]


def bounds(points):
    values = [coordinates(point) for point in points]
    return ([min(p[i] for p in values) for i in range(3)],
            [max(p[i] for p in values) for i in range(3)])


def bbox_mm(box):
    return dict((key, [round(value * 304.8, 3) for value in point])
                for key, point in zip(('min', 'max'), box))


def median(values):
    values = sorted(values)
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2


def parameter_string(view, parameter_id):
    parameter = view.get_Parameter(parameter_id)
    if parameter is None:
        return None
    value = parameter.AsValueString()
    if value is None and parameter.StorageType == DB.StorageType.String:
        value = parameter.AsString()
    return None if value is None else unicode(value)


def read_field(result, key, operation):
    try:
        result[key] = operation()
    except Exception as exc:
        result[key] = None
        result['errors'].append('{0}: {1}'.format(key, exc))


def probe_view(doc, view, result):
    counts = Counter()
    category_ids = set()
    boxes = []
    total = 0
    bbox_errors = Counter()
    for element in DB.FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType():
        total += 1
        category = element.Category
        category_name = unicode(category.Name) if category is not None else '<none>'
        counts[category_name] += 1
        if category is not None:
            category_ids.add(eid(category.Id))
        try:
            box = element.get_BoundingBox(view)
            if box is not None:
                low, high = bounds([box.Transform.OfPoint(p) for p in
                                    corners(coordinates(box.Min), coordinates(box.Max))])
                boxes.append({'id': eid(element.Id), 'category': category_name,
                              'bounds': (low, high)})
        except Exception as exc:
            bbox_errors[unicode(exc)] += 1
    result['totalCount'] = total
    result['countsByCategory'] = dict(counts)
    result['boundingBoxCount'] = len(boxes)
    result['boundingBoxErrors'] = dict(bbox_errors)
    union = None
    result['unionBoundingBoxMm'] = None
    result['medianCentreMm'] = None
    result['outliers'] = []
    if boxes:
        union = ([min(item['bounds'][0][i] for item in boxes) for i in range(3)],
                 [max(item['bounds'][1][i] for item in boxes) for i in range(3)])
        centre = [median([(item['bounds'][0][i] + item['bounds'][1][i]) / 2
                          for item in boxes]) for i in range(3)]
        result['unionBoundingBoxMm'] = bbox_mm(union)
        result['medianCentreMm'] = [round(value * 304.8, 3) for value in centre]
        for item in boxes:
            item['distance'] = max(math.sqrt(sum((point[i] - centre[i]) ** 2
                                                 for i in range(3)))
                                   for point in item['bounds'])
        result['outliers'] = [
            {'id': item['id'], 'category': item['category'],
             'bboxMm': bbox_mm(item['bounds']),
             'distanceFromMedianCentreMm': round(item['distance'] * 304.8, 3)}
            for item in sorted(boxes, key=lambda item: (-item['distance'], item['id']))[:10]]
    read_field(result, 'cropBoxActive', lambda: bool(view.CropBoxActive))
    read_field(result, 'cropBoxMm', lambda: bbox_mm(
        (coordinates(view.CropBox.Min), coordinates(view.CropBox.Max))))
    read_field(result, 'cropBoxTransform', lambda: {
        'originMm': [value * 304.8 for value in coordinates(view.CropBox.Transform.Origin)],
        'basisX': coordinates(view.CropBox.Transform.BasisX),
        'basisY': coordinates(view.CropBox.Transform.BasisY),
        'basisZ': coordinates(view.CropBox.Transform.BasisZ)})
    read_field(result, 'scale', lambda: view.Scale)
    read_field(result, 'displayStyle', lambda: unicode(view.DisplayStyle))
    read_field(result, 'detailLevel', lambda: unicode(view.DetailLevel))
    read_field(result, 'viewTemplateId', lambda: eid(view.ViewTemplateId))
    read_field(result, 'phase', lambda: parameter_string(view, DB.BuiltInParameter.VIEW_PHASE))
    read_field(result, 'phaseFilter', lambda: parameter_string(view, DB.BuiltInParameter.VIEW_PHASE_FILTER))
    read_field(result, 'discipline', lambda: unicode(view.Discipline))
    read_field(result, 'temporaryHideIsolateActive', lambda: bool(view.IsTemporaryHideIsolateActive()))
    hidden = {'count': 0, 'checkedCount': 0, 'cap': 50000, 'capped': False, 'errors': {}}
    result['hiddenElements'] = hidden
    hidden_errors = Counter()
    if category_ids:
        ids = List[DB.ElementId]()
        for identifier in sorted(category_ids):
            ids.Add(DB.ElementId(identifier))
        collector = DB.FilteredElementCollector(doc).WhereElementIsNotElementType().WherePasses(
            DB.ElementMulticategoryFilter(ids))
        for element in collector:
            if hidden['checkedCount'] >= hidden['cap']:
                hidden['capped'] = True
                break
            hidden['checkedCount'] += 1
            try:
                if element.IsHidden(view):
                    hidden['count'] += 1
            except Exception as exc:
                hidden_errors[unicode(exc)] += 1
    hidden['errors'] = dict(hidden_errors)
    return union


def export_view(doc, view, out_dir, tag, mode):
    stem = safe_name(DB.Element.Name.GetValue(view)) + '__' + safe_name(tag) + '__' + mode
    target = os.path.join(out_dir, stem + '.png')
    prefix = stem + '__' + uuid.uuid4().hex
    options = DB.ImageExportOptions()
    try:
        options.ExportRange = DB.ExportRange.SetOfViews
        ids = List[DB.ElementId]()
        ids.Add(view.Id)
        options.SetViewsAndSheets(ids)
        options.ZoomType = DB.ZoomFitType.FitToPage
        options.PixelSize = 2400
        options.ImageResolution = DB.ImageResolution.DPI_150
        options.HLRandWFViewsFileType = DB.ImageFileType.PNG
        options.ShadowViewsFileType = DB.ImageFileType.PNG
        options.FitDirection = DB.FitDirectionType.Horizontal
        options.FilePath = os.path.join(out_dir, prefix)
        doc.ExportImage(options)
    finally:
        options.Dispose()
    candidates = [os.path.join(out_dir, filename) for filename in os.listdir(out_dir)
                  if filename.startswith(prefix) and filename.lower().endswith('.png')]
    if len(candidates) != 1:
        raise RuntimeError('Expected one newly exported image, found {0}'.format(len(candidates)))
    if os.path.exists(target):
        os.remove(target)
    os.rename(candidates[0], target)
    return os.path.basename(target)


def export_crop(doc, view, union, out_dir, tag):
    transaction = DB.Transaction(doc, 'Temporary view probe crop')
    try:
        transaction.Start()
        crop = view.CropBox
        inverse = crop.Transform.Inverse
        low, high = bounds([inverse.OfPoint(point) for point in corners(*union)])
        for axis in range(2):
            margin = (high[axis] - low[axis]) * 0.05
            low[axis] -= margin
            high[axis] += margin
        # Plan crop depth is not the view range; retain its original limits.
        low[2], high[2] = crop.Min.Z, crop.Max.Z
        crop.Min = DB.XYZ(*low)
        crop.Max = DB.XYZ(*high)
        view.CropBoxActive = True
        view.CropBox = crop
        doc.Regenerate()
        return export_view(doc, view, out_dir, tag, 'crop')
    finally:
        try:
            if transaction.GetStatus() == DB.TransactionStatus.Started:
                transaction.RollBack()
        finally:
            transaction.Dispose()


def main():
    out_dir = os.environ['RVT_VIEWPROBE_OUT']
    requested = os.environ['RVT_VIEWPROBE_VIEWS'].split(';')
    if not any(requested):
        raise RuntimeError('RVT_VIEWPROBE_VIEWS must contain exact view names')
    tag = os.environ.get('RVT_VIEWPROBE_TAG', '')
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    doc = get_document()
    owned = bool(globals().get('__models__') or os.environ.get('RVT_DUMP_MODEL'))
    try:
        report = {'title': unicode(doc.Title), 'tag': tag, 'views': []}
        views = dict((unicode(DB.Element.Name.GetValue(view)), view)
                     for view in DB.FilteredElementCollector(doc).OfClass(DB.View)
                     if not view.IsTemplate)
        for name in requested:
            result = {'name': name, 'errors': [], 'exports': {}}
            report['views'].append(result)
            try:
                if name not in views:
                    raise RuntimeError('Non-template view not found: ' + name)
                view = views[name]
                result['id'] = eid(view.Id)
                result['viewType'] = unicode(view.ViewType)
                union = probe_view(doc, view, result)
                try:
                    result['exports']['fit'] = export_view(doc, view, out_dir, tag, 'fit')
                except Exception as exc:
                    result['errors'].append('fit export: ' + unicode(exc))
                if not isinstance(view, DB.ViewPlan):
                    result['exports']['cropSkipped'] = 'Forced crop applies to plan views only'
                elif union is None:
                    result['exports']['cropSkipped'] = 'No element bounding boxes available'
                else:
                    try:
                        result['exports']['crop'] = export_crop(doc, view, union, out_dir, tag)
                    except Exception as exc:
                        result['errors'].append('crop export: ' + unicode(exc))
            except Exception as exc:
                result['errors'].append(unicode(exc))
            print('VIEW {0}: {1}'.format(name, json.dumps(result, ensure_ascii=True)))
        filename = safe_name(doc.Title) + '__viewprobe__' + safe_name(tag) + '.json'
        with codecs.open(os.path.join(out_dir, filename), 'w', 'utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        print('REPORT ' + filename)
    finally:
        if owned:
            doc.Close(False)


main()
