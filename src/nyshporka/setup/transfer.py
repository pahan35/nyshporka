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
from collections.abc import Callable
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
         skip: Callable[[str], bool] | None = None) -> Tree:
    """Обійти `root`, не заходячи в посилання.

    `top` відсіює імена лише в корені, `skip` — відносні шляхи будь-де.
    """
    out = Tree()
    if not root.is_dir():
        return out
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
            except OSError:
                out.files[rel] = (-1, 0.0)
    return out


def _same(dst: Path, size: int, mtime: float) -> bool:
    try:
        st = dst.stat()
    except OSError:
        return False
    return st.st_size == size and abs(st.st_mtime - mtime) <= _MTIME_SLACK


def to_copy(tree: Tree, dst: Path) -> int:
    """Скільки байтів ще треба покласти в `dst` (решта там уже така сама)."""
    return sum(max(s, 0) for rel, (s, m) in tree.files.items() if not _same(dst / rel, s, m))


def mirror(src: Path, tree: Tree, dst: Path, *, prune: bool = False,
           echo: Callable[[str], None] = print,
           tick: Callable[[], None] | None = None) -> tuple[int, int]:
    """Покласти `tree` із `src` у `dst`: лише нове й змінене, потім звірити.

    `prune` прибирає з `dst` файли, яких у джерелі вже немає: інакше видалене
    на старій машині воскресало б на новій. Ставиться лише для тек пакунка —
    вони наші.

    🔴 Звірка пофайлова (шлях + розмір), і розбіжність — відмова: диск, який
    мовчки обрізав файл, інакше дав би «перенесено» без половини сканів.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for rel in tree.dirs:
        (dst / rel).mkdir(parents=True, exist_ok=True)
    copied = done_bytes = 0
    total = to_copy(tree, dst)
    step = max(total // 10, 256 * 1024 ** 2)
    shown = 0
    for rel, (size, mtime) in tree.files.items():
        target = dst / rel
        if _same(target, size, mtime):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src / rel, target)
        except OSError as exc:
            raise TransferError(f"не вдалось скопіювати {src / rel} → {target}: {exc}") from None
        copied += 1
        done_bytes += max(size, 0)
        if done_bytes - shown >= step:
            shown = done_bytes
            echo(f"    … {human(done_bytes)} з {human(total)}")
            if tick is not None:
                tick()
    if prune:
        extra = scan(dst)
        for rel in extra.files:
            if rel not in tree.files:
                (dst / rel).unlink(missing_ok=True)
    bad = diff(tree.sizes(), scan(dst).sizes())
    if bad:
        raise TransferError("копія не зійшлась з оригіналом:\n  " + "\n  ".join(bad[:20]))
    same = len(tree.files) - copied
    echo(f"  ✓ звірено {len(tree.files)} файлів: скопійовано {copied} ({human(done_bytes)})"
         + (f", {same} вже були такі самі" if same else ""))
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


def plan_export(dest: str | Path, *, with_roots: bool = False, with_data: bool = False,
                with_memory: bool = True) -> ExportPlan:
    """Порахувати експорт. Нічого не пише."""
    from nyshporka.core import appdirs
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
    if _is_synced(to):
        p.warnings.append("тека синхронізується з хмарою — пакунок поїде в мережу, "
                          "і копіювання буде повільним")

    tree = scan(hub, top=_skip_top)
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
            rt = scan(Path(raw))
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
                p.copy_bytes += to_copy(scan(src), to / DATA_DIR / name)
    if with_memory:
        p.memory = _memory_dirs(hub)
        for key, src in p.memory.items():
            p.copy_bytes += to_copy(scan(src), to / MEMORY_DIR / key)

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


def run_export(p: ExportPlan, *, echo: Callable[[str], None] = print) -> Path:
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
    with lock:
        echo("1. простір")
        mirror(p.source, scan(p.source, top=_skip_top), to / WS_DIR, prune=True,
               echo=echo, tick=lock.beat)
    slots = {r.slot for r in p.roots if r.slot}
    if any(r.slot for r in p.roots):
        echo("2. корені справ поза простором")
    for r in p.roots:
        if r.slot:
            echo(f"  {r.path} → {r.slot}")
            mirror(Path(r.path), scan(Path(r.path)), to / r.slot, prune=True, echo=echo)
    # Корінь, якого цього разу не клали, не має лишатись у пакунку зі старого разу.
    if (to / ROOTS_DIR).is_dir():
        for old in (to / ROOTS_DIR).iterdir():
            if f"{ROOTS_DIR}/{old.name}" not in slots and old.is_dir():
                shutil.rmtree(old)
    if p.data:
        echo("3. довідники й ваги")
    for name, src in p.data.items():
        mirror(src, scan(src), to / DATA_DIR / name, prune=True, echo=echo)
    if p.memory:
        echo("4. пам'ять помічника")
    for key, src in p.memory.items():
        mirror(src, scan(src), to / MEMORY_DIR / key, prune=True, echo=echo)

    sections = [WS_DIR, *sorted(slots), *(f"{DATA_DIR}/{n}" for n in p.data),
                *(f"{MEMORY_DIR}/{k}" for k in p.memory)]
    files: dict[str, int] = {}
    for sec in sections:
        for rel, size in scan(to / sec).sizes().items():
            files[f"{sec}/{rel}"] = size
    meta = {
        "format": FORMAT, "nysh_version": __version__,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
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
    need_bytes: int = 0
    free_bytes: int = -1
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

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
                roots_to: str | Path | None = None) -> ImportPlan:
    """Порахувати імпорт. Нічого не пише."""
    from nyshporka import __version__
    from nyshporka.core.workspace import validate_root
    from nyshporka.migrate import version_key
    from nyshporka.setup.wizard import _is_synced, default_root

    src = Path(os.path.abspath(Path(bundle).expanduser()))
    meta = read_bundle(src)
    source_ws = str((meta.get("source") or {}).get("workspace") or "")
    leaf = re.split(r"[\\/]", source_ws.rstrip("\\/"))[-1] if source_ws else ""
    # Типово — поруч із документами, під іменем теки на старій машині.
    to = validate_root(Path(target) if target
                       else default_root().parent / (leaf or default_root().name))
    rt = (Path(os.path.abspath(Path(roots_to).expanduser())) if roots_to
          else to.parent / f"{to.name}-справи")
    p = ImportPlan(bundle=src, target=to, roots_to=rt, meta=meta)

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
    for sec in {k.split("/", 1)[0] for k in files}:
        for rel, size in scan(src / sec).sizes().items():
            have[f"{sec}/{rel}"] = size
    bad = diff(files, have)
    if bad:
        p.problems.append("пакунок не збігається зі своїм описом (диск пошкоджено чи "
                          "файли міняли руками):\n    " + "\n    ".join(bad[:10]))

    if _inside(to, src) or _inside(src, to):
        p.problems.append("простір не може лежати в пакунку чи містити його")
    elif to.exists() and not to.is_dir():
        p.problems.append(f"{to} — файл, а потрібна тека")
    elif to.is_dir() and any(to.iterdir()):
        if _skeleton(to):
            p.skeleton = True
            p.warnings.append(f"у {to} — порожній простір від встановлення; його буде "
                              f"відсунуто вбік, а не видалено")
        else:
            p.problems.append(f"{to} не порожня — назвіть нову теку: "
                              f"`nysh workspace import {src} <тека>`")
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


def run_import(p: ImportPlan, *, echo: Callable[[str], None] = print,
               build_cases: bool = True) -> ImportReport:
    """Розкласти пакунок на цій машині. Пакунок не змінюється."""
    from nyshporka.core import appdirs
    from nyshporka.core.lock import LockBusy, WorkspaceLock
    from nyshporka.core.workspace import MARKER, remember, use
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
        return scan(src / section)

    echo("1. простір")
    mirror(src / WS_DIR, tree_of(WS_DIR), to, echo=echo)

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
            mirror(src / slot, tree_of(slot), dest, echo=echo)
            moves[old] = dest
            rep.roots_moved[old] = dest
        elif old:
            rep.roots_missing.append(old)
    rewrite_roots(to / MARKER, moves)

    for name in meta.get("data") or []:
        dest = appdirs.data_dir() / "catalog" if name == "catalog" else appdirs.cache_dir()
        if not _empty(dest):
            rep.data[name] = f"лишено наявне: {dest}"
            continue
        mirror(src / DATA_DIR / name, tree_of(f"{DATA_DIR}/{name}"), dest, echo=echo)
        rep.data[name] = str(dest)
    for key in meta.get("memory") or []:
        root = to if key == HUB_MEMORY else to / key
        dest = memory_dir(root)
        if not _empty(dest):
            rep.memory[key] = f"лишено наявну: {dest}"
            continue
        mirror(src / MEMORY_DIR / key, tree_of(f"{MEMORY_DIR}/{key}"), dest, echo=echo)
        rep.memory[key] = str(dest)

    rep.git_hook = _fix_git_hook(to)
    rep.agent_files = _fix_agent_files(to)
    remember(use(to))
    if build_cases:
        try:
            from nyshporka.cases import db
            from nyshporka.utils.plural import cases

            res = db.rebuild(rescan=True)
            rep.cases = f"реєстр: {cases(int(res.get('cases', 0)))}"
        except Exception as exc:
            rep.cases = f"реєстр не зібрано ({exc}) — `nysh cases build`"
    rep.todo = checklist(meta, rep)
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
