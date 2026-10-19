"""The e2e-fullstack pipeline, authored with the SDK (ADR-0087 parity twin
of ``cairn/packs/training/pipelines/e2e-fullstack.yaml``).

Same shared kernels, same DuckDB dedup opt-in, same gate structure, same
engine-neutrality — compiled into its OWN pack (D5 self-sufficiency: the
stage lists are re-declared here, the code is not). Compile + inspect:

    .venv/bin/python examples/e2e_fullstack_sdk.py            # compile, print records
    .venv/bin/python examples/e2e_fullstack_sdk.py --write DIR  # emit the pack

Run it like any pack (the drift pin is the compiled ``content_hash``):

    from cairn_sdk import run_pipeline
    run_pipeline(E2eFullstack, inputs={"dataset": ..., "eval_probe": ...})
"""
from __future__ import annotations

from cairn_sdk import compile_pipeline, flow, gate, inp, pipeline

REPORT_PROMPT = """\
You are writing the closing report for a governed pipeline that just
completed: the data flow validated and FIXED the corpus (exact-dedup on
DuckDB, PII redaction, decontamination), a data gate confirmed the cleaned
set, a capped QLoRA leg trained on it, and a human approved promotion.
Narrate ONLY the wired-in inputs (row accounting, fix deltas, losses,
adapter location) — quote the real numbers, never invent one, and leave
anything you cannot ground empty.
"""


@pipeline(series="e2e-fullstack", pack="e2e-fullstack-sdk")
class E2eFullstack:
    class Inputs:
        dataset: str
        eval_probe: str = ""
        base_model: str = "Qwen/Qwen2.5-0.5B-Instruct"
        max_steps: int = 24

    # DATA leg — same kernels + DuckDB dedup as flows/data-prep.yaml
    prep = (flow.data("data-prep", raw=inp.dataset, eval_path=inp.eval_probe)
            .stage("ingest.normalize", path=inp.dataset)
            .stage("dedup.exact", backend="duckdb")
            .stage("decontam.apply", eval_path=inp.eval_probe)
            .stage("pii.redact")
            .stage("scan.all")
            .stage("split", seed=13, train_ratio=0.9, val_ratio=0.1)
            .stage("export.jsonl", name="train-ready.jsonl", checkpoint=True))

    # DATA gate — answered by the post-fix scan, instantly (#1275); the
    # guarantee the fixes make is ZERO SECRETS (pii_rows stays nonzero on
    # real prose — NER flags names/dates forever)
    clean = gate(prep.out.scan_all_secret_rows == 0)

    # ML leg — same three-kernel finetune as flows/sft-train.yaml
    train = (flow.ml("finetune",
                     dataset_uri=prep.out.export,
                     base_model=inp.base_model)
             .stage("train.qlora",
                    dataset_uri=prep.out.export, base_model=inp.base_model,
                    output_name="e2e-fullstack-sdk-candidate",
                    max_steps=inp.max_steps,
                    report_to=["mlflow"], tracking_project="e2e-fullstack")
             .stage("eval.checkpoints")
             .stage("package.adapter", base_model=inp.base_model))

    # HITL gate — the human sees the REAL numbers
    promote = gate(approvers="ml-leads",
                   show=[prep.out.dedup_exact_dropped,
                         prep.out.pii_redact_redacted_rows,
                         train.out.train_qlora_train_loss,
                         train.out.train_qlora_eval_loss])

    # AGENTIC leg — one gateway LLM call over the wired-in facts
    report = flow.agentic(
        "e2e-report", model="gpt-4o-mini", prompt=REPORT_PROMPT,
        dataset_path=inp.dataset, base_model=inp.base_model,
        rows_ingested=prep.out.ingest_normalize_rows,
        dedup_backend=prep.out.dedup_exact_backend,
        dupes_dropped=prep.out.dedup_exact_dropped,
        pii_redacted_rows=prep.out.pii_redact_redacted_rows,
        contaminated_dropped=prep.out.decontam_apply_dropped,
        rows_exported=prep.out.scan_all_scanned,
        train_loss=train.out.train_qlora_train_loss,
        eval_loss=train.out.train_qlora_eval_loss,
        adapter_uri=train.out.train_qlora_adapter_uri,
    )

    graph = prep >> clean >> train >> promote >> report


if __name__ == "__main__":
    import sys

    compiled = compile_pipeline(E2eFullstack)
    print("pack:", compiled.pack_name, "| pipeline:", compiled.name)
    print("content_hash:", compiled.content_hash)
    for note in compiled.notes:
        print("note:", note)
    if len(sys.argv) > 2 and sys.argv[1] == "--write":
        compiled.write(sys.argv[2])
        print("wrote pack →", sys.argv[2])
