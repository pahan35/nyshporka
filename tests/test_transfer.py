"""🧳 `nysh workspace export` / `import`: простір їде на іншу машину без тихих втрат.

🔴 Обіцянки:

- пакунок повний лише з описом, який пишеться ОСТАННІМ; без нього імпорт
  відмовляє, а не розкладає половину;
- venv рушіїв і замок не їдуть; посилання не розкриваються;
- корені справ поза простором або їдуть і переписуються на нове місце, або
  називаються в переліку «лишилось руками» — але ніколи не зникають мовчки;
- хук git і запис запобіжника помічника, що несуть шляхи старої машини,
  виправляються під нову;
- повторний експорт докопійовує лише змінене й прибирає видалене.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from nyshporka.core import workspace as W
from nyshporka.setup import transfer as T

OLD_PY = "/old/machine/python"


def _quiet(*_a: object, **_k: object) -> None:
    return None


@pytest.fixture
def old(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    from nyshporka.canon import hook as CH
    from nyshporka.project import LEDGER, memory_dir
    from nyshporka.skills import hook as SH

    hub = tmp_path / "A" / "Users" / "x" / "Нишпорка"
    ext = tmp_path / "A" / "Volumes" / "архів" / "фонд"
    (hub / "data" / "raw" / "spr-1").mkdir(parents=True)
    (hub / "data" / "raw" / "spr-1" / "0001.jpg").write_bytes(b"x" * 100)
    (hub / "data" / "raw" / "порожня").mkdir()
    (hub / "config").mkdir()
    (hub / "config" / "research_profile.yaml").write_text("fallback: moje\n", encoding="utf-8")
    (ext / "spr-9").mkdir(parents=True)
    (ext / "spr-9" / "0001.jpg").write_bytes(b"e" * 40)
    (hub / W.MARKER).write_text(
        "[workspace]\nschema = 1\nname = \"Рід\"\n"
        "# корені — руками\n"
        f'case_roots = ["{(hub / "data" / "raw").as_posix()}", "{ext.as_posix()}"]\n',
        encoding="utf-8")
    # непереносне
    (hub / ".venv_htr" / "bin").mkdir(parents=True)
    (hub / ".venv_htr" / "bin" / "python").write_bytes(b"venv")
    (hub / ".venv" / "bin").mkdir(parents=True)
    (hub / ".venv" / "bin" / "python").write_bytes(b"venv")
    (hub / ".lock").write_text("{}", encoding="utf-8")
    # проєкт із керованими файлами
    proj = hub / "projects" / "rid"
    proj.mkdir(parents=True)
    (proj / W.MARKER).write_text('[project]\nname = "Рід"\n', encoding="utf-8")
    (proj / LEDGER).write_text(json.dumps({"version": "0.46.5", "files": {}}), encoding="utf-8")
    # хук git зі шляхом старої машини
    (hub / ".git" / "hooks").mkdir(parents=True)
    (hub / ".git" / "hooks" / "pre-commit").write_text(
        CH.HOOK.replace("{interpreter}", OLD_PY), encoding="utf-8")
    # запобіжник помічника поруч із власним дозволом людини
    local = hub / ".claude" / "settings.local.json"
    local.parent.mkdir(parents=True)
    local.write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {"PreToolUse": [{"matcher": SH.MATCHER, "hooks": [
            {"type": "command", "command": f'"{OLD_PY}" "/old/{SH.SCRIPT}"'}]}]},
    }), encoding="utf-8")
    # пам'ять помічника — у профілі, не в просторі
    mem = memory_dir(hub)
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- рід із Поділля\n", encoding="utf-8")
    # тека даних і кеш
    data = tmp_path / "A" / "appdata"
    (data / "catalog").mkdir(parents=True)
    (data / "catalog" / "pack.sqlite").write_bytes(b"c" * 50)
    cache = tmp_path / "A" / "cache"
    (cache / "model").mkdir(parents=True)
    (cache / "model" / "w.pt").write_bytes(b"m" * 70)
    monkeypatch.setattr("nyshporka.core.appdirs.data_dir", lambda: data)
    monkeypatch.setattr("nyshporka.core.appdirs.cache_dir", lambda: cache)
    monkeypatch.setattr(T, "fs_type", lambda _p: "apfs")

    W.reset()
    W.use(hub)
    yield {"hub": hub, "ext": ext, "data": data, "cache": cache, "proj": proj}
    W.reset()


def _export(dest: Path, **kw: bool) -> T.ExportPlan:
    p = T.plan_export(dest, **kw)
    assert not p.problems, p.problems
    T.run_export(p, echo=_quiet)
    return p


@pytest.fixture
def new_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Порожні тека даних і кеш «нової машини» та її інтерпретатор.

    Тека даних підміняється лише під час імпорту (`_import`): експорт мусить
    бачити теку старої машини.
    """
    data, cache = tmp_path / "B" / "appdata", tmp_path / "B" / "cache"
    monkeypatch.setattr(T, "_installed_python", lambda: "/new/machine/python")
    real = T.run_import

    def on_new_machine(*a: object, **k: object) -> T.ImportReport:
        with monkeypatch.context() as m:
            m.setattr("nyshporka.core.appdirs.data_dir", lambda: data)
            m.setattr("nyshporka.core.appdirs.cache_dir", lambda: cache)
            return real(*a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(T, "run_import", on_new_machine)
    return {"data": data, "cache": cache, "target": tmp_path / "B" / "Docs" / "Нишпорка"}


# ── експорт ──────────────────────────────────────────────────────────────────
def test_export_leaves_out_venvs_and_lock_and_writes_the_description_last(
        old, tmp_path: Path) -> None:
    dest = tmp_path / "drive" / "пакунок"
    _export(dest, with_roots=True, with_data=True)

    meta = json.loads((dest / T.BUNDLE).read_text(encoding="utf-8"))
    assert not (dest / T.PARTIAL).exists()
    ws = dest / T.WS_DIR
    assert (ws / "data" / "raw" / "spr-1" / "0001.jpg").read_bytes() == b"x" * 100
    assert (ws / "data" / "raw" / "порожня").is_dir(), "порожня тека справи теж їде"
    for gone in (".venv_htr", ".venv", ".lock"):
        assert not (ws / gone).exists(), gone
    assert meta["workspace"]["engines"] is True
    assert meta["data"] == ["cache", "catalog"]
    assert meta["memory"] == [T.HUB_MEMORY]
    # перелік файлів — повний і з розмірами: за ним імпорт звіряє пакунок
    assert meta["files"]["workspace/data/raw/spr-1/0001.jpg"] == 100
    assert all(not k.startswith("workspace/.venv") for k in meta["files"])
    roots = {r["path"]: r for r in meta["roots"]}
    assert roots[(old["hub"] / "data" / "raw").as_posix()]["inside"] == "data/raw"
    slot = roots[old["ext"].as_posix()]["slot"]
    assert slot == "roots/1-фонд"
    assert (dest / slot / "spr-9" / "0001.jpg").is_file()


def test_reexport_copies_only_what_changed_and_drops_what_was_deleted(
        old, tmp_path: Path) -> None:
    dest = tmp_path / "drive" / "пакунок"
    _export(dest)
    raw = old["hub"] / "data" / "raw"
    (raw / "spr-2").mkdir()
    (raw / "spr-2" / "0001.jpg").write_bytes(b"n" * 10)
    (old["hub"] / "config" / "research_profile.yaml").unlink()

    p = T.plan_export(dest)
    assert p.refresh and not p.problems
    assert p.copy_bytes == 10, "уже скопійоване вдруге не рахується"
    T.run_export(p, echo=_quiet)
    files = json.loads((dest / T.BUNDLE).read_text(encoding="utf-8"))["files"]
    assert "workspace/data/raw/spr-2/0001.jpg" in files
    assert "workspace/config/research_profile.yaml" not in files
    assert not (dest / T.WS_DIR / "config" / "research_profile.yaml").exists()


def test_export_refuses_a_foreign_non_empty_folder_and_one_inside_the_workspace(
        old, tmp_path: Path) -> None:
    busy = tmp_path / "drive" / "фото"
    busy.mkdir(parents=True)
    (busy / "img.jpg").write_bytes(b"1")
    assert any("не порожня" in x for x in T.plan_export(busy).problems)
    assert any("не може лежати" in x
               for x in T.plan_export(old["hub"] / "reports" / "x").problems)


def test_export_refuses_while_the_console_holds_the_workspace(old, tmp_path: Path,
                                                             monkeypatch) -> None:
    from nyshporka.core import lock as L

    def busy(self: L.WorkspaceLock) -> L.WorkspaceLock:
        raise L.LockBusy(L.LockInfo(pid=1, host="h", started=0.0, heartbeat=0.0, port=8765))

    monkeypatch.setattr(L.WorkspaceLock, "acquire", busy)
    p = T.plan_export(tmp_path / "drive" / "b")
    with pytest.raises(T.TransferError, match="Закрийте вікно"):
        T.run_export(p, echo=_quiet)
    assert not (tmp_path / "drive" / "b" / T.BUNDLE).exists()


def test_symlinks_are_reported_not_followed(old, tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("символьні посилання на Windows потребують прав")
    (old["hub"] / "data" / "raw" / "диск").symlink_to(old["ext"], target_is_directory=True)
    dest = tmp_path / "drive" / "b"
    p = _export(dest)
    assert "workspace/data/raw/диск" in p.links
    assert not (dest / T.WS_DIR / "data" / "raw" / "диск").exists()


def test_names_windows_cannot_store_block_an_exfat_drive(old, tmp_path: Path,
                                                         monkeypatch) -> None:
    (old["hub"] / "config" / "a:b.txt").write_text("1", encoding="utf-8")
    monkeypatch.setattr(T, "fs_type", lambda _p: "exfat")
    assert any("не ляже на цей диск" in x for x in T.plan_export(tmp_path / "d").problems)
    monkeypatch.setattr(T, "fs_type", lambda _p: "apfs")
    p = T.plan_export(tmp_path / "d")
    assert not p.problems and any("Windows" in w for w in p.warnings)


def test_files_over_4gb_block_a_fat32_drive(old, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(T, "_FAT_LIMIT", 99)
    monkeypatch.setattr(T, "fs_type", lambda _p: "msdos")
    assert any("FAT32" in x for x in T.plan_export(tmp_path / "d").problems)


@pytest.mark.parametrize(("name", "bad"), [
    ("data/raw/spr-1/0001.jpg", False), ("справа/аркуш 1.jpg", False),
    ("a:b", True), ("x/кінець.", True), ("x/кінець ", True), ("CON.txt", True),
    ("x/lpt1", True), ("console.txt", False),
])
def test_windows_names(name: str, bad: bool) -> None:
    assert T.win_bad_name(name) is bad


# ── імпорт ───────────────────────────────────────────────────────────────────
def test_round_trip_rewrites_roots_hooks_and_remembers_the_workspace(
        old, new_machine, tmp_path: Path) -> None:
    from nyshporka.project import LEDGER, memory_dir

    dest = tmp_path / "drive" / "пакунок"
    _export(dest, with_roots=True, with_data=True)
    target = new_machine["target"]
    p = T.plan_import(dest, target)
    assert not p.problems, p.problems
    rep = T.run_import(p, echo=_quiet, build_cases=False)

    assert (target / "data" / "raw" / "spr-1" / "0001.jpg").read_bytes() == b"x" * 100
    moved = target.parent / "Нишпорка-справи" / "1-фонд"
    assert (moved / "spr-9" / "0001.jpg").is_file()
    assert rep.roots_moved == {old["ext"].as_posix(): moved}
    marker = (target / W.MARKER).read_text(encoding="utf-8")
    assert "# корені — руками" in marker, "коментарі людини в маркері лишаються"
    assert old["hub"].as_posix() not in marker and old["ext"].as_posix() not in marker
    ws = W.workspace()
    assert ws.hub == target
    assert {p.as_posix() for p in ws.case_roots()} == {
        (target / "data" / "raw").as_posix(), moved.as_posix()}
    assert W._load_last_used() == target

    hook = (target / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    assert 'py="/new/machine/python"' in hook and OLD_PY not in hook
    local = json.loads((target / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert local == {"permissions": {"allow": ["Bash(ls:*)"]}}
    led = json.loads((target / "projects" / "rid" / LEDGER).read_text(encoding="utf-8"))
    assert led["version"] == "перенесено", "перший запуск nysh перепише файли проєкту"
    assert (memory_dir(target) / "MEMORY.md").read_text(encoding="utf-8").startswith("- рід")
    assert (new_machine["data"] / "catalog" / "pack.sqlite").is_file()
    assert (new_machine["cache"] / "model" / "w.pt").is_file()
    # пакунок не чіпали
    assert (dest / T.BUNDLE).is_file() and (dest / T.WS_DIR / W.MARKER).is_file()
    assert not any("roots add" in x for x in rep.todo)
    assert any("htr install" in x for x in rep.todo)
    assert not any("models get" in x for x in rep.todo), "ваги приїхали в пакунку"


def test_roots_left_behind_are_named_not_dropped(old, new_machine, tmp_path: Path) -> None:
    dest = tmp_path / "drive" / "b"
    _export(dest)
    rep = T.run_import(T.plan_import(dest, new_machine["target"]), echo=_quiet,
                       build_cases=False)
    assert rep.roots_missing == [old["ext"].as_posix()]
    assert any("roots add" in x and old["ext"].as_posix() in x for x in rep.todo)
    assert any("catalog update" in x for x in rep.todo)
    # корінь у самому просторі переписано на нове місце
    marker = (new_machine["target"] / W.MARKER).read_text(encoding="utf-8")
    assert (new_machine["target"] / "data" / "raw").as_posix() in marker


def test_existing_catalog_and_memory_on_the_new_machine_are_kept(
        old, new_machine, tmp_path: Path) -> None:
    from nyshporka.project import memory_dir

    dest = tmp_path / "drive" / "b"
    _export(dest, with_data=True)
    (new_machine["data"] / "catalog").mkdir(parents=True)
    (new_machine["data"] / "catalog" / "newer.sqlite").write_bytes(b"n")
    mem = memory_dir(new_machine["target"])
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("своє\n", encoding="utf-8")
    rep = T.run_import(T.plan_import(dest, new_machine["target"]), echo=_quiet,
                       build_cases=False)
    assert not (new_machine["data"] / "catalog" / "pack.sqlite").exists()
    assert "лишено наявне" in rep.data["catalog"]
    assert (mem / "MEMORY.md").read_text(encoding="utf-8") == "своє\n"


def test_an_unfinished_export_is_refused(old, tmp_path: Path) -> None:
    dest = tmp_path / "drive" / "b"
    _export(dest)
    (dest / T.BUNDLE).unlink()
    (dest / T.PARTIAL).write_text("{}", encoding="utf-8")
    with pytest.raises(T.TransferError, match="неповний"):
        T.plan_import(dest, tmp_path / "B" / "x" / "Нишпорка")
    with pytest.raises(T.TransferError, match="не пакунок"):
        T.plan_import(tmp_path / "A", tmp_path / "B" / "x" / "Нишпорка")


def test_a_bundle_from_a_newer_nysh_or_a_damaged_one_is_refused(old, tmp_path: Path) -> None:
    dest = tmp_path / "drive" / "b"
    _export(dest)
    meta = json.loads((dest / T.BUNDLE).read_text(encoding="utf-8"))
    meta["nysh_version"] = "99.0.0"
    (dest / T.BUNDLE).write_text(json.dumps(meta), encoding="utf-8")
    (dest / T.WS_DIR / "data" / "raw" / "spr-1" / "0001.jpg").write_bytes(b"x")
    problems = T.plan_import(dest, tmp_path / "B" / "x" / "Нишпорка").problems
    assert any("nysh update" in x for x in problems)
    assert any("не збігається" in x and "0001.jpg" in x for x in problems)


def test_import_refuses_a_used_folder_but_moves_an_empty_skeleton_aside(
        old, new_machine, tmp_path: Path) -> None:
    dest = tmp_path / "drive" / "b"
    _export(dest)
    used = tmp_path / "B" / "used" / "Нишпорка"
    (used / "data").mkdir(parents=True)
    (used / "data" / "notes.md").write_text("моє", encoding="utf-8")
    assert any("не порожня" in x for x in T.plan_import(dest, used).problems)

    target = new_machine["target"]
    (target / ".git" / "refs").mkdir(parents=True)
    (target / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (target / W.MARKER).write_text("[workspace]\n", encoding="utf-8")
    (target / ".gitignore").write_text("data/raw/**\n", encoding="utf-8")
    p = T.plan_import(dest, target)
    assert p.skeleton and not p.problems
    rep = T.run_import(p, echo=_quiet, build_cases=False)
    assert rep.moved_aside is not None and (rep.moved_aside / W.MARKER).is_file()
    assert (target / "config" / "research_profile.yaml").is_file()


def test_installed_python_is_read_from_the_nysh_script_outside_this_env(
        tmp_path: Path, monkeypatch) -> None:
    if sys.platform == "win32":
        pytest.skip("на Windows `nysh.exe` — не скрипт")
    py = tmp_path / "tool" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_bytes(b"")
    plain, wrapped = tmp_path / "p1", tmp_path / "p2"
    plain.mkdir()
    wrapped.mkdir()
    (plain / "nysh").write_text(f"#!{py}\nimport sys\n", encoding="utf-8")
    (wrapped / "nysh").write_text(
        f"#!/bin/sh\n'''exec' \"{py}\" \"$0\" \"$@\"\n' '''\n", encoding="utf-8")
    for d in (plain, wrapped):
        monkeypatch.setenv("PATH", str(d))
        assert T._installed_python() == py.as_posix()
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    assert T._installed_python() == Path(sys.executable).as_posix()


# ── командний рядок ──────────────────────────────────────────────────────────
def test_cli_round_trip(old, new_machine, tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from nyshporka.cli import app

    monkeypatch.setattr("nyshporka.cases.db.rebuild", lambda **_k: {"cases": 2})
    dest = tmp_path / "drive" / "b"
    runner = CliRunner()
    got = runner.invoke(app, ["workspace", "export", str(dest), "--dry-run"])
    assert got.exit_code == 0, got.output
    assert not dest.exists()
    got = runner.invoke(app, ["workspace", "export", str(dest)])
    assert got.exit_code == 0, got.output
    assert "workspace import" in got.output
    got = runner.invoke(app, ["workspace", "import", str(dest), str(new_machine["target"])])
    assert got.exit_code == 0, got.output
    assert "nysh roots add" in got.output and "2 справи" in got.output
    assert os.path.isfile(new_machine["target"] / W.MARKER)


def test_export_progress_and_cached_trees(old, tmp_path: Path, monkeypatch) -> None:
    lines: list[str] = []
    dest = tmp_path / "drive" / "bundle"
    p = T.plan_export(dest, with_roots=True, with_data=True, echo=lines.append)
    assert any(line.startswith("сканую простір") for line in lines)
    assert any(line.startswith("сканую корінь") for line in lines)
    assert T.WS_DIR in p.trees

    # `run_export` reuses cached `p.trees` instead of re-scanning source directories,
    # and `mirror` emits percentage progress when `_COPY_PROGRESS_SEC` elapses.
    copy_lines: list[str] = []
    monkeypatch.setattr(T, "_COPY_PROGRESS_SEC", 0.0)
    orig_scan = T.scan
    scanned_paths: list[Path] = []

    def tracking_scan(root: Path, **kw):
        scanned_paths.append(root)
        return orig_scan(root, **kw)

    monkeypatch.setattr(T, "scan", tracking_scan)
    T.run_export(p, echo=copy_lines.append)
    assert old["hub"] not in scanned_paths
    assert old["ext"] not in scanned_paths
    assert any("%)" in line for line in copy_lines)


def test_plan_export_fails_fast_when_workspace_is_locked(
    old, tmp_path: Path, monkeypatch,
) -> None:
    from nyshporka.core import lock as L

    with L.WorkspaceLock(old["hub"]).acquire():
        monkeypatch.setattr(
            T, "scan",
            lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("scan should not run")),
        )
        p = T.plan_export(tmp_path / "drive" / "b")
        assert any("простір уже відкрито" in msg for msg in p.problems)


def test_import_progress_and_cached_trees(old, new_machine, tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "drive" / "bundle"
    ep = T.plan_export(bundle, with_roots=True, with_data=True)
    T.run_export(ep, echo=lambda _m: None)

    plan_lines: list[str] = []
    plan_prog: list[str] = []
    ip = T.plan_import(
        bundle,
        new_machine["target"],
        echo=plan_lines.append,
        progress=plan_prog.append,
    )
    assert any(line.startswith("сканую пакунок") for line in plan_lines)
    assert T.WS_DIR in ip.trees
    assert T.ROOTS_DIR in ip.trees

    # `run_import` reuses cached `ip.trees` (including sliced subtrees for `roots/...`)
    # instead of re-scanning the bundle, and routes live ticks to `progress`.
    import_lines: list[str] = []
    import_prog: list[str] = []
    monkeypatch.setattr(T, "_LIVE_PROGRESS_SEC", 0.0)
    orig_scan = T.scan
    scanned_paths: list[Path] = []

    def tracking_scan(root: Path, **kw):
        scanned_paths.append(root)
        return orig_scan(root, **kw)

    monkeypatch.setattr(T, "scan", tracking_scan)
    T.run_import(ip, echo=import_lines.append, progress=import_prog.append, build_cases=False)
    assert not any(bundle in p.parents or p == bundle for p in scanned_paths)
    assert any("%)" in line for line in import_prog)
    assert not any("%)" in line for line in import_lines)


def test_mirror_eta_uses_ema_and_file_rate(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    # One large file (100 KB) followed by 10 tiny files (10 B each).
    (src / "00_big.bin").write_bytes(b"x" * 100_000)
    for i in range(10):
        (src / f"10_small_{i}.txt").write_bytes(b"y" * 10)
    tree = T.scan(src)

    # Simulate 1s for the big file, then 1s per tiny file so file-rate dominates ETA.
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0])
    monkeypatch.setattr(T.time, "monotonic", lambda: next(ticks, 12.0))
    monkeypatch.setattr(T, "_LIVE_PROGRESS_SEC", 0.5)

    prog: list[str] = []
    T.mirror(src, tree, dst, echo=lambda _m: None, progress=prog.append)
    # After the big file + first small file (at t=2.0, 9 small files left),
    # byte ETA would be ~0s, while file-rate ETA is >5s.
    assert any("1/11 файлів" in line or "2/11 файлів" in line for line in prog)
    eta_lines = [line for line in prog if "~" in line]
    assert eta_lines
    assert not eta_lines[0].endswith("~0с)")

