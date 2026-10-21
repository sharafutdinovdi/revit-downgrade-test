from __future__ import print_function, division

import codecs
import json
import os

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
                   for c in unicode(value)).rstrip(' .') or 'focus'


def error(report, step, exc):
    report['errors'].append({'step': step, 'error': unicode(exc)})
    print('ERROR {0}: {1}'.format(step, exc))


def point_mm(point):
    return [point.X * 304.8, point.Y * 304.8, point.Z * 304.8]


def bounds(element):
    box = element.get_BoundingBox(None)
    if box is None:
        raise RuntimeError('Element has no bounding box')
    corners = [box.Transform.OfPoint(DB.XYZ(x, y, z))
               for x in (box.Min.X, box.Max.X)
               for y in (box.Min.Y, box.Max.Y)
               for z in (box.Min.Z, box.Max.Z)]
    return (DB.XYZ(min(p.X for p in corners), min(p.Y for p in corners),
                   min(p.Z for p in corners)),
            DB.XYZ(max(p.X for p in corners), max(p.Y for p in corners),
                   max(p.Z for p in corners)))


def read_elements(doc, variable, report, collect_metadata):
    elements = []
    boxes = []
    for uid in os.environ.get(variable, '').split(','):
        uid = uid.strip()
        if not uid:
            continue
        row = {'unique_id': uid, 'category': None, 'family': None,
               'type': None, 'location_mm': None, 'bbox_mm': None}
        if collect_metadata:
            report['elements'].append(row)
        try:
            element = doc.GetElement(uid)
            if element is None:
                raise RuntimeError('UniqueId not found: ' + uid)
            elements.append(element)
        except Exception as exc:
            error(report, variable + ':' + uid, exc)
            continue
        if not collect_metadata:
            continue
        try:
            low, high = bounds(element)
            boxes.append((low, high))
            row['bbox_mm'] = {'min': point_mm(low), 'max': point_mm(high)}
        except Exception as exc:
            error(report, 'bbox:' + uid, exc)
        try:
            row['category'] = unicode(element.Category.Name) if element.Category else None
            element_type = doc.GetElement(element.GetTypeId())
            if element_type is not None:
                row['type'] = unicode(DB.Element.Name.GetValue(element_type))
                row['family'] = unicode(element_type.FamilyName)
        except Exception as exc:
            error(report, 'identity:' + uid, exc)
        try:
            location = element.Location
            if isinstance(location, DB.LocationPoint):
                row['location_mm'] = point_mm(location.Point)
        except Exception as exc:
            error(report, 'location:' + uid, exc)
    return elements, boxes


def export_image(doc, view, out_dir, stem, pixels):
    target = os.path.join(out_dir, stem + '.png')
    options = DB.ImageExportOptions()
    try:
        options.ExportRange = DB.ExportRange.SetOfViews
        ids = List[DB.ElementId]()
        ids.Add(view.Id)
        options.SetViewsAndSheets(ids)
        options.ZoomType = DB.ZoomFitType.FitToPage
        options.PixelSize = pixels
        options.ImageResolution = DB.ImageResolution.DPI_150
        options.HLRandWFViewsFileType = DB.ImageFileType.PNG
        options.ShadowViewsFileType = DB.ImageFileType.PNG
        options.FitDirection = DB.FitDirectionType.Horizontal
        options.FilePath = os.path.join(out_dir, stem)
        before = dict((n, (os.stat(os.path.join(out_dir, n)).st_mtime,
                           os.stat(os.path.join(out_dir, n)).st_size))
                      for n in os.listdir(out_dir))
        doc.Regenerate()
        doc.ExportImage(options)
    finally:
        options.Dispose()
    candidates = []
    for filename in os.listdir(out_dir):
        if filename.startswith(stem) and filename.lower().endswith('.png'):
            path = os.path.join(out_dir, filename)
            stamp = (os.stat(path).st_mtime, os.stat(path).st_size)
            if filename not in before or before[filename] != stamp:
                candidates.append(path)
    if len(candidates) != 1:
        raise RuntimeError('Expected one newly exported image, found {0}'.format(len(candidates)))
    if candidates[0] != target:
        if os.path.exists(target):
            os.remove(target)
        os.rename(candidates[0], target)
    return os.path.basename(target)


def main():
    report = {'elements': [], 'images': {}, 'errors': []}
    doc = None
    transaction = None
    out_dir = os.environ.get('RVT_FOCUS_OUT')
    tag = safe_name(os.environ.get('RVT_FOCUS_TAG', 'focus'))
    owned = bool(globals().get('__models__') or os.environ.get('RVT_DUMP_MODEL'))
    try:
        if not out_dir:
            raise RuntimeError('RVT_FOCUS_OUT is required')
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        pad = 15000.0 / 304.8
        pixels = 2400
        try:
            pad = float(os.environ.get('RVT_FOCUS_PAD_MM', '15000')) / 304.8
        except Exception as exc:
            error(report, 'pad', exc)
        try:
            pixels = int(os.environ.get('RVT_FOCUS_PIXELS', '2400'))
        except Exception as exc:
            error(report, 'pixels', exc)
        doc = get_document()
        focus, boxes = read_elements(doc, 'RVT_FOCUS_UIDS', report, True)
        context, unused = read_elements(doc, 'RVT_FOCUS_CONTEXT_UIDS', report, False)
        transaction = DB.Transaction(doc, 'Temporary focus images')
        transaction.Start()
        view = None
        center = None
        distance = None
        try:
            view_type = next(v for v in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType)
                             if v.ViewFamily == DB.ViewFamily.ThreeDimensional)
            view = DB.View3D.CreateIsometric(doc, view_type.Id)
        except Exception as exc:
            error(report, 'create_view', exc)
        if view is not None:
            for step, configure in (
                    ('template', lambda: setattr(view, 'ViewTemplateId', DB.ElementId.InvalidElementId)),
                    ('name', lambda: setattr(view, 'Name', 'Focus ' + tag)),
                    ('shading', lambda: setattr(view, 'DisplayStyle', DB.DisplayStyle.Shading)),
                    ('detail', lambda: setattr(view, 'DetailLevel', DB.ViewDetailLevel.Fine))):
                try:
                    configure()
                except Exception as exc:
                    error(report, step, exc)
            try:
                if not boxes:
                    raise RuntimeError('No focus bounding boxes available')
                low = DB.XYZ(min(b[0].X for b in boxes), min(b[0].Y for b in boxes),
                             min(b[0].Z for b in boxes))
                high = DB.XYZ(max(b[1].X for b in boxes), max(b[1].Y for b in boxes),
                              max(b[1].Z for b in boxes))
                center = (low + high) * 0.5
                box = DB.BoundingBoxXYZ()
                padding = DB.XYZ(pad, pad, pad / 3.0)
                box.Min = low - padding
                box.Max = high + padding
                view.SetSectionBox(box)
                view.IsSectionBoxActive = True
                distance = max((box.Max - box.Min).GetLength(), 1.0) * 10.0
            except Exception as exc:
                error(report, 'section_box', exc)
            solid = None
            try:
                solid = next(p for p in DB.FilteredElementCollector(doc).OfClass(DB.FillPatternElement)
                             if p.GetFillPattern().IsSolidFill)
            except Exception as exc:
                error(report, 'solid_fill', exc)
            focus_uids = set(e.UniqueId for e in focus)
            for elements, color, transparency in ((context, DB.Color(110, 135, 155), 30),
                                                   (focus, DB.Color(230, 40, 40), 0)):
                for element in elements:
                    if transparency and element.UniqueId in focus_uids:
                        continue
                    try:
                        settings = DB.OverrideGraphicSettings()
                        settings.SetProjectionLineColor(color)
                        settings.SetSurfaceForegroundPatternColor(color)
                        if solid is not None:
                            settings.SetSurfaceForegroundPatternId(solid.Id)
                            settings.SetSurfaceForegroundPatternVisible(True)
                        settings.SetSurfaceTransparency(transparency)
                        view.SetElementOverrides(element.Id, settings)
                    except Exception as exc:
                        error(report, 'override:' + element.UniqueId, exc)
            for kind in ('iso', 'side'):
                try:
                    if kind == 'side':
                        if center is None or distance is None:
                            raise RuntimeError('Side orientation requires focus bounds')
                        forward = DB.XYZ(0, 1, 0)
                        eye = center - forward * distance
                        view.SetOrientation(DB.ViewOrientation3D(eye, DB.XYZ(0, 0, 1), forward))
                    report['images'][kind] = export_image(doc, view, out_dir, kind + '__' + tag, pixels)
                    print('EXPORTED {0}: {1}'.format(kind, report['images'][kind]))
                except Exception as exc:
                    error(report, 'export_' + kind, exc)
    except Exception as exc:
        error(report, 'setup', exc)
    finally:
        try:
            if out_dir:
                with codecs.open(os.path.join(out_dir, 'focus__' + tag + '.json'), 'w', 'utf-8') as stream:
                    json.dump(report, stream, ensure_ascii=False, indent=2)
        except Exception as exc:
            error(report, 'write_json', exc)
        if transaction is not None:
            try:
                if transaction.GetStatus() == DB.TransactionStatus.Started:
                    transaction.RollBack()
            except Exception as exc:
                error(report, 'rollback', exc)
            finally:
                try:
                    transaction.Dispose()
                except Exception as exc:
                    error(report, 'dispose_transaction', exc)
        if doc is not None and owned:
            try:
                doc.Close(False)
            except Exception as exc:
                error(report, 'close_document', exc)


main()
