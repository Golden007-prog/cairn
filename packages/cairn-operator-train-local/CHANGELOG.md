# Changelog

## 0.1.0

- Initial release (ADR-0127 D4, #2572 WP3): a builtin pack (fourteen with secrets-infisical, D3).
- `train/lora` implements `contract://train.finetune/1.0.0` (`cairn-contracts-train` 1.0.0);
  the same fine-tune ships as the ml flow stage `train.lora`.
- `train/eval-loss` scores a saved adapter; implements `contract://train.eval_loss/1.0.0`.
- The training stack (torch, transformers, peft, datasets) is the `[train]` extra; mock paths
  for both operations and the stage need none of it.
