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
                   for c in unicode(value)).rstrip(' .') or 'view'


def view_name(view):
    return unicode(DB.Element.Name.GetValue(view))


def select_views(doc):
    views = [v for v in DB.FilteredElementCollector(doc).OfClass(DB.View) if not v.IsTemplate]
    requested = os.environ.get('RVT_EXPORT_VIEWS', '')
    if requested.strip():
        by_name = dict((view_name(v), v) for v in views)
        result = []
        for name in requested.split(';'):
            if name not in by_name:
                print('ERROR view not found: ' + name)
            elif by_name[name] not in result:
                result.append(by_name[name])
        return result
    result = []
    three = [v for v in views if v.ViewType == DB.ViewType.ThreeD]
    if three:
        result.append(next((v for v in three if view_name(v) == '{3D}'), three[0]))
    plans = [v for v in views if v.ViewType == DB.ViewType.FloorPlan]
    if doc.IsFamilyDocument:
        candidates = [v for v in views if v.ViewType in (DB.ViewType.FloorPlan, DB.ViewType.Elevation)]
        if candidates:
            result.append(next((v for v in candidates if view_name(v) == 'Front'), candidates[0]))
    else:
        if plans:
            result.append(next((v for v in plans if 'Site' in view_name(v)), plans[0]))
        sheets = [v for v in views if isinstance(v, DB.ViewSheet)]
        if sheets:
            result.append(sheets[0])
    return result


def export_view(doc, view, out_dir, tag):
    stem = safe_name(view_name(view)) + '__' + safe_name(tag)
    target = os.path.join(out_dir, stem + '.png')
    options = DB.ImageExportOptions()
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
    options.FilePath = os.path.join(out_dir, stem)
    before = dict((n, (os.stat(os.path.join(out_dir, n)).st_mtime,
                       os.stat(os.path.join(out_dir, n)).st_size)) for n in os.listdir(out_dir))
    transaction = None
    try:
        if view.ViewType == DB.ViewType.ThreeD:
            transaction = DB.Transaction(doc, 'Temporary shaded image export')
            transaction.Start()
            view.DisplayStyle = DB.DisplayStyle.Shading
            doc.Regenerate()
        doc.ExportImage(options)
    finally:
        if transaction is not None:
            try:
                if transaction.GetStatus() == DB.TransactionStatus.Started:
                    transaction.RollBack()
            finally:
                transaction.Dispose()
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
    out_dir = os.environ['RVT_EXPORT_OUT']
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    doc = get_document()
    owned = bool(globals().get('__models__') or os.environ.get('RVT_DUMP_MODEL'))
    exported = {}
    try:
        tag = os.environ.get('RVT_EXPORT_TAG', '')
        for view in select_views(doc):
            name = view_name(view)
            try:
                exported[name] = export_view(doc, view, out_dir, tag)
                print('EXPORTED {0}: {1}'.format(name, exported[name]))
            except Exception as exc:
                print('ERROR {0}: {1}'.format(name, exc))
        with codecs.open(os.path.join(out_dir, 'views_exported.json'), 'w', 'utf-8') as stream:
            json.dump(exported, stream, ensure_ascii=False, indent=2)
    finally:
        if owned:
            doc.Close(False)


main()
