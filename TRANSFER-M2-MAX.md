# Move the research from the Intel laptop to the M2 Max and read with MPS

Local notes for the branch `local/mps-transfer`. It is never opened as a PR: it
only combines two branches so one clone does everything:

- `feature/mps-accel` — reading on the Apple GPU (MPS),
  [SERGIUSH-UA/nyshporka#31](https://github.com/SERGIUSH-UA/nyshporka/pull/31);
- `feature/workspace-transfer` — `nysh workspace export` / `import`,
  [SERGIUSH-UA/nyshporka#30](https://github.com/SERGIUSH-UA/nyshporka/issues/30).

Both machines run Nyshporka from a clone of this branch with `uv run`. The
Intel laptop's installed `nysh` is never modified.

## 0. On the M5 Pro: publish the branch

The Intel laptop and the M2 Max clone it from the fork:

```sh
cd ~/PycharmProjects/nyshporka
git push -u origin local/mps-transfer
```

When either feature branch gets new commits, merge it into this branch
(`git merge feature/mps-accel`, `git merge feature/workspace-transfer`) and push
again. Then on the other machines: `git pull`.

## 1. Intel laptop: export to the external drive

1. **Prepare an APFS destination (without wiping an exFAT drive).** Both machines
   are Macs, so APFS keeps every file name and timestamp and handles hundreds of
   thousands of small HTR files much faster than exFAT. Check the drive format:

   ```sh
   diskutil info "/Volumes/<drive>" | grep Personality
   ```

   - If it is already **APFS**, export directly to `/Volumes/<drive>/Нишпорка-перенос`.
   - If it is **exFAT** with existing data you don't want to format, create an
     APFS `.sparsebundle` on the drive (existing files stay untouched, and the
     bundle only uses space for what is written):

     ```sh
     hdiutil create -size 50g -type SPARSEBUNDLE -fs APFS -volname "NyshTransfer" \
         "/Volumes/<drive>/NyshTransfer.sparsebundle"
     hdiutil attach "/Volumes/<drive>/NyshTransfer.sparsebundle"
     ```

     Then export to `/Volumes/NyshTransfer/Нишпорка-перенос`.

2. **Close Nyshporka.** Quit the browser window, and stop `nysh serve` if it is
   running in a terminal (`pgrep -fl "nysh serve"` shows it). The export
   refuses to run while the workspace is open.

3. **Find the tools and the workspace path.** The installer puts `uv` in its
   own folder, which is often not on PATH:

   ```sh
   UV="$(command -v uv || echo "$HOME/.local/share/nyshporka/uv/uv")"; "$UV" --version
   nysh info        # note the workspace path it prints
   ```

4. **Get the code.** If `git` is missing, `xcode-select --install` installs it.

   ```sh
   git clone -b local/mps-transfer https://github.com/pahan35/nyshporka.git ~/nysh-transfer
   cd ~/nysh-transfer
   ```

5. **Dry run.** The workspace path is passed explicitly so the export can't
   pick the wrong one:

   ```sh
   "$UV" run nysh --workspace "<workspace path>" workspace export \
       "/Volumes/NyshTransfer/Нишпорка-перенос" --with-roots --with-data --dry-run
   ```

   Check in the output:

   - the `простір:` line shows the right path;
   - every case-folder location outside the workspace is listed with `→ roots/…`;
   - the `дані:` lines show `catalog` and `cache` (the model weights);
   - there are no `✗` lines.

6. **Export.** Run the same command without `--dry-run`. It ends with
   `✅ пакунок готовий`. If it gets interrupted, run it again: it copies only
   what is missing, and an unfinished bundle can't be imported by mistake.

7. **Detach the sparsebundle (if used) and eject the drive:**

   ```sh
   hdiutil detach /Volumes/NyshTransfer   # only if using NyshTransfer.sparsebundle
   diskutil eject "/Volumes/<drive>"
   ```

## 2. M2 Max: set up the environment

```sh
xcode-select --install                               # only if git is missing
curl -LsSf https://astral.sh/uv/install.sh | sh      # only if uv is missing; open a new terminal afterwards
git clone -b local/mps-transfer https://github.com/pahan35/nyshporka.git ~/nyshporka
cd ~/nyshporka
uv sync --extra all                                  # no torch here; the engines get their own environment in step 4
export NYSH_CI=1                                     # developer machine: keeps model downloads out of the download stats
```

The Nyshporka installer is not needed on the M2 Max. If it was already run,
the import moves its empty workspace aside instead of failing.

## 3. M2 Max: import from the drive

If you used a sparsebundle, attach it first (`hdiutil attach "/Volumes/<drive>/NyshTransfer.sparsebundle"`), then import:

```sh
uv run nysh workspace import "/Volumes/NyshTransfer/Нишпорка-перенос" --dry-run
uv run nysh workspace import "/Volumes/NyshTransfer/Нишпорка-перенос"
```

- **Where things land:** the workspace goes to `~/Documents/<old folder name>`,
  case folders from outside the workspace go to `~/Documents/<name>-справи/`.
  For another place, add a target path after the bundle path.
- **What the import does:** checks the bundle against its file list, rewrites
  the case-folder paths, points the git history hook at this clone's Python,
  rebuilds the case registry and prints what is left to do by hand.
- **Keep the drive as it is.** Import doesn't modify the bundle, so it stays
  the copy of the Intel results to compare against.
- **Keep this clone.** The git history hook runs this clone's `.venv` Python;
  deleting the clone breaks history commits until `nysh canon hook --install`
  is run from another installation.

## 4. M2 Max: build the reading engines

```sh
uv run nysh htr install       # engine environment with arm64 torch from PyPI
uv run nysh doctor            # expect: «Прискорення (GPU): карта Apple (MPS) · torch 2.14.x»
```

If torch is not 2.14, `auto` stays on the CPU; `export NYSH_HTR_DEVICE=mps`
uses the GPU anyway.

## 5. M2 Max: confirm MPS gives the same text as the CPU

PR #31 proved this only on the M5 Pro, so check it once on the M2 Max:

```sh
uv run nysh models list
PY="$HOME/Documents/<name>/.venv_htr/bin/python"
uv run python -m nyshporka.htr.patches.gpu_sato_verify mps
uv run python -m nyshporka.htr.patches.kraken_lineage_verify --py-a "$PY" --py-b "$PY" \
    --device cpu --device-b mps --frames "<folder with ~8 scans>" --model "<model>" \
    [--models "<voices>"] --out /tmp/verify-m2max
```

Segmentation, line boxes and texts must all match with 0 differences. A
mismatch means MPS on the M2 Max is not trusted yet: report it and read with
`NYSH_HTR_DEVICE=cpu` meanwhile.

## 6. M2 Max: read

```sh
uv run nysh read "<case folder>"               # MPS chosen automatically
uv run nysh read "<case folder>" --workers 2   # shards sharing the Apple GPU
```

Pages already read with the same model are skipped; `--rerun` reads only the
pages that have no text yet.

## 7. Optional: compare speed with the M5 Pro

Run the same loop on both machines with the same frames, models and torch.
The fresh `--out` and `--seg-cache` folders keep the real results untouched and
force segmentation to run instead of reusing the cache:

```sh
for w in 1 2 3; do
  /usr/bin/time -p uv run nysh read "<case>" --limit 16 --workers $w \
    --out /tmp/bench-w$w --seg-cache /tmp/seg-empty-w$w
done
```

Expected: the M5 Pro wins with one worker (the CPU side limits a single
process); with 2–3 workers the 38-core M2 Max may catch up. Not measured yet.

## 8. Open the workspace-transfer PR (from any machine)

`PR-workspace-transfer.md` next to this file is the drafted PR description
for `feature/workspace-transfer` (Ukrainian, like the rest of the project).
Before opening it, update its «Що не перевірено» section with the real
Intel → M2 Max result.

```sh
git fetch -q origin && git fetch -q upstream
git switch feature/workspace-transfer && git pull
git merge upstream/main                      # merge, never rebase: the branch is pushed
uv run python tools/scan_private.py --history
git push origin feature/workspace-transfer
gh pr create --repo SERGIUSH-UA/nyshporka --base main \
    --head pahan35:feature/workspace-transfer \
    --title "Перенести простір на інший комп'ютер: nysh workspace export / import" \
    --body-file <path to PR-workspace-transfer.md>
```

## If something goes wrong

- **Not enough free space:** the dry run refuses and says how much is needed.
- **The import lists a case folder as not transferred:** its disk wasn't
  readable on the Intel laptop during export. Plug that disk into the M2 Max
  and run `uv run nysh roots add <path>`.
