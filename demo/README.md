# Demo

An example run, kept so the shape of the output can be reviewed without generating
one.

- `bayside-print-signs.json` is the intake, written against schema 1.4.0.
- `report/` is what the engine produced from it: the one page PDF, the same summary as
  HTML, the full markdown record, and the Mermaid process maps.

Regenerate with:

```bash
python -m ai_process_audit report demo/bayside-print-signs.json --out demo/report --date 2026-08-08
```

## What this is not

**Bayside Print and Signs does not exist.** It is invented, and every figure in it was
made up to be plausible.

**It is not part of the gold set and must never be labelled.** The gold intakes live in
`eval/intakes`, were written by a different model, and are the only thing agreement is
measured against. This one exists to answer "does the report read properly", which is a
different question from "does the engine judge well".

**The scores in `report/` came from the stub judge**, which applies fixed thresholds
rather than making a judgement. They demonstrate that the pipeline runs end to end.
They demonstrate nothing about whether the ranking is any good.

The date is pinned in the command above so that regenerating produces the same file
rather than a diff every time somebody runs it.
