"""Regression tests for LLMLingua input-window protection."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_compress_text_skips_inputs_over_llmlingua_window(monkeypatch):
    from proxy import compression

    class FakeTokenizer:
        def __call__(self, text, **kwargs):
            return {"input_ids": list(range(513))}

    class FakeCompressor:
        tokenizer = FakeTokenizer()
        called = False

        def compress_prompt(self, *args, **kwargs):
            self.called = True
            return {"compressed_prompt": "CORRUPTED"}

    fake = FakeCompressor()
    monkeypatch.setattr(compression, "_compressor", fake)
    original = "word " * 500
    assert compression.compress_text(original) == original
    assert fake.called is False
