# Model dumps and reports

`dump_model.py` runs inside Revit 2023 or 2027 with pyRevit CLI 6.5 and IronPython 2.7.
Project models, project templates, families, and family templates use the same script.

`run_dump.ps1` opens the model through `RVT_DUMP_MODEL`, without pyRevit CLI model-version detection.
```powershell
.\scripts\run_dump.ps1 -Model data\original.rvt -Year 2027
.\scripts\run_dump.ps1 -Model data\converted.rvt -Year 2023 -OutDir out\dumps -NoGeometry
```
`-Script` selects another dump script; the default is `dump_model.py` beside the wrapper.
The wrapper prints CLI output, elapsed seconds, the last 80 runner log lines when available, and output file names and sizes.

Without `RVT_DUMP_OUT`, dumps use `%LOCALAPPDATA%\revit-downgrade-test\dumps`.
Each run writes `<title>__<version>.json` and a matching `.log`, replacing existing files with those names.
The log includes timings, progress every 5000 elements, and errors also stored in `meta.errors`.
`RVT_DUMP_GEOMETRY=0` disables instance and family type geometry collection.
Family type inspection rolls back its transaction and does not save the document.
Lengths use millimetres, areas use square metres, volumes use cubic metres, and angles use degrees.
Formatted parameter strings retain the document display units.
Dumps contain document paths and model metadata; publication requires review of those generated files.

`diff_dumps.py` runs on Python 3.12 with the standard library.

```sh
python3 scripts/diff_dumps.py out/dumps/original__2027.json out/dumps/converted__2023.json --out out/report --tol-mm 1.0 --tol-pct 0.1 --label-before 2027 --label-after 2023
```

`report.md` combines the summary, category counts, parameter losses, documentation highlights, warnings, and optional family comparison.
Separate Markdown and CSV files contain detailed changes; `summary.json` contains machine-readable counts.
Matching uses unique ids, then one-to-one nearest locations within the same category, family, and type.
Numeric differences must exceed the percentage tolerance; identified length parameters also use the millimetre tolerance.
Schema 1 identifies length parameters by a displayed `mm` unit or a length-related English name.
Warning totals count occurrences; new and gone counts count distinct descriptions.
The CLI always returns exit code 0; input and I/O failures print diagnostics to stderr.

```sh
python3 -m py_compile scripts/diff_dumps.py
python3 -m pytest tests -q -p no:cacheprovider
```

## Family extraction
`extract_families.py` scans editable, non-in-place project families and exports a deduplicated selection of up to six families.
The selection combines complexity scores with lookup tables, formula-driven connectors, door/window formulas, and nested shared families.
```powershell
.\scripts\run_extract.ps1 -Model data\original.rvt -Year 2027 -OutDir out\families
```
The wrapper sets `RVT_DUMP_MODEL` and `RVT_EXTRACT_OUT` and prints CLI output, elapsed seconds, runner log tail, and output files.
`families_index.json` records all scanned family metrics, per-family errors, selection reasons, and saved file names.
Selected `.rfa` files overwrite matching output names; family documents close without further saving, and the project is not saved.

## View images
```powershell
.\scripts\run_export.ps1 -Model data\original.rvt -Year 2027 -OutDir out\images -Tag 2027
.\scripts\run_export.ps1 -Model data\converted.rvt -Year 2023 -OutDir out\images -Tag 2023 -Views '{3D};Site;A101'
```
`export_views.py` reads `RVT_DUMP_MODEL`, `RVT_EXPORT_OUT`, `RVT_EXPORT_TAG`, and optional semicolon-separated exact names in `RVT_EXPORT_VIEWS`.
Without names, projects select a 3D view (preferring `{3D}`), a floor plan (preferring a name containing `Site`), and a sheet.
Families select a 3D view and a floor plan or elevation (preferring `Front`).
Exports are 2400-pixel, 150-DPI PNGs named `<safe view name>__<tag>.png`.
`views_exported.json` maps successful view names to file names; reuse its keys as `-Views` when comparing versions.
3D shading changes are rolled back, the source is not saved, and per-view failures appear in runner output.

## Focus images
```powershell
.\scripts\run_focus.ps1 -Model data\original.rvt -Year 2027 -OutDir out\focus -Tag 2027 -Uids 'focus-unique-id,another-unique-id' -ContextUids 'context-unique-id' -PadMm 15000
```
`export_focus.py` reads `RVT_DUMP_MODEL`, `RVT_FOCUS_OUT`, `RVT_FOCUS_TAG`, and comma-separated element UniqueIds in `RVT_FOCUS_UIDS`.
Optional `RVT_FOCUS_CONTEXT_UIDS` identifies context elements; `RVT_FOCUS_PAD_MM` defaults to 15000 and `RVT_FOCUS_PIXELS` defaults to 2400.
The wrapper accepts `-ContextUids` and `-PadMm`; set `RVT_FOCUS_PIXELS` before running it to change image size.
A temporary shaded, fine-detail 3D view clips to the focus bounding boxes, padded by the requested distance in X/Y and one third of it in Z.
Focus elements are solid red; context elements are blue-grey with 30 percent transparency, with focus styling taking precedence for overlapping selections. Other elements are not halftoned.
Exports are 150-DPI PNGs named `iso__<tag>.png` and `side__<tag>.png`; the side camera looks north with vertical up.
`focus__<tag>.json` records focus UniqueIds, category, family, type, point locations and bounding boxes in millimetres, image names, and step errors.
Tags are sanitized for file names. Existing matching outputs are replaced. Errors are printed and recorded without preventing independent steps.
View creation and overrides use one transaction, image exports run after regeneration inside it, and the transaction is rolled back without saving the model.

## Edit and persistence probes
```powershell
.\scripts\run_probe.ps1 -Model data\converted.rvt -Year 2023 -OutDir out\probes
.\scripts\run_probe.ps1 -Model data\family.rfa -Year 2023 -OutDir out\probes
```
`edit_probe.py` reads `RVT_DUMP_MODEL` and `RVT_PROBE_OUT`.
Projects commit one transaction that moves a wall by 100 mm, sets timestamped instance comments on a wall, a toposolid when available, and a family instance, creates a 3000 mm wall, and copies a family instance by 5000 mm.
The JSON records per-step success, element ids, wall endpoint coordinates in millimetres, warnings, and persistence checks after saving and reopening `<title>__probe.rvt`.
Families record every type's formatted and raw parameter values, solid volume in cubic metres, lookup table dimensions, and the effects of increasing the first eligible length parameter by 10 percent.
Family inspection and parameter changes are rolled back before saving `<title>__probe.rfa`; reopening confirms the saved family can be opened.
Results and individual errors go to `<title>__probe.json`. Probe copies overwrite matching output names; source files are not saved.
Both wrappers print CLI output, elapsed seconds, the runner log tail when available, and output file names and sizes.
