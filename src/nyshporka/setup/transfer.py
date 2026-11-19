"""🧳 Перенести простір на іншу машину — `nysh workspace export / import` (#30).

FAQ давно каже «робоча тека — звичайна тека, скопіюйте цілком». Це майже так,
але ручна копія ламається тихо:

* скани поза простором (`case_roots`) записані абсолютними шляхами; кореня,
  якого на новій машині немає, `Workspace.case_roots()` мовчки не бачить, і
  справи з нього зникають із пошуку;
* середовище рушіїв (venv у просторі) не переноситься, а копіюється — гігабайти, що
  на новій машині не запустяться;
* копія під живим `nysh serve` — копія, в яку дописують;
* що доробити на новій машині (рушії, ваги, вхід у Супрягу, скіли), ніде не
  зібрано.

Розкладка копії:

    <тека>/workspace/               простір (з `.git`, без venv рушіїв і `.program`)
    <тека>/roots/01-<ім'я>/         зовнішні корені справ (`--with-roots`)
    <тека>/data/catalog|Cache/      довідники й ваги (`--with-data`)
    <тека>/nyshporka-bundle.json    пишеться ОСТАННІМ: немає — копія неповна

`nysh relocate` — про ОДНУ машину (програма в `<тека>/.program`, нова тека стає
живим простором); тут — копія на диск і розгортання на іншій машині. Спільне —
звірка пофайлово (`relocate.manifest/diff`) і замок простору на час копії.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nyshporka.setup.relocate import DATA_PARTS, SKIP_TOP, human

BUNDLE = "nyshporka-bundle.json"
FORMAT = 1
WS_DIR = "workspace"
ROOTS_DIR = "roots"
DATA_DIR = "data"

_SCAN_PROGRESS_SEC = 10.0
_COPY_PROGRESS_SEC = 15.0
_LIVE_PROGRESS_SEC = 0.25
_COPY_WORKERS = 4

#: Що з кореня простору не їде: venv рушіїв і замок (як у `relocate`) і
#: програма, яку туди поставив `relocate` — вона прив'язана до машини.
WS_SKIP_TOP = (*SKIP_TOP, ".program")
#: Окремі файли простору, що належать машині, а не дослідженню: дозволи агента.
WS_SKIP_REL = (".claude/settings.local.json",)

#: Допуск на mtime при докопіюванні. FAT/exFAT зовнішніх дисків зберігає час із
#: кроком 2 с: без допуску кожен повторний експорт копіював би все наново.
MTIME_SLACK = 2.0


class TransferError(RuntimeError):
    """Експорт чи імпорт неможливий — з причиною; нічого не зіпсовано."""


def _nfc(s: str) -> str:
    """Ключ шляху для звірки. macOS віддає імена в NFD, Windows — у NFC: без
    зведення «Йосип» з однієї машини не збігався б із тим самим ім'ям на
    іншій, і імпорт відмовляв би «копія не сходиться» на справній копії."""
    return unicodedata.normalize("NFC", s)


@dataclass
class _Entry:
    path: Path
    size: int
    mtime: float


def _format_eta(seconds: float) -> str:
    secs = max(int(seconds), 0)
    if secs < 60:
        return f"{secs} с"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins} хв {secs:02d} с"
    hours, mins = divmod(mins, 60)
    return f"{hours} год {mins:02d} хв"


def _walk(root: Path, skip_top: tuple[str, ...] = (),
          skip_rel: tuple[str, ...] = (), *, label: str = "",
          echo: Callable[[str], None] | None = None,
          progress: Callable[[str], None] | None = None) -> dict[str, _Entry]:
    """Кожен файл під `root`: NFC-шлях відносно `root` → запис."""
    out: dict[str, _Entry] = {}
    if not root.is_dir():
        return out
    skip = {_nfc(s) for s in skip_rel}
    report = progress or echo
    interval = _LIVE_PROGRESS_SEC if progress is not None else _SCAN_PROGRESS_SEC
    last = time.monotonic()
    total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        if here == root:
            dirnames[:] = [d for d in dirnames if d not in skip_top]
            filenames = [f for f in filenames if f not in skip_top]
        for name in filenames:
            p = here / name
            rel = _nfc(p.relative_to(root).as_posix())
            if rel in skip:
                continue
            try:
                st = p.stat()
            except OSError:
                out[rel] = _Entry(p, -1, 0.0)
                continue
            out[rel] = _Entry(p, st.st_size, st.st_mtime)
            total_bytes += max(st.st_size, 0)
            if report is not None and label:
                now = time.monotonic()
                if now - last >= interval:
                    last = now
                    report(f"  … {label}: знайдено {len(out)} файлів ({human(total_bytes)})")
    if echo is not None and label:
        echo(f"  ✓ {label}: {len(out)} файлів ({human(total_bytes)})")
    return out


@dataclass
class SyncReport:
    copied: int = 0
    kept: int = 0
    removed: int = 0
    copied_bytes: int = 0
    #: Що вийшло в копії: NFC-шлях → розмір.
    files: dict[str, int] = field(default_factory=dict)


def _copy_one(src_file: Path, dst_file: Path) -> int:
    try:
        shutil.copy2(src_file, dst_file)
    except FileNotFoundError:
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src_file, dst_file)
        except OSError as exc:
            raise TransferError(f"не вдалось скопіювати {src_file} → {dst_file}: {exc}") from None
    except OSError as exc:
        raise TransferError(f"не вдалось скопіювати {src_file} → {dst_file}: {exc}") from None
    try:
        return dst_file.stat().st_size
    except OSError:
        return -1


def sync_tree(src: Path, dst: Path, skip_top: tuple[str, ...] = (),
              skip_rel: tuple[str, ...] = (), *,
              want: dict[str, _Entry] | None = None,
              echo: Callable[[str], None] = print,
              progress: Callable[[str], None] | None = None,
              tick: Callable[[], None] | None = None) -> SyncReport:
    """Дзеркало `src` у `dst`: нове й змінене копіюється, зайве в `dst` знімається.

    Незмінений файл (той самий розмір, mtime у межах `MTIME_SLACK`) не
    копіюється — повторний експорт на той самий диск коштує лише змінене.
    Зайве знімається, бо інакше видалена картка воскресла б після імпорту.
    Наприкінці — звірка пофайлово; не зійшлось — `TransferError`.
    """
    if want is None:
        want = _walk(src, skip_top, skip_rel)
    have = _walk(dst)
    rep = SyncReport()
    # 🔴 Теки — теж, і порожні: git без `.git/refs` і `.git/objects` (вони
    # порожні в репозиторії без комітів чи після `gc`) не впізнає репозиторій,
    # і історія простору після імпорту «зникала» б.
    for dirpath, dirnames, _files in os.walk(src):
        here = Path(dirpath)
        if here == src:
            dirnames[:] = [d for d in dirnames if d not in skip_top]
        for d in dirnames:
            (dst / (here / d).relative_to(src)).mkdir(parents=True, exist_ok=True)

    pending: list[tuple[str, _Entry, Path]] = []
    total_bytes = 0
    for rel, e in want.items():
        h = have.get(rel)
        if h is not None and h.size == e.size and abs(h.mtime - e.mtime) <= MTIME_SLACK:
            rep.kept += 1
            continue
        target = h.path if h is not None else dst / e.path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        pending.append((rel, e, target))
        total_bytes += max(e.size, 0)

    total_files = len(pending)
    got = {k: v.size for k, v in have.items()}
    report = progress or echo
    interval = _LIVE_PROGRESS_SEC if progress is not None else _COPY_PROGRESS_SEC
    started = last_report = prev_t = time.monotonic()
    prev_bytes = prev_files = 0
    ema_byte_rate = ema_file_rate = 0.0
    alpha = 0.3

    def _on_copied(rel: str, e: _Entry, copied_size: int) -> None:
        nonlocal last_report, prev_t, prev_bytes, prev_files, ema_byte_rate, ema_file_rate
        rep.copied += 1
        rep.copied_bytes += max(e.size, 0)
        if copied_size >= 0:
            got[rel] = copied_size
        now = time.monotonic()
        if now - last_report >= interval:
            last_report = now
            dt = now - prev_t
            if dt > 0.05:
                inst_bytes = (rep.copied_bytes - prev_bytes) / dt
                inst_files = (rep.copied - prev_files) / dt
                ema_byte_rate = (inst_bytes if ema_byte_rate == 0.0
                                 else alpha * inst_bytes + (1.0 - alpha) * ema_byte_rate)
                ema_file_rate = (inst_files if ema_file_rate == 0.0
                                 else alpha * inst_files + (1.0 - alpha) * ema_file_rate)
                prev_t = now
                prev_bytes = rep.copied_bytes
                prev_files = rep.copied
            pct = int(rep.copied_bytes * 100 / total_bytes) if total_bytes > 0 else 100
            if now - started >= 1.0 and (total_bytes > rep.copied_bytes or total_files > rep.copied):
                eta_bytes = ((total_bytes - rep.copied_bytes) / ema_byte_rate
                             if ema_byte_rate > 0 and total_bytes > rep.copied_bytes else 0.0)
                eta_files = ((total_files - rep.copied) / ema_file_rate
                             if ema_file_rate > 0 and total_files > rep.copied else 0.0)
                eta = max(eta_bytes, eta_files)
                report(f"    … {human(rep.copied_bytes)} з {human(total_bytes)} "
                       f"({pct}%) · {rep.copied}/{total_files} файлів · "
                       f"{human(int(ema_byte_rate))}/с · ~{_format_eta(eta)}")
            else:
                report(f"    … {human(rep.copied_bytes)} з {human(total_bytes)} "
                       f"({pct}%) · {rep.copied}/{total_files} файлів")
            if tick is not None:
                tick()

    if len(pending) <= 1:
        for rel, e, target in pending:
            _on_copied(rel, e, _copy_one(e.path, target))
    else:
        with ThreadPoolExecutor(max_workers=min(_COPY_WORKERS, len(pending))) as pool:
            inflight = {}
            it = iter(pending)
            for _ in range(min(_COPY_WORKERS * 2, len(pending))):
                rel, e, target = next(it)
                inflight[pool.submit(_copy_one, e.path, target)] = (rel, e)
            while inflight:
                done, _ = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in done:
                    rel, e = inflight.pop(fut)
                    _on_copied(rel, e, fut.result())
                    nxt = next(it, None)
                    if nxt is not None:
                        n_rel, n_e, n_target = nxt
                        inflight[pool.submit(_copy_one, n_e.path, n_target)] = (n_rel, n_e)

    for rel, h in have.items():
        if rel not in want:
            h.path.unlink()
            got.pop(rel, None)
            rep.removed += 1
    bad = [f"немає в копії: {k}" for k in want if k not in got]
    bad += [f"інший розмір: {k}" for k, e in want.items() if k in got and got[k] != e.size]
    if bad:
        raise TransferError("копія не зійшлась з оригіналом:\n  " + "\n  ".join(bad[:20]))
    rep.files = {k: e.size for k, e in want.items()}
    return rep


def _inside(a: Path, b: Path) -> bool:
    try:
        a.resolve().relative_to(b.resolve())
        return True
    except ValueError:
        return False


def _root_dir_name(i: int, root: Path) -> str:
    keep = "".join(c if c.isalnum() or c in "-_ ." else "_" for c in root.name).strip()
    return f"{i:02d}-{keep or 'root'}"


# ── експорт ──────────────────────────────────────────────────────────────────
@dataclass
class ExportPlan:
    workspace: Path
    target: Path
    #: Зовнішні корені справ: (корінь, тека в копії або None — не переноситься).
    roots: list[tuple[Path, str | None]] = field(default_factory=list)
    data_from: Path | None = None
    engines: bool = False
    workspace_bytes: int = 0
    roots_bytes: int = 0
    data_bytes: int = 0
    free_bytes: int = -1
    problems: list[str] = field(default_factory=list)
    trees: dict[str, dict[str, _Entry]] = field(default_factory=dict, repr=False)

    @property
    def total_bytes(self) -> int:
        return self.workspace_bytes + self.roots_bytes + self.data_bytes


def _tree_bytes(entries: dict[str, _Entry]) -> int:
    return sum(max(e.size, 0) for e in entries.values())


def _size(root: Path, skip_top: tuple[str, ...] = (), skip_rel: tuple[str, ...] = ()) -> int:
    return _tree_bytes(_walk(root, skip_top, skip_rel))


def plan_export(target: str | Path, *, with_roots: bool = False,
                with_data: bool = False,
                echo: Callable[[str], None] | None = None,
                progress: Callable[[str], None] | None = None) -> ExportPlan:
    """Порахувати експорт. Нічого не пише."""
    from nyshporka.core.appdirs import data_dir
    from nyshporka.core.lock import LockBusy, check_busy
    from nyshporka.core.workspace import validate_root, workspace
    from nyshporka.setup.doctor import engine_venv

    ws = workspace()
    to = validate_root(Path(target))
    p = ExportPlan(workspace=ws.hub, target=to)
    if _inside(to, ws.hub) or _inside(ws.hub, to):
        p.problems.append("тека копії не може лежати в просторі чи містити його")
    elif to.exists() and any(to.iterdir()) and not (
            (to / BUNDLE).is_file() or (to / WS_DIR).is_dir()):
        p.problems.append(f"{to} не порожня й не є копією простору — потрібна нова, "
                          f"порожня або тека попереднього експорту")
    holder = check_busy(ws.hub)
    if holder is not None:
        p.problems.append(f"{LockBusy(holder)} Закрийте вікно Нишпорки (і `nysh serve`) "
                          f"на час експорту.")
        return p
    for i, root in enumerate(r for r in ws.extra_case_roots if r.is_dir()):
        if _inside(to, root) or _inside(root, to):
            p.problems.append(f"тека копії перетинається з коренем справ {root}")
        p.roots.append((root, f"{ROOTS_DIR}/{_root_dir_name(i + 1, root)}" if with_roots
                        else None))
    p.roots += [(r, None) for r in ws.extra_case_roots if not r.is_dir()]
    try:
        p.engines = engine_venv().is_dir() and _inside(engine_venv(), ws.hub)
    except Exception:
        p.engines = False
    ws_walk = _walk(ws.hub, WS_SKIP_TOP, WS_SKIP_REL,
                    label=f"простір {ws.hub.name}", echo=echo, progress=progress)
    p.trees[WS_DIR] = ws_walk
    p.workspace_bytes = _tree_bytes(ws_walk)
    for r, d in p.roots:
        if d:
            rw = _walk(r, label=f"корінь {r.name}", echo=echo, progress=progress)
            p.trees[d] = rw
            p.roots_bytes += _tree_bytes(rw)
    if with_data:
        p.data_from = data_dir()
        for part in DATA_PARTS:
            dw = _walk(p.data_from / part, label=f"довідники {part}",
                       echo=echo, progress=progress)
            p.trees[f"{DATA_DIR}/{part}"] = dw
            p.data_bytes += _tree_bytes(dw)
    present = _size(to) if to.is_dir() else 0
    probe = to
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        p.free_bytes = shutil.disk_usage(probe).free
    except OSError:
        p.free_bytes = -1
    if 0 <= p.free_bytes < p.total_bytes - present:
        p.problems.append(f"на диску {to.anchor} вільно {human(p.free_bytes)}, а треба "
                          f"близько {human(p.total_bytes - present)}")
    return p


def run_export(p: ExportPlan, echo: Callable[[str], None] = print, *,
               progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Скопіювати простір (і, за планом, корені та довідники), паспорт — останнім."""
    from nyshporka import __version__
    from nyshporka.core.lock import LockBusy, WorkspaceLock

    if p.problems:
        raise TransferError("\n".join(p.problems))
    p.target.mkdir(parents=True, exist_ok=True)
    # Паспорт знімається ПЕРШИМ: обірваний повторний експорт не має виглядати
    # повною копією зі старим переліком.
    (p.target / BUNDLE).unlink(missing_ok=True)
    files: dict[str, int] = {}

    def take(prefix: str, rep: SyncReport) -> None:
        files.update({f"{prefix}/{k}": v for k, v in rep.files.items()})
        echo(f"  {prefix}: скопійовано {rep.copied} ({human(rep.copied_bytes)}), "
             f"без змін {rep.kept}" + (f", знято зайвих {rep.removed}" if rep.removed else ""))

    # 🔴 Застосунок у браузері тримає замок простору: копія під живим демоном —
    # копія, в яку дописують, і звірка, що бреше.
    try:
        lock = WorkspaceLock(p.workspace).acquire()
    except LockBusy as exc:
        raise TransferError(f"{exc} Закрийте вікно Нишпорки (і `nysh serve`) на час "
                            f"експорту.") from None
    echo(f"1. простір {p.workspace}")
    with lock:
        take(WS_DIR, sync_tree(
            p.workspace, p.target / WS_DIR, WS_SKIP_TOP, WS_SKIP_REL,
            want=p.trees.get(WS_DIR), echo=echo, progress=progress, tick=lock.beat,
        ))

    roots = [(r, d) for r, d in p.roots if d]
    if roots:
        echo("2. корені справ")
        for root, d in roots:
            take(d, sync_tree(root, p.target / d, want=p.trees.get(d),
                               echo=echo, progress=progress))
    # Корені, яких цього разу не беруть, не мають лишатись у копії від минулого.
    old_roots = p.target / ROOTS_DIR
    if old_roots.is_dir():
        for sub in old_roots.iterdir():
            if f"{ROOTS_DIR}/{sub.name}" not in {d for _, d in roots}:
                shutil.rmtree(sub)
    if p.data_from is not None:
        echo("3. довідники й ваги")
        for part in DATA_PARTS:
            if (p.data_from / part).is_dir():
                sec = f"{DATA_DIR}/{part}"
                take(sec, sync_tree(p.data_from / part, p.target / sec,
                                     want=p.trees.get(sec), echo=echo, progress=progress))
    elif (p.target / DATA_DIR).is_dir():
        shutil.rmtree(p.target / DATA_DIR)

    bundle = {
        "format": FORMAT,
        "nyshporka": __version__,
        "platform": sys.platform,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": str(p.workspace),
        "engines": p.engines,
        "roots": [{"original": str(r).replace("\\", "/"), "dir": d} for r, d in p.roots],
        "data": p.data_from is not None,
        "files": files,
    }
    from nyshporka.utils.atomic import atomic_write_text

    atomic_write_text(p.target / BUNDLE, json.dumps(bundle, ensure_ascii=False, indent=1))
    return bundle


# ── імпорт ───────────────────────────────────────────────────────────────────
@dataclass
class RootMove:
    original: str
    #: keep — шлях існує на цій машині; copy — береться з копії; missing — ні там, ні там.
    action: str
    new: Path | None = None
    copied: str | None = None


@dataclass
class ImportPlan:
    source: Path
    dest: Path
    bundle: dict[str, Any]
    roots: list[RootMove] = field(default_factory=list)
    roots_to: Path | None = None
    data_parts: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    source_walk: dict[str, _Entry] = field(default_factory=dict, repr=False)


def read_bundle(source: Path) -> dict[str, Any]:
    path = source / BUNDLE
    if not path.is_file():
        raise TransferError(f"у {source} немає {BUNDLE} — копія неповна (експорт "
                            f"обірвався) або це не копія простору")
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TransferError(f"{path} не читається: {exc}") from None
    if not isinstance(bundle, dict) or bundle.get("format") != FORMAT:
        raise TransferError(f"{path}: невідомий формат копії — оновіть Нишпорку "
                            f"(`nysh update`)")
    return bundle


def _verify_walk(source: Path, bundle: dict[str, Any], *,
                 echo: Callable[[str], None] | None = None,
                 progress: Callable[[str], None] | None = None) -> tuple[list[str], dict[str, _Entry]]:
    want: dict[str, int] = bundle.get("files") or {}
    walked = _walk(source, label="звірка копії", echo=echo, progress=progress)
    got = {k: e.size for k, e in walked.items()}
    bad = [f"немає: {k}" for k in want if _nfc(k) not in got]
    bad += [f"інший розмір: {k}" for k, v in want.items() if _nfc(k) in got and got[_nfc(k)] != v]
    return bad, walked


def verify_copy(source: Path, bundle: dict[str, Any]) -> list[str]:
    """Чого в копії бракує проти переліку з паспорта. Порожньо — зійшлось."""
    bad, _ = _verify_walk(source, bundle)
    return bad


def plan_import(source: str | Path, dest: str | Path | None = None, *,
                roots_to: str | Path | None = None,
                echo: Callable[[str], None] | None = None,
                progress: Callable[[str], None] | None = None) -> ImportPlan:
    """Порахувати імпорт. Нічого не пише."""
    from nyshporka import __version__
    from nyshporka.core.appdirs import data_dir
    from nyshporka.core.workspace import validate_root
    from nyshporka.setup.update import _key
    from nyshporka.setup.wizard import _is_synced, default_root

    src = Path(source).expanduser().resolve()
    bundle = read_bundle(src)
    to = validate_root(Path(dest) if dest else default_root())
    p = ImportPlan(source=src, dest=to, bundle=bundle)
    made = str(bundle.get("nyshporka") or "0")
    if _key(made) > _key(__version__):
        p.problems.append(f"копію зроблено Нишпоркою {made}, а тут {__version__} — "
                          f"спершу `nysh update`")
    bad, walked = _verify_walk(src, bundle, echo=echo, progress=progress)
    p.source_walk = walked
    if bad:
        p.problems.append("копія не сходиться з переліком у паспорті:\n  "
                          + "\n  ".join(bad[:20]))
    if _is_synced(to):
        p.problems.append(f"{to} синхронізується з хмарою — оберіть теку поза нею")
    if _inside(to, src) or _inside(src, to):
        p.problems.append("простір не може лежати в копії чи містити її")
    elif to.exists() and any(to.iterdir()):
        p.problems.append(f"{to} не порожня — потрібна нова або порожня тека")

    p.roots_to = (Path(roots_to).expanduser() if roots_to
                  else to.parent / f"{to.name}-скани")
    for r in bundle.get("roots") or []:
        original, copied = str(r.get("original") or ""), r.get("dir")
        if not original:
            continue
        here = Path(original)
        if here.is_absolute() and here.is_dir():
            # Той самий диск із тією самою літерою (або та сама машина):
            # копіювати нема чого, корінь лишається, де був.
            p.roots.append(RootMove(original, "keep", here))
        elif copied and (src / copied).is_dir():
            new = p.roots_to / Path(copied).name
            if new.exists() and any(new.iterdir()):
                p.problems.append(f"{new} уже існує й не порожня — назвіть іншу теку "
                                  f"`--roots-to`")
            p.roots.append(RootMove(original, "copy", new, copied))
        else:
            p.roots.append(RootMove(original, "missing"))
    if bundle.get("data"):
        for part in DATA_PARTS:
            if (src / DATA_DIR / part).is_dir() and not (
                    (data_dir() / part).is_dir() and any((data_dir() / part).iterdir())):
                p.data_parts.append(part)
    return p


Runner = Callable[[list[str]], int]


def _run(cmd: list[str]) -> int:
    return subprocess.run(cmd, check=False).returncode


def _set_case_roots(marker: Path, roots: list[str]) -> None:
    from nyshporka.core import workspace as W

    text = marker.read_text(encoding="utf-8") if marker.is_file() else "[workspace]\n"
    listed = ", ".join(f'"{r}"' for r in roots)
    W._write_marker(marker, W._place_case_roots(
        text, f"case_roots = [{listed}]" if roots else None))


def _subtree_walk(entries: dict[str, _Entry], prefix: str) -> dict[str, _Entry] | None:
    if not entries:
        return None
    head = prefix + "/"
    n = len(head)
    return {rel[n:]: e for rel, e in entries.items() if rel.startswith(head)}


def run_import(p: ImportPlan, *, runner: Runner = _run,
               echo: Callable[[str], None] = print,
               progress: Callable[[str], None] | None = None) -> list[str]:
    """Розгорнути копію. Повертає, що лишилось зробити на цій машині руками."""
    from nyshporka.canon import hook as H
    from nyshporka.core import workspace as W
    from nyshporka.core.appdirs import data_dir

    if p.problems:
        raise TransferError("\n".join(p.problems))
    echo(f"1. простір → {p.dest}")
    rep = sync_tree(p.source / WS_DIR, p.dest,
                    want=_subtree_walk(p.source_walk, WS_DIR),
                    echo=echo, progress=progress)
    echo(f"  скопійовано {rep.copied} файлів ({human(rep.copied_bytes)})")

    moves = [m for m in p.roots if m.action == "copy"]
    if moves:
        echo(f"2. корені справ → {p.roots_to}")
        for m in moves:
            assert m.new is not None and m.copied is not None
            r = sync_tree(p.source / m.copied, m.new,
                          want=_subtree_walk(p.source_walk, m.copied),
                          echo=echo, progress=progress)
            echo(f"  {m.original} → {m.new}  ({r.copied} файлів)")
    # Відсутні корені лишаються в маркері: `nysh roots list` покаже їх із «теки
    # немає», а не сховає разом зі справами. Диск можуть підключити пізніше.
    final = [str(m.new).replace("\\", "/") if m.action != "missing" and m.new is not None
             else m.original for m in p.roots]
    _set_case_roots(p.dest / W.MARKER, final)

    for part in p.data_parts:
        echo(f"3. {part} → {data_dir() / part}")
        sync_tree(p.source / DATA_DIR / part, data_dir() / part,
                  want=_subtree_walk(p.source_walk, f"{DATA_DIR}/{part}"),
                  echo=echo, progress=progress)

    hook = p.dest / ".git" / "hooks" / "pre-commit"
    if hook.is_file() and any(m in hook.read_text(encoding="utf-8", errors="replace")
                              for m in H.MARKERS):
        H.install(p.dest)
        echo("  хук канону — з інтерпретатором цієї машини")

    W.state_set(workspace=str(p.dest))
    me = [sys.executable, "-m", "nyshporka", "--workspace", str(p.dest)]
    echo("4. реєстр справ")
    if runner([*me, "cases", "build"]) != 0:
        echo("  ⚠ реєстр не зібрався — повторіть `nysh cases build`")

    todo: list[str] = []
    for m in p.roots:
        if m.action == "missing":
            todo.append(f"корінь справ {m.original} не перенесено: підключіть диск або "
                        f"`nysh roots remove \"{m.original}\"` і `nysh roots add <тека>`")
    if p.bundle.get("engines"):
        todo.append("рушії читання: `nysh htr install`, потім `nysh models get`")
    todo.append("вхід у Супрягу (ключ лежить у сховищі системи й не переноситься): "
                "`nysh share login`")
    todo.append("скіли для агента: `nysh skills install`")
    runner([*me, "doctor"])
    return todo
