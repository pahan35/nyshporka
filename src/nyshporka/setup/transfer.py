"""🧳 Перенести простір на інший комп'ютер: `nysh workspace export` / `import`.

🔴 Навіщо окрема команда, якщо простір — звичайна тека. Скопіювати теку руками
можна, але чотири речі при цьому ламаються тихо:

* **корені справ поза простором** (`case_roots` у маркері — зовнішній диск,
  мережева тека). На новій машині їх немає, і `Workspace.case_roots()` мовчки
  їх відсіює: справи просто зникають з бібліотеки, без жодного попередження;
* **хук git для канону** несе абсолютний шлях до інтерпретатора старої машини.
  Він їде разом із `.git`, і на новій машині коміти історії падають;
* **середовище рушіїв** (venv у просторі) копіюється гігабайтами, хоч на іншій ОС
  воно непридатне: venv не переноситься, у ньому абсолютні шляхи;
* **копія під живою консоллю**: демон дописує файли, поки їх копіюють, і копія
  виходить неузгодженою.

Тому перенесення — це пакунок на диску з описом (`nyshporka-bundle.json`), а не
просто копія теки. Пакунок має таку розкладку:

    <тека>/nyshporka-bundle.json   опис і перелік файлів; пишеться ОСТАННІМ
    <тека>/workspace/              Майстерня з проєктами (без venv і замка)
    <тека>/roots/<n>-<ім'я>/        зовнішні корені справ (`--with-roots`)
    <тека>/data/catalog, cache/    довідники й ваги (`--with-data`)
    <тека>/agent-memory/…          автопам'ять помічника для простору й проєктів

🔴 Опис пишеться останнім, і саме він робить пакунок повним. Експорт, що
обірвався (витягнули диск, заснув ноутбук), лишає теку без опису, і `import`
таку теку не бере. Повторний експорт у ту саму теку докопійовує лише змінене.

Команда навмисно працює й із теки з кодом (`uv run nysh workspace …`) поруч зі
встановленою Нишпоркою: тоді вона не чіпає ні скілів, ні хуків помічника цієї
машини (див. `_NO_SKILL_SYNC_FOR` у `cli.py`), а хук git ставить на інтерпретатор
встановленої програми, якщо його вдається знайти.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nyshporka.setup.relocate import SKIP_TOP, diff
from nyshporka.setup.relocate import human as human  # друк розмірів і для `cli.py`

#: Опис пакунка. Немає його — пакунок неповний.
BUNDLE = "nyshporka-bundle.json"
#: Позначка «експорт іде»: з нею повторний експорт знає, що тека наша.
PARTIAL = "nyshporka-bundle.partial"
#: Версія розкладки пакунка. Новіша, ніж знає ця програма, — не імпортуємо.
FORMAT = 1

WS_DIR = "workspace"
ROOTS_DIR = "roots"
DATA_DIR = "data"
MEMORY_DIR = "agent-memory"
#: Ключ автопам'яті самої Майстерні (решта — `projects/<ім'я>`).
HUB_MEMORY = "_hub"

#: Скільки місця лишити понад те, що копіюємо.
SPARE = 256 * 1024 ** 2
#: Файлові системи, де файл понад 4 ГБ не лягає.
_FAT = frozenset({"msdos", "vfat", "fat", "fat16", "fat32"})
#: Файлові системи з правилами імен Windows (зовнішні диски, відформатовані там).
_WIN_NAMES_FS = _FAT | {"exfat", "ntfs", "ntfs3"}
_FAT_LIMIT = 4 * 1024 ** 3 - 1
#: Різниця часу зміни, яку ще вважаємо «тим самим файлом»: FAT/exFAT
#: зберігають час із кроком до 2 с.
_MTIME_SLACK = 2.0
_LIVE_PROGRESS_SEC = 0.5
_SCAN_PROGRESS_SEC = 10.0
_COPY_PROGRESS_SEC = 15.0
_COPY_STEP_MIN = 256 * 1024 ** 2
_COPY_STEP_MAX = 512 * 1024 ** 2
_COPY_WORKERS = 4

_WIN_BAD_CHARS = re.compile(r'[<>:"|?*\\\x00-\x1f]')
_WIN_RESERVED = frozenset({"con", "prn", "aux", "nul",
                           *(f"com{i}" for i in range(1, 10)),
                           *(f"lpt{i}" for i in range(1, 10))})


class TransferError(RuntimeError):
    """Перенесення неможливе — з поясненням, що робити."""


# ── обхід дерева ─────────────────────────────────────────────────────────────
@dataclass
class Tree:
    """Файли дерева (шлях → розмір, час зміни), порожні теки й посилання."""

    files: dict[str, tuple[int, float]] = field(default_factory=dict)
    dirs: list[str] = field(default_factory=list)
    #: Символьні посилання й junction-теки. Їх НЕ розкриваємо: посилання на
    #: архів на іншому диску перетворило б пакунок на копію всього архіву.
    links: list[str] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        return sum(max(s, 0) for s, _ in self.files.values())

    def sizes(self) -> dict[str, int]:
        return {k: s for k, (s, _) in self.files.items()}


def _is_link(p: Path) -> bool:
    if p.is_symlink():
        return True
    isjunction = getattr(os.path, "isjunction", None)   # Python 3.12+
    return bool(isjunction and isjunction(p))


def _skip_top(name: str) -> bool:
    """Що з кореня простору не їде: замок і будь-яке середовище Python.

    `.venv*`, а не лише імена рушіїв із `doctor`: у просторі буває й звичайний
    `.venv` (його згадує хук git), і він так само непереносний.
    """
    return name in SKIP_TOP or name.startswith(".venv")


def scan(root: Path, *, top: Callable[[str], bool] | None = None,
         skip: Callable[[str], bool] | None = None,
         on_progress: Callable[[int, int], None] | None = None,
         progress_sec: float | None = None) -> Tree:
    """Обійти `root`, не заходячи в посилання.

    `top` відсіює імена лише в корені, `skip` — відносні шляхи будь-де.
    """
    out = Tree()
    if not root.is_dir():
        return out
    total_bytes = 0
    interval = _SCAN_PROGRESS_SEC if progress_sec is None else progress_sec
    last_report = time.monotonic()
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        rel_here = "" if here == root else here.relative_to(root).as_posix()
        if here == root and top is not None:
            dirnames[:] = [d for d in dirnames if not top(d)]
            filenames = [f for f in filenames if not top(f)]
        keep = []
        for d in dirnames:
            rel = f"{rel_here}/{d}" if rel_here else d
            if skip is not None and skip(rel):
                continue
            if _is_link(here / d):
                out.links.append(rel)
                continue
            keep.append(d)
            out.dirs.append(rel)
        dirnames[:] = keep
        for name in filenames:
            rel = f"{rel_here}/{name}" if rel_here else name
            if skip is not None and skip(rel):
                continue
            p = here / name
            if _is_link(p):
                out.links.append(rel)
                continue
            try:
                st = p.stat()
                out.files[rel] = (st.st_size, st.st_mtime)
                total_bytes += max(st.st_size, 0)
            except OSError:
                out.files[rel] = (-1, 0.0)
            if on_progress is not None and (len(out.files) & 255) == 0:
                now = time.monotonic()
                if now - last_report >= interval:
                    last_report = now
                    on_progress(len(out.files), total_bytes)
    return out


def _format_eta(seconds: float) -> str:
    sec = max(round(seconds), 0)
    if sec < 60:
        return f"{sec}с"
    minutes, sec = divmod(sec, 60)
    if minutes < 60:
        return f"{minutes}хв {sec:02d}с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}год {minutes:02d}хв"


def _scan_section(root: Path, *, label: str, echo: Callable[[str], None] | None = None,
                  progress: Callable[[str], None] | None = None,
                  top: Callable[[str], bool] | None = None,
                  skip: Callable[[str], bool] | None = None) -> Tree:
    """Просканувати розділ, показуючи перебіг для великих тек."""
    if echo is None and progress is None:
        return scan(root, top=top, skip=skip)
    if echo is not None:
        echo(f"сканую {label}…")
    report = progress or echo
    assert report is not None
    return scan(
        root,
        top=top,
        skip=skip,
        progress_sec=_LIVE_PROGRESS_SEC if progress is not None else None,
        on_progress=lambda count, size: report(f"  … {label}: {count} файлів ({human(size)})"),
    )


def _same(dst: Path, size: int, mtime: float) -> bool:
    try:
        st = dst.stat()
    except OSError:
        return False
    return st.st_size == size and abs(st.st_mtime - mtime) <= _MTIME_SLACK


def _pending_copies(tree: Tree, dst: Path) -> tuple[list[tuple[str, int]], int, int]:
    """Зібрати список файлів для копіювання, суму байтів і кількість за один обхід."""
    pending: list[tuple[str, int]] = []
    total_bytes = 0
    for rel, (s, m) in tree.files.items():
        if not _same(dst / rel, s, m):
            sz = max(s, 0)
            pending.append((rel, sz))
            total_bytes += sz
    return pending, total_bytes, len(pending)


def to_copy(tree: Tree, dst: Path) -> int:
    """Скільки байтів ще треба покласти в `dst` (решта там уже така сама)."""
    return _pending_copies(tree, dst)[1]


def _copy_one(src_file: Path, dst_file: Path) -> None:
    try:
        shutil.copy2(src_file, dst_file)
    except FileNotFoundError:
        # `tree.dirs` створюється заздалегідь; цей fallback лише на випадок синтетичного `Tree`.
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src_file, dst_file)
        except OSError as exc:
            raise TransferError(f"не вдалось скопіювати {src_file} → {dst_file}: {exc}") from None
    except OSError as exc:
        raise TransferError(f"не вдалось скопіювати {src_file} → {dst_file}: {exc}") from None


def _mirror_verified(
    src: Path, tree: Tree, dst: Path, *, prune: bool = False,
    echo: Callable[[str], None] = print,
    progress: Callable[[str], None] | None = None,
    tick: Callable[[], None] | None = None,
) -> tuple[int, int, Tree]:
    """Покласти `tree` із `src` у `dst` і повернути `(copied, done_bytes, verify_tree)`."""
    dst.mkdir(parents=True, exist_ok=True)
    for rel in tree.dirs:
        (dst / rel).mkdir(parents=True, exist_ok=True)
    pending, total, total_files = _pending_copies(tree, dst)
    copied = done_bytes = 0
    step = min(max(total // 20, _COPY_STEP_MIN), _COPY_STEP_MAX) if total > 0 else _COPY_STEP_MIN
    shown = 0
    report = progress or echo
    progress_sec = _LIVE_PROGRESS_SEC if progress is not None else _COPY_PROGRESS_SEC
    started = last_report = prev_t = time.monotonic()
    prev_bytes = prev_files = 0
    ema_byte_rate = ema_file_rate = 0.0
    alpha = 0.3

    def _on_copied(sz: int) -> None:
        nonlocal copied, done_bytes, shown, last_report, prev_t, prev_bytes, prev_files
        nonlocal ema_byte_rate, ema_file_rate
        copied += 1
        done_bytes += sz
        now = time.monotonic()
        should_report = (now - last_report >= progress_sec if progress is not None
                         else done_bytes - shown >= step or now - last_report >= progress_sec)
        if not should_report:
            return
        shown = done_bytes
        last_report = now
        dt = now - prev_t
        if dt > 0.05:
            inst_bytes = (done_bytes - prev_bytes) / dt
            inst_files = (copied - prev_files) / dt
            ema_byte_rate = (inst_bytes if ema_byte_rate == 0.0
                             else alpha * inst_bytes + (1.0 - alpha) * ema_byte_rate)
            ema_file_rate = (inst_files if ema_file_rate == 0.0
                             else alpha * inst_files + (1.0 - alpha) * ema_file_rate)
            prev_t = now
            prev_bytes = done_bytes
            prev_files = copied
        pct = int(done_bytes * 100 / total) if total > 0 else 100
        if now - started >= 1.0 and (total > done_bytes or total_files > copied):
            eta_bytes = ((total - done_bytes) / ema_byte_rate
                         if ema_byte_rate > 0 and total > done_bytes else 0.0)
            eta_files = ((total_files - copied) / ema_file_rate
                         if ema_file_rate > 0 and total_files > copied else 0.0)
            eta = max(eta_bytes, eta_files)
            report(f"    … {human(done_bytes)} з {human(total)} "
                   f"({pct}%) · {copied}/{total_files} файлів · "
                   f"{human(int(ema_byte_rate))}/с · ~{_format_eta(eta)}")
        else:
            report(f"    … {human(done_bytes)} з {human(total)} "
                   f"({pct}%) · {copied}/{total_files} файлів")
        if tick is not None:
            tick()

    workers = max(_COPY_WORKERS, 1)
    if workers == 1 or len(pending) <= 1:
        for rel, sz in pending:
            _copy_one(src / rel, dst / rel)
            _on_copied(sz)
    else:
        max_in_flight = workers * 32
        it = iter(pending)
        in_flight: dict[Any, int] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for rel, sz in it:
                in_flight[pool.submit(_copy_one, src / rel, dst / rel)] = sz
                if len(in_flight) >= max_in_flight:
                    done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                    for fut in done:
                        fut.result()
                        _on_copied(in_flight.pop(fut))
            while in_flight:
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for fut in done:
                    fut.result()
                    _on_copied(in_flight.pop(fut))

    verify_tree = scan(
        dst,
        progress_sec=_LIVE_PROGRESS_SEC if progress is not None else None,
        on_progress=(lambda count, _size: report(f"  … звіряю {dst.name}: {count} файлів"))
        if progress is not None else None,
    )
    if prune:
        for rel in list(verify_tree.files):
            if rel not in tree.files:
                (dst / rel).unlink(missing_ok=True)
                verify_tree.files.pop(rel, None)
    bad = diff(tree.sizes(), verify_tree.sizes())
    if bad:
        raise TransferError("копія не зійшлась з оригіналом:\n  " + "\n  ".join(bad[:20]))
    same = len(tree.files) - copied
    echo(f"  ✓ звірено {len(tree.files)} файлів: скопійовано {copied} ({human(done_bytes)})"
         + (f", {same} вже були такі самі" if same else ""))
    return copied, done_bytes, verify_tree


def mirror(src: Path, tree: Tree, dst: Path, *, prune: bool = False,
           echo: Callable[[str], None] = print,
           progress: Callable[[str], None] | None = None,
           tick: Callable[[], None] | None = None) -> tuple[int, int]:
    """Покласти `tree` із `src` у `dst`: лише нове й змінене, потім звірити.

    `prune` прибирає з `dst` файли, яких у джерелі вже немає: інакше видалене
    на старій машині воскресало б на новій. Ставиться лише для тек пакунка —
    вони наші.

    🔴 Звірка пофайлова (шлях + розмір), і розбіжність — відмова: диск, який
    мовчки обрізав файл, інакше дав би «перенесено» без половини сканів.
    """
    copied, done_bytes, _ = _mirror_verified(
        src, tree, dst, prune=prune, echo=echo, progress=progress, tick=tick,
    )
    return copied, done_bytes


# ── перевірки місця призначення ──────────────────────────────────────────────
def _free(path: Path) -> int:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return -1


def _inside(a: Path, b: Path) -> bool:
    try:
        Path(os.path.abspath(a)).relative_to(Path(os.path.abspath(b)))
        return True
    except ValueError:
        return False


def fs_type(path: Path) -> str:
    """Тип файлової системи під `path` (`exfat`, `msdos`, `apfs`…) або порожньо."""
    try:
        import psutil
    except ImportError:
        return ""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    where = os.path.abspath(probe)
    best, kind = "", ""
    try:
        parts = psutil.disk_partitions(all=True)
    except Exception:
        return ""
    for part in parts:
        mp = part.mountpoint
        if (where == mp or where.startswith(mp.rstrip("\\/") + os.sep)) and len(mp) > len(best):
            best, kind = mp, (part.fstype or "").lower()
    return kind


def win_bad_name(rel: str) -> bool:
    """Чи ім'я не ляже на Windows (і на диск, відформатований під Windows)."""
    for part in rel.split("/"):
        if _WIN_BAD_CHARS.search(part) or part.endswith((" ", ".")):
            return True
        if part.split(".")[0].lower() in _WIN_RESERVED:
            return True
    return False


# ── експорт ──────────────────────────────────────────────────────────────────
@dataclass
class Root:
    """Корінь справ із маркера та його доля в пакунку."""

    path: str
    #: Корінь у самому просторі (відносно Майстерні) — їде разом із ним.
    inside: str = ""
    #: Тека в пакунку (`roots/1-архів`), якщо корінь кладемо.
    slot: str = ""
    exists: bool = True
    bytes: int = 0


@dataclass
class ExportPlan:
    source: Path
    dest: Path
    name: str
    preset: str
    roots: list[Root] = field(default_factory=list)
    #: Частини теки даних, які кладемо: назва в пакунку → звідки.
    data: dict[str, Path] = field(default_factory=dict)
    #: Автопам'ять помічника: ключ у пакунку → тека на цій машині.
    memory: dict[str, Path] = field(default_factory=dict)
    engines: bool = False
    #: Тека вже містить пакунок цього ж простору — докопійовуємо змінене.
    refresh: bool = False
    ws_bytes: int = 0
    copy_bytes: int = 0
    free_bytes: int = -1
    fs: str = ""
    links: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    trees: dict[str, Tree] = field(default_factory=dict, repr=False)
    scope: str = "full"
    cases: list[str] = field(default_factory=list)
    slices: list[tuple[Path, str, str]] = field(default_factory=list, repr=False)


def _slot_name(i: int, path: str) -> str:
    leaf = re.split(r"[\\/]", path.rstrip("\\/"))[-1] or "root"
    leaf = _WIN_BAD_CHARS.sub("_", leaf).strip(" .") or "root"
    return f"{ROOTS_DIR}/{i}-{leaf}"


def _projects(hub: Path) -> list[Path]:
    from nyshporka.core.workspace import MARKER, PROJECTS_DIR

    base = hub / PROJECTS_DIR
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if (p / MARKER).is_file())


def _memory_dirs(hub: Path) -> dict[str, Path]:
    """Автопам'ять Claude Code для Майстерні й кожного проєкту — ті, що існують.

    Вона лежить у профілі (`~/.claude/projects/<шлях>/memory`), а не в просторі,
    і прив'язана до ШЛЯХУ теки. Без неї помічник на новій машині починав би
    знайомство з дослідженням наново.
    """
    from nyshporka.project import memory_dir

    out: dict[str, Path] = {}
    for key, root in ((HUB_MEMORY, hub),
                      *((p.relative_to(hub).as_posix(), p) for p in _projects(hub))):
        d = memory_dir(root)
        if d.is_dir() and any(d.iterdir()):
            out[key] = d
    return out


def _existing_bundle(dest: Path) -> dict[str, Any]:
    for name in (BUNDLE, PARTIAL):
        try:
            got = json.loads((dest / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(got, dict):
            return got
    return {}


def _resolve_case_for_export(hub: Path, spec: str) -> tuple[Path, list[Path], str]:
    """Знайти теку справи та її HTR-прогони для `--case`."""
    from nyshporka.cases import db
    from nyshporka.core.workspace import WorkspaceError

    raw = spec.strip()
    if not raw:
        raise WorkspaceError("порожній ідентифікатор справи для --case")
    # 1. Прямий шлях на диску.
    cand = Path(raw).expanduser()
    if not cand.is_absolute():
        cand = hub / cand
    if cand.is_dir():
        case_dir = Path(os.path.abspath(cand))
        runs = _runs_for_case_dir(hub, case_dir)
        return case_dir, runs, case_dir.name

    # 2. Пошук у реєстрі справ за ключем, шифрою або підрядком.
    try:
        rows = db.query(q=raw, limit=50)
    except Exception:
        rows = []
    exact = [r for r in rows if raw in (str(r.get("key") or ""), str(r.get("shifra") or ""))]
    picked = exact if exact else rows
    if not picked:
        raise WorkspaceError(f"справу «{spec}» не знайдено")
    if len(picked) > 1:
        opts = ", ".join(f"{r.get('shifra') or r.get('key')} ({r.get('key')})" for r in picked[:5])
        raise WorkspaceError(f"під «{spec}» підпадає кілька справ: {opts}")
    row = picked[0]
    p_str = str(row.get("path") or "")
    case_dir = Path(os.path.abspath(Path(p_str) if Path(p_str).is_absolute() else hub / p_str))
    if not case_dir.is_dir():
        raise WorkspaceError(f"тека справи «{spec}» відсутня: {case_dir}")
    runs = [hub / "reports" / "htr" / str(r) for r in (row.get("htr_runs") or [])
            if (hub / "reports" / "htr" / str(r)).is_dir()]
    if not runs:
        runs = _runs_for_case_dir(hub, case_dir)
    label = str(row.get("shifra") or row.get("key") or case_dir.name)
    return case_dir, runs, label


def _runs_for_case_dir(hub: Path, case_dir: Path) -> list[Path]:
    htr_root = hub / "reports" / "htr"
    if not htr_root.is_dir():
        return []
    norm = os.path.normcase(os.path.abspath(case_dir))
    out: list[Path] = []
    for d in sorted(htr_root.iterdir()):
        meta_file = d / "_htr_meta.json"
        if not d.is_dir() or not meta_file.is_file():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cdir = str(meta.get("case_dir") or "")
        if cdir and os.path.normcase(os.path.abspath(cdir)) == norm:
            out.append(d)
    return out


def plan_export(dest: str | Path, *, with_roots: bool = False, with_data: bool = False,
                with_memory: bool = True,
                cases: list[str] | None = None,
                echo: Callable[[str], None] | None = None,
                progress: Callable[[str], None] | None = None) -> ExportPlan:
    """Порахувати експорт. Нічого не пише."""
    from nyshporka.core import appdirs
    from nyshporka.core.lock import LockBusy, check_busy
    from nyshporka.core.workspace import workspace
    from nyshporka.setup.doctor import _ENGINE_VENV_NAMES
    from nyshporka.setup.wizard import _is_synced

    ws = workspace()
    hub = Path(os.path.abspath(ws.hub))
    to = Path(os.path.abspath(Path(dest).expanduser()))
    p = ExportPlan(source=hub, dest=to, name=ws.name, preset=ws.preset or "")
    # Імена тек рушіїв — з `doctor`, щоб вони жили в одному місці.
    p.engines = any((hub / n).is_dir() for n in _ENGINE_VENV_NAMES)

    if _inside(to, hub) or _inside(hub, to):
        p.problems.append("тека пакунка не може лежати в просторі чи містити його")
        return p
    if to.exists() and not to.is_dir():
        p.problems.append(f"{to} — файл, а потрібна тека")
        return p
    if to.is_dir() and any(to.iterdir()):
        prev = _existing_bundle(to)
        if (prev.get("source") or {}).get("workspace") == str(hub):
            p.refresh = True
        else:
            p.problems.append(f"{to} не порожня — потрібна нова чи порожня тека "
                              f"або пакунок цього ж простору")
    holder = check_busy(hub)
    if holder is not None:
        p.problems.append(f"{LockBusy(holder)} Закрийте вікно Нишпорки (і `nysh serve`) "
                          f"на час експорту.")
        return p
    if _is_synced(to):
        p.warnings.append("тека синхронізується з хмарою — пакунок поїде в мережу, "
                          "і копіювання буде повільним")

    big: list[str] = []
    bad_names: list[str] = []
    if cases:
        p.scope = "cases"
        seen_rel: set[str] = set()
        ext_idx = 0
        for spec in cases:
            case_dir, runs, label = _resolve_case_for_export(hub, spec)
            p.cases.append(label)
            if _inside(case_dir, hub):
                rel = case_dir.relative_to(hub).as_posix()
                dst_rel = f"{WS_DIR}/{rel}"
                if dst_rel not in seen_rel:
                    seen_rel.add(dst_rel)
                    p.slices.append((case_dir, dst_rel, f"справа ({label})"))
            else:
                ext_idx += 1
                slot = _slot_name(ext_idx, str(case_dir))
                r = Root(path=str(case_dir), slot=slot)
                p.roots.append(r)
                if slot not in seen_rel:
                    seen_rel.add(slot)
                    p.slices.append((case_dir, slot, f"корінь ({label})"))
            for rdir in runs:
                dst_rel = f"{WS_DIR}/reports/htr/{rdir.name}"
                if dst_rel not in seen_rel:
                    seen_rel.add(dst_rel)
                    p.slices.append((rdir, dst_rel, f"прогін ({rdir.name})"))
        ov_dir = hub / "data" / "cases"
        if (ov_dir / "overrides.json").is_file() and f"{WS_DIR}/data/cases" not in seen_rel:
            p.slices.append((ov_dir, f"{WS_DIR}/data/cases", "прив'язки справ"))
        for src_dir, dst_rel, label in p.slices:
            st = _scan_section(src_dir, label=label, echo=echo, progress=progress)
            p.trees[dst_rel] = st
            p.ws_bytes += st.bytes
            p.copy_bytes += to_copy(st, to / dst_rel)
            p.links += [f"{dst_rel}/{x}" for x in st.links]
            big += [f"{dst_rel}/{rel}" for rel, (s, _) in st.files.items() if s > _FAT_LIMIT]
            bad_names += [f"{dst_rel}/{rel}" for rel in st.files if win_bad_name(rel)]
    else:
        tree = _scan_section(hub, label=f"простір ({hub})", echo=echo, progress=progress,
                             top=_skip_top)
        p.trees[WS_DIR] = tree
        p.links = [f"{WS_DIR}/{x}" for x in tree.links]
        p.ws_bytes = tree.bytes
        p.copy_bytes = to_copy(tree, to / WS_DIR)
        big = [rel for rel, (s, _) in tree.files.items() if s > _FAT_LIMIT]
        bad_names = [rel for rel in tree.files if win_bad_name(rel)]

        i = 0
        for raw in ws.extra_case_roots:
            r = Root(path=str(raw))
            if _inside(Path(raw), hub):
                r.inside = Path(os.path.abspath(raw)).relative_to(hub).as_posix()
            elif not Path(raw).is_dir():
                r.exists = False
            elif with_roots:
                i += 1
                r.slot = _slot_name(i, str(raw))
                rt = _scan_section(Path(raw), label=f"корінь ({raw})", echo=echo, progress=progress)
                p.trees[r.slot] = rt
                r.bytes = rt.bytes
                p.copy_bytes += to_copy(rt, to / r.slot)
                p.links += [f"{r.slot}/{x}" for x in rt.links]
                big += [f"{raw}/{rel}" for rel, (s, _) in rt.files.items() if s > _FAT_LIMIT]
                bad_names += [f"{raw}/{rel}" for rel in rt.files if win_bad_name(rel)]
            p.roots.append(r)

        if with_data:
            for name, src in (("catalog", appdirs.data_dir() / "catalog"),
                              ("cache", appdirs.cache_dir())):
                if src.is_dir():
                    p.data[name] = src
                    dt = _scan_section(src, label=f"дані ({name})", echo=echo, progress=progress)
                    p.trees[f"{DATA_DIR}/{name}"] = dt
                    p.copy_bytes += to_copy(dt, to / DATA_DIR / name)
        if with_memory:
            p.memory = _memory_dirs(hub)
            for key, src in p.memory.items():
                mt = _scan_section(src, label=f"пам'ять ({key})", echo=echo, progress=progress)
                p.trees[f"{MEMORY_DIR}/{key}"] = mt
                p.copy_bytes += to_copy(mt, to / MEMORY_DIR / key)

    p.fs = fs_type(to)
    if big and p.fs in _FAT:
        p.problems.append(f"диск відформатовано у FAT32, а {len(big)} файл(ів) понад 4 ГБ "
                          f"(напр. {big[0]}) — відформатуйте диск в exFAT")
    if bad_names:
        sample = ", ".join(bad_names[:3])
        if p.fs in _WIN_NAMES_FS:
            p.problems.append(f"{len(bad_names)} імен не ляже на цей диск ({p.fs}): {sample} "
                              f"— перейменуйте їх")
        else:
            p.warnings.append(f"{len(bad_names)} імен не перенесуться на Windows: {sample}")
    p.free_bytes = _free(to)
    if 0 <= p.free_bytes < p.copy_bytes + SPARE:
        p.problems.append(f"на диску вільно {human(p.free_bytes)}, а треба близько "
                          f"{human(p.copy_bytes + SPARE)}")
    return p


def _write_json(path: Path, data: dict[str, Any]) -> None:
    from nyshporka.utils.atomic import atomic_write_text

    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                      newline="\n")


def run_export(p: ExportPlan, *, echo: Callable[[str], None] = print,
               progress: Callable[[str], None] | None = None) -> Path:
    """Покласти пакунок. Простір під замком: живий демон дописував би копію."""
    from nyshporka import __version__
    from nyshporka.core.lock import LockBusy, WorkspaceLock

    if p.problems:
        raise TransferError("\n".join(p.problems))
    try:
        lock = WorkspaceLock(p.source).acquire()
    except LockBusy as exc:
        raise TransferError(f"{exc} Закрийте вікно Нишпорки (і `nysh serve`) на час "
                            f"експорту.") from None
    to = p.dest
    to.mkdir(parents=True, exist_ok=True)
    source = {"workspace": str(p.source), "os": sys.platform}
    # Спершу прибираємо опис: доки експорт не дійшов до кінця, пакунок неповний.
    (to / BUNDLE).unlink(missing_ok=True)
    _write_json(to / PARTIAL, {"source": source})
    verified: dict[str, Tree] = {}
    files: dict[str, int] = {}
    if p.scope == "cases":
        with lock:
            for src_dir, dst_rel, label in p.slices:
                echo(label)
                st = p.trees.get(dst_rel) or scan(src_dir)
                _, _, verified[dst_rel] = _mirror_verified(
                    src_dir, st, to / dst_rel, prune=True,
                    echo=echo, progress=progress, tick=lock.beat,
                )
        for _src_dir, dst_rel, _label in p.slices:
            vt = verified.get(dst_rel) or scan(to / dst_rel)
            for rel, size in vt.sizes().items():
                files[f"{dst_rel}/{rel}"] = size
    else:
        with lock:
            echo("1. простір")
            ws_tree = p.trees.get(WS_DIR) or scan(p.source, top=_skip_top)
            _, _, verified[WS_DIR] = _mirror_verified(
                p.source, ws_tree, to / WS_DIR, prune=True,
                echo=echo, progress=progress, tick=lock.beat,
            )
        slots = {r.slot for r in p.roots if r.slot}
        if any(r.slot for r in p.roots):
            echo("2. корені справ поза простором")
        for r in p.roots:
            if r.slot:
                echo(f"  {r.path} → {r.slot}")
                rt = p.trees.get(r.slot) or scan(Path(r.path))
                _, _, verified[r.slot] = _mirror_verified(
                    Path(r.path), rt, to / r.slot, prune=True, echo=echo, progress=progress,
                )
        # Корінь, якого цього разу не клали, не має лишатись у пакунку зі старого разу.
        if (to / ROOTS_DIR).is_dir():
            for old in (to / ROOTS_DIR).iterdir():
                if f"{ROOTS_DIR}/{old.name}" not in slots and old.is_dir():
                    shutil.rmtree(old)
        if p.data:
            echo("3. довідники й ваги")
        for name, src in p.data.items():
            sec = f"{DATA_DIR}/{name}"
            dt = p.trees.get(sec) or scan(src)
            _, _, verified[sec] = _mirror_verified(
                src, dt, to / sec, prune=True, echo=echo, progress=progress,
            )
        if p.memory:
            echo("4. пам'ять помічника")
        for key, src in p.memory.items():
            sec = f"{MEMORY_DIR}/{key}"
            mt = p.trees.get(sec) or scan(src)
            _, _, verified[sec] = _mirror_verified(
                src, mt, to / sec, prune=True, echo=echo, progress=progress,
            )

        sections = [WS_DIR, *sorted(slots), *(f"{DATA_DIR}/{n}" for n in p.data),
                    *(f"{MEMORY_DIR}/{k}" for k in p.memory)]
        for sec in sections:
            vt = verified.get(sec) or scan(to / sec)
            for rel, size in vt.sizes().items():
                files[f"{sec}/{rel}"] = size
    meta = {
        "format": FORMAT, "nysh_version": __version__,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "scope": p.scope, "cases": p.cases,
        "source": source,
        "workspace": {"name": p.name, "preset": p.preset, "engines": p.engines},
        "roots": [{"path": r.path, "inside": r.inside, "slot": r.slot, "bytes": r.bytes}
                  for r in p.roots],
        "data": sorted(p.data), "memory": sorted(p.memory),
        "links": p.links, "files": files,
    }
    _write_json(to / BUNDLE, meta)
    (to / PARTIAL).unlink(missing_ok=True)
    return to / BUNDLE


# ── імпорт ───────────────────────────────────────────────────────────────────
@dataclass
class ImportPlan:
    bundle: Path
    target: Path
    roots_to: Path
    meta: dict[str, Any] = field(default_factory=dict)
    #: Ціль — порожній кістяк від `nysh init` (інсталятор): відсунемо його вбік.
    skeleton: bool = False
    #: Доливання змін у наявний простір (не затираючи решту).
    update: bool = False
    need_bytes: int = 0
    free_bytes: int = -1
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    trees: dict[str, Tree] = field(default_factory=dict, repr=False)

    @property
    def roots(self) -> list[dict[str, Any]]:
        return list(self.meta.get("roots") or [])


def read_bundle(bundle: Path) -> dict[str, Any]:
    """Опис пакунка або `TransferError`, якщо пакунок неповний чи чужий."""
    meta_path = bundle / BUNDLE
    if not meta_path.is_file():
        if (bundle / PARTIAL).is_file():
            raise TransferError(f"пакунок у {bundle} неповний: експорт не дійшов до кінця. "
                                f"Повторіть `nysh workspace export {bundle}` на старій машині")
        raise TransferError(f"у {bundle} немає {BUNDLE} — це не пакунок Нишпорки")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TransferError(f"{meta_path} не читається ({exc})") from None
    if not isinstance(meta, dict):
        raise TransferError(f"{meta_path}: несподіваний вміст")
    return meta


def _skeleton(target: Path) -> bool:
    """Чи `target` — щойно створений `nysh init` простір, у якому нічого немає.

    Інсталятор на новій машині сам кличе `nysh init`, тож типова тека майже
    завжди вже існує. Порожній кістяк (маркер, `.gitignore`, `.git` з одним
    комітом) відсуваємо вбік, а не відмовляємо: інакше людина мусила б руками
    видаляти теку, якої не створювала.
    """
    from nyshporka.core.lock import LOCK_NAME
    from nyshporka.core.workspace import MARKER

    tree = scan(target)
    return bool(tree.files) and all(
        rel in (MARKER, ".gitignore", LOCK_NAME) or rel.startswith(".git/")
        for rel in tree.files) and not tree.links


def plan_import(bundle: str | Path, target: str | Path | None = None,
                roots_to: str | Path | None = None,
                *, update: bool = False,
                echo: Callable[[str], None] | None = None,
                progress: Callable[[str], None] | None = None) -> ImportPlan:
    """Порахувати імпорт. Нічого не пише."""
    from nyshporka import __version__
    from nyshporka.core.lock import LockBusy, check_busy
    from nyshporka.core.workspace import WorkspaceError, validate_root, workspace
    from nyshporka.migrate import version_key
    from nyshporka.setup.wizard import _is_synced, default_root

    src = Path(os.path.abspath(Path(bundle).expanduser()))
    meta = read_bundle(src)
    is_update = bool(update or meta.get("scope") == "cases")
    source_ws = str((meta.get("source") or {}).get("workspace") or "")
    leaf = re.split(r"[\\/]", source_ws.rstrip("\\/"))[-1] if source_ws else ""
    if target:
        to = validate_root(Path(target))
    elif is_update:
        try:
            to = Path(os.path.abspath(workspace().hub))
        except WorkspaceError:
            to = validate_root(default_root().parent / (leaf or default_root().name))
    else:
        # Типово — поруч із документами, під іменем теки на старій машині.
        to = validate_root(default_root().parent / (leaf or default_root().name))
    rt = (Path(os.path.abspath(Path(roots_to).expanduser())) if roots_to
          else to.parent / f"{to.name}-справи")
    p = ImportPlan(bundle=src, target=to, roots_to=rt, meta=meta, update=is_update)

    if int(meta.get("format") or 0) > FORMAT:
        p.problems.append(f"пакунок нового формату ({meta.get('format')}) — оновіть "
                          f"Нишпорку: `nysh update`")
    theirs = str(meta.get("nysh_version") or "0")
    if version_key(theirs) > version_key(__version__):
        p.problems.append(f"пакунок зроблено Нишпоркою {theirs}, а тут {__version__} — "
                          f"спершу `nysh update`, інакше нова версія простору зустрінеться "
                          f"зі старою програмою")
    files: dict[str, int] = {k: int(v) for k, v in (meta.get("files") or {}).items()}
    have: dict[str, int] = {}
    for sec in sorted({k.split("/", 1)[0] for k in files}):
        tree = _scan_section(src / sec, label=f"пакунок ({sec})", echo=echo, progress=progress)
        p.trees[sec] = tree
        for rel, size in tree.sizes().items():
            have[f"{sec}/{rel}"] = size
    bad = diff(files, have)
    if bad:
        p.problems.append("пакунок не збігається зі своїм описом (диск пошкоджено чи "
                          "файли міняли руками):\n    " + "\n    ".join(bad[:10]))

    if _inside(to, src) or _inside(src, to):
        p.problems.append("простір не може лежати в пакунку чи містити його")
    elif to.exists() and not to.is_dir():
        p.problems.append(f"{to} — файл, а потрібна тека")
    elif is_update:
        if to.is_dir():
            holder = check_busy(to)
            if holder is not None:
                p.problems.append(f"{LockBusy(holder)} Закрийте вікно Нишпорки (і `nysh serve`) "
                                  f"на час оновлення.")
    elif to.is_dir() and any(to.iterdir()):
        if _skeleton(to):
            p.skeleton = True
            p.warnings.append(f"у {to} — порожній простір від встановлення; його буде "
                              f"відсунуто вбік, а не видалено")
        else:
            p.problems.append(f"{to} не порожня — назвіть нову теку: "
                              f"`nysh workspace import {src} <тека>` "
                              f"(або `--update`, щоб долити зміни)")
    if _is_synced(to):
        p.problems.append(f"{to} синхронізується з хмарою — оберіть теку поза нею")
    if sys.platform == "win32":
        names = [k for k in files if win_bad_name(k)]
        if names:
            p.problems.append(f"{len(names)} імен не ляже на Windows: {', '.join(names[:3])} "
                              f"— перейменуйте їх на старій машині й повторіть експорт")
    p.need_bytes = sum(max(v, 0) for v in files.values())
    p.free_bytes = _free(to)
    if 0 <= p.free_bytes < p.need_bytes + SPARE:
        p.problems.append(f"на диску вільно {human(p.free_bytes)}, а треба близько "
                          f"{human(p.need_bytes + SPARE)}")
    return p


def _quote_variants(raw: str) -> tuple[str, ...]:
    """Як шлях може стояти в TOML: як є чи з подвоєними зворотними скісними."""
    return (f'"{raw}"', f"'{raw}'", f'"{raw.replace(chr(92), chr(92) * 2)}"')


def rewrite_roots(marker: Path, moves: dict[str, Path]) -> list[str]:
    """Переписати корені справ у маркері: старий рядок → нове місце.

    Рядком, а не через розбір TOML: маркер людина правила руками, і коментарі
    в ньому мають лишитись. Повертає ті старі шляхи, які справді знайшлись.
    """
    try:
        text = marker.read_text(encoding="utf-8")
    except OSError:
        return []
    done = []
    for old, new in moves.items():
        for quoted in _quote_variants(old):
            if quoted in text:
                text = text.replace(quoted, json.dumps(new.as_posix(), ensure_ascii=False))
                done.append(old)
                break
    if done:
        marker.write_text(text, encoding="utf-8")
    return done


def _installed_python() -> str:
    """Інтерпретатор ВСТАНОВЛЕНОЇ Нишпорки, якщо команду запущено з теки з кодом.

    Хук git переживає цей процес: якщо записати в нього `sys.executable` клону,
    хук зламається, щойно теку з кодом приберуть. Тому шукаємо `nysh` у PATH
    поза власним середовищем і беремо інтерпретатор із його першого рядка
    (`#!…/python`). Не знайшли (Windows, де `nysh.exe` — не скрипт) — свій.
    """
    own = Path(sys.prefix).resolve()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        cand = Path(d) / "nysh"
        try:
            if not cand.is_file() or _inside(cand.resolve(), own):
                continue
            head = cand.read_bytes()[:1024].decode("utf-8", "replace")
        except OSError:
            continue
        if not head.startswith("#!"):
            continue
        # uv пише або `#!/шлях/python`, або — коли в шляху пробіл — обгортку
        # `#!/bin/sh` з рядком `'''exec' "/шлях/python" "$0" "$@"`.
        wrapped = re.search(r"exec' \"([^\"]+)\"", head)
        py = wrapped.group(1) if wrapped else head[2:].split("\n", 1)[0].strip().split(" ")[0]
        if py and Path(py).is_file():
            return Path(py).as_posix()
    return Path(sys.executable).as_posix()


def _fix_git_hook(hub: Path) -> str:
    """Перевстановити наш хук git з інтерпретатором цієї машини. Порожньо — не було."""
    from nyshporka.canon import hook as CH

    hook = hub / ".git" / "hooks" / "pre-commit"
    try:
        text = hook.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not any(m in text for m in CH.MARKERS):
        return ""
    py = _installed_python()
    hook.write_text(CH.HOOK.replace("{interpreter}", py), encoding="utf-8", newline="\n")
    with contextlib.suppress(OSError):
        hook.chmod(0o755)
    return py


def _fix_agent_files(hub: Path) -> list[str]:
    """Прибрати з тек помічника записи, що несуть шляхи старої машини.

    * `.claude/settings.local.json` — наш запобіжник скілів (інтерпретатор і
      копія скрипта старої машини). Чужі записи людини лишаються;
    * облік керованих файлів проєкту (`.nysh-project.json`) — позначаємо
      застарілим, і перший же запуск `nysh` у проєкті перепише хуки старту
      сесії та `autoMemoryDirectory` під нові шляхи (`project.sync`).
    """
    from nyshporka.project import LEDGER
    from nyshporka.skills import hook as SH

    out: list[str] = []
    for root in (hub, *_projects(hub)):
        local = root / ".claude" / "settings.local.json"
        if local.is_file():
            try:
                if SH.unhook(local):
                    out.append(local.relative_to(hub).as_posix())
            except ValueError:
                pass
        led = root / LEDGER
        try:
            data = json.loads(led.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data["version"] = "перенесено"
            _write_json(led, data)
    return out


@dataclass
class ImportReport:
    target: Path
    moved_aside: Path | None = None
    roots_moved: dict[str, Path] = field(default_factory=dict)
    roots_missing: list[str] = field(default_factory=list)
    data: dict[str, str] = field(default_factory=dict)
    memory: dict[str, str] = field(default_factory=dict)
    git_hook: str = ""
    agent_files: list[str] = field(default_factory=list)
    cases: str = ""
    todo: list[str] = field(default_factory=list)


def _empty(d: Path) -> bool:
    return not d.is_dir() or not any(d.iterdir())


def _subtree(tree: Tree, sub: str) -> Tree:
    prefix = f"{sub}/"
    cut = len(prefix)
    return Tree(
        dirs=[d[cut:] for d in tree.dirs if d.startswith(prefix)],
        files={k[cut:]: v for k, v in tree.files.items() if k.startswith(prefix)},
        links=[link[cut:] for link in tree.links if link.startswith(prefix)],
    )


def _merge_overrides(src_file: Path, dst_file: Path) -> None:
    if not src_file.is_file():
        return
    try:
        incoming = json.loads(src_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(incoming, dict):
        return
    existing: dict[str, Any] = {}
    if dst_file.is_file():
        try:
            loaded = json.loads(dst_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError):
            pass
    merged = {**existing, **incoming}
    dst_file.parent.mkdir(parents=True, exist_ok=True)
    _write_json(dst_file, merged)


def _skip_update_rel(rel: str, marker: str) -> bool:
    if rel == marker or rel.startswith(".venv") or rel == "data/cases/overrides.json":
        return True
    return rel.startswith("data/derived/") and ".sqlite" in rel


def run_import(p: ImportPlan, *, echo: Callable[[str], None] = print,
               progress: Callable[[str], None] | None = None,
               build_cases: bool = True) -> ImportReport:
    """Розкласти пакунок на цій машині. Пакунок не змінюється."""
    from nyshporka.core import appdirs
    from nyshporka.core.lock import LockBusy, WorkspaceLock
    from nyshporka.core.workspace import MARKER, add_case_root, remember, use
    from nyshporka.project import memory_dir

    if p.problems:
        raise TransferError("\n".join(p.problems))
    meta, src, to = p.meta, p.bundle, p.target
    rep = ImportReport(target=to)
    if p.skeleton:
        try:
            WorkspaceLock(to, steal_stale=True).acquire().release()
        except LockBusy as exc:
            raise TransferError(f"{exc} Закрийте вікно Нишпорки на час імпорту.") from None
        aside = to.with_name(f"{to.name}.порожній-{datetime.now():%Y%m%d-%H%M%S}")
        to.rename(aside)
        rep.moved_aside = aside
        echo(f"  порожній простір відсунуто: {aside}")

    def tree_of(section: str) -> Tree:
        if section in p.trees:
            return p.trees[section]
        if "/" in section:
            top, sub = section.split("/", 1)
            if top in p.trees:
                return _subtree(p.trees[top], sub)
        return scan(src / section)

    echo("1. простір")
    ws_tree = tree_of(WS_DIR)
    if p.update:
        ws_tree = Tree(
            dirs=ws_tree.dirs,
            files={k: v for k, v in ws_tree.files.items() if not _skip_update_rel(k, MARKER)},
            links=ws_tree.links,
        )
    mirror(src / WS_DIR, ws_tree, to, echo=echo, progress=progress)
    if p.update:
        _merge_overrides(src / WS_DIR / "data" / "cases" / "overrides.json",
                         to / "data" / "cases" / "overrides.json")

    moves: dict[str, Path] = {}
    for r in p.roots:
        old = str(r.get("path") or "")
        if r.get("inside"):
            moves[old] = to / str(r["inside"])
        elif r.get("slot"):
            slot = str(r["slot"])
            dest = p.roots_to / slot.split("/", 1)[1]
            if not rep.roots_moved:
                echo(f"2. корені справ → {p.roots_to}")
            mirror(src / slot, tree_of(slot), dest, echo=echo, progress=progress)
            moves[old] = dest
            rep.roots_moved[old] = dest
        elif old:
            rep.roots_missing.append(old)
    if not p.update:
        rewrite_roots(to / MARKER, moves)

    data_items = list(meta.get("data") or [])
    if data_items:
        echo("3. довідники й ваги")
    for name in data_items:
        dest = appdirs.data_dir() / "catalog" if name == "catalog" else appdirs.cache_dir()
        if not _empty(dest):
            rep.data[name] = f"лишено наявне: {dest}"
            continue
        mirror(src / DATA_DIR / name, tree_of(f"{DATA_DIR}/{name}"), dest,
               echo=echo, progress=progress)
        rep.data[name] = str(dest)
    mem_items = list(meta.get("memory") or [])
    if mem_items:
        echo("4. пам'ять помічника")
    for key in mem_items:
        root = to if key == HUB_MEMORY else to / key
        dest = memory_dir(root)
        if not _empty(dest):
            rep.memory[key] = f"лишено наявну: {dest}"
            continue
        mirror(src / MEMORY_DIR / key, tree_of(f"{MEMORY_DIR}/{key}"), dest,
               echo=echo, progress=progress)
        rep.memory[key] = str(dest)

    if not p.update:
        rep.git_hook = _fix_git_hook(to)
        rep.agent_files = _fix_agent_files(to)
    remember(use(to))
    if p.update:
        for dest in rep.roots_moved.values():
            with contextlib.suppress(Exception):
                add_case_root(dest)
    if build_cases:
        echo("5. реєстр справ")
        try:
            from nyshporka.cases import db
            from nyshporka.utils.plural import cases

            res = db.rebuild(rescan=True)
            rep.cases = f"реєстр: {cases(int(res.get('cases', 0)))}"
            if p.update:
                with contextlib.suppress(Exception):
                    from nyshporka.search import text as ST

                    ST.index()
        except Exception as exc:
            rep.cases = f"реєстр не зібрано ({exc}) — `nysh cases build`"
    rep.todo = [] if p.update else checklist(meta, rep)
    return rep


def checklist(meta: dict[str, Any], rep: ImportReport) -> list[str]:
    """Що лишилось зробити руками: те, що на іншу машину не переноситься в принципі."""
    ws = meta.get("workspace") or {}
    out: list[str] = []
    for old in rep.roots_missing:
        out.append(f"корінь справ {old} не перенесено: підключіть диск і "
                   f"`nysh roots add <нове місце>`, потім `nysh roots remove {old}`")
    if ws.get("engines"):
        out.append("рушії читання: `nysh htr install` (середовище не переноситься)")
        if "cache" not in (meta.get("data") or []):
            out.append("ваги моделей: `nysh models get`")
    if "catalog" not in (meta.get("data") or []):
        out.append("довідники: `nysh catalog update`")
    if rep.agent_files or (rep.target / ".claude").is_dir():
        out.append("помічник: `nysh skills install` і `nysh skills hook --install` "
                   "(у теці простору)")
    out.append("Супряга: `nysh share login` — ключ лежить у сховищі ключів старої машини")
    out.append("перевірка: `nysh doctor`")
    return out
