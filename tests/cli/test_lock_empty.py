"""`cairn lock` on a tree that ships no packs says what it does not pin (#2536).

The public tree ships its packs as wheels and the example pack through CAIRN_PACK_PATH,
so the first `cairn lock` printed "0 packs pinned" and nothing else."""
from __future__ import annotations

from cairn.cli.commands import lock


def test_an_empty_closure_explains_itself(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(lock, "_cairn_root", lambda: tmp_path / "no-packs")
    monkeypatch.setattr(lock, "_installed_pack_wheels", lambda: 13)
    monkeypatch.chdir(tmp_path)
    lock.cmd(check=False)
    out = capsys.readouterr().out
    assert "wrote cairn.lock — 0 packs pinned" in out
    assert "this source tree ships no packs" in out
    assert "dev packs on CAIRN_PACK_PATH are not pinned" in out
    assert "installed pack wheels (13)" in out
    lock.cmd(check=True)  # an empty lock is still a consistent lock


def test_a_shipped_closure_prints_no_note(tmp_path, monkeypatch, capsys) -> None:
    pack = tmp_path / "src" / "packs" / "demo"
    pack.mkdir(parents=True)
    (pack / "pack.toml").write_text('[pack]\nname = "demo"\nversion = "0.1.0"\n')
    monkeypatch.setattr(lock, "_cairn_root", lambda: tmp_path / "src")
    monkeypatch.chdir(tmp_path)
    lock.cmd(check=False)
    out = capsys.readouterr().out
    assert "1 packs pinned" in out and "ships no packs" not in out
