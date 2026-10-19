"""A tokenizer and a randomly initialised one-layer GPT-2 built from the fixture rows: the
offline stand-in for ``sshleifer/tiny-gpt2`` (needs the ``[train]`` extra)."""
from __future__ import annotations

import json
from pathlib import Path

ROWS = Path(__file__).parent / "fixtures" / "tiny.jsonl"


def build_tiny_model(dest: Path) -> str:
    """A tokenizer + randomly initialised GPT-2 small enough to train in well under a second."""
    from tokenizers import Tokenizer, models, pre_tokenizers
    from transformers import GPT2Config, GPT2LMHeadModel, PreTrainedTokenizerFast

    words = sorted({w for line in ROWS.read_text().splitlines() for w in json.loads(line)["text"].split()})
    vocab = {"<unk>": 0, "<eos>": 1, **{w: i + 2 for i, w in enumerate(words)}}
    tok = Tokenizer(models.WordLevel(vocab=vocab, unk_token="<unk>"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    fast = PreTrainedTokenizerFast(tokenizer_object=tok, unk_token="<unk>", eos_token="<eos>", pad_token="<eos>")
    fast.save_pretrained(dest)
    config = GPT2Config(n_layer=1, n_head=1, n_embd=8, n_positions=64, vocab_size=len(vocab),
                        bos_token_id=1, eos_token_id=1)
    GPT2LMHeadModel(config).save_pretrained(dest)
    return str(dest)
