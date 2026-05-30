# Southside Publisher

A standalone publisher for the Southside pack repository.

It follows the same broad workflow as `kstext_publisher`, but with one hard rule:

- `packs/*.json` are treated as read-only source files
- publish metadata lives in `publisher_meta/`
- `index.json` is generated from source files plus sidecar metadata

## Files

- `sscfg_publisher.py`
  Main app with CLI and Tk GUI
- `publisher_meta/registry.json`
  Tracks `maxPackId` and `sourceFile -> id` bindings
- `publisher_meta/packs/<id>.json`
  Per-pack publish metadata sidecars

## GUI flow

1. Sync the cache repo
2. Scan the repo
3. Select an unpublished source file and click `Publish Selected`
4. Edit `name`, `author`, `summary`, `version`, `date`, `southsideVersion`
5. Rebuild `index.json`
6. Commit and push

## CLI

Preview:

```powershell
python sscfg_publisher.py --repo E:\DESKTOP\project\SouthsideRepo --dry-run
```

Write `index.json`:

```powershell
python sscfg_publisher.py --repo E:\DESKTOP\project\SouthsideRepo --write-index
```

Commit and push:

```powershell
python sscfg_publisher.py --repo E:\DESKTOP\project\SouthsideRepo --commit --push
```
