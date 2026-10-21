# Results index

I keep the one-way conversion and round-trip comparisons in separate folders.
The [main report](../README.md) explains the method, controls and interpretation.

| Folder | Test case | Comparisons |
|---|---|---|
| [00-control-native-2023-opened-in-2027](00-control-native-2023-opened-in-2027/) | Native Autodesk 2023 project and family opened in 2027 | Revit version noise |
| [01-family-basic](01-family-basic/) | Simple furniture family | `downgrade`, `roundtrip` |
| [02-family-lookup-table](02-family-lookup-table/) | Plumbing family with lookup table | `downgrade`, `roundtrip` |
| [03-family-nested-formulas](03-family-nested-formulas/) | Window family with formulas and nested families | `downgrade`, `roundtrip` |
| [04-project-template](04-project-template/) | DefaultMetric template | `downgrade`, `roundtrip` |
| [05-site-toposolid](05-site-toposolid/) | Snowdon Towers Sample Site | `downgrade`, `roundtrip` |
| [06-project-residence](06-project-residence/) | Pacific Continental Residence Sample | `downgrade`, `roundtrip` |

`downgrade` compares the 2027 original with the converted file opened in 2023.
`roundtrip` compares the 2027 original with the converted file reopened in 2027.
The control folder separates `rac_basic_sample_project` and `rac_basic_sample_family`.

Each comparison has `summary.md`, `by_category.md` and `report.md`.
CSV files contain element and parameter details; `summary.json` contains comparison counts.
I interpret missing parameters against the native-version control before treating them as conversion losses.
The [compressed dumps](../data/dumps/) and [probe output](../data/probes/) provide the underlying derived data.
