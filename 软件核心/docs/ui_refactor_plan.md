# UI Refactor Plan

## Restore Point

- Label: `V6.3_UI拆分前_20260626_当前可运行版`
- Path: `用户工作区/版本还原点/V6.3_UI拆分前_20260626_当前可运行版`
- Restore command: run `开发源码还原.ps1 -ConfirmRestore` inside the restore point folder.
- Scope: restores source files and `软件核心`; does not overwrite `用户工作区`, output files, materials, or `网盘资源包`.

## Goal

Keep `ui.py` as the main shell only:

- main window
- left navigation
- global theme
- module loading
- global log and feedback report

Move feature pages and workers into `软件核心/modules/*` so UI changes in one feature area do not affect unrelated feature areas.

## Migration Order

1. `modules/av_processing`
   - Move audio/video background workers first.
   - Then move AV processing pages and helper methods.
   - Risk: low to medium. It is mostly independent from batch mixing.
   - Status: completed. Worker code is in `modules/av_processing/workers.py`; page and helper methods are in `modules/av_processing/page_mixin.py`.

2. `modules/batch_mixer/subtitle`
   - Move smart subtitle form, preview canvas binding, and config serialization.
   - Risk: medium. It shares preview assets and font state.
   - Status: completed for UI split. Smart subtitle page and preview rendering are in `modules/batch_mixer/subtitle_mixin.py`; shared config serialization is in `modules/batch_mixer/config_mixin.py`.

3. `modules/batch_mixer/watermark`
   - Move text watermark form, table operations, preview rendering, and presets.
   - Risk: medium to high. This area has many UI state references.
   - Status: completed for UI split. Text watermark form, table operations, and preview rendering are in `modules/batch_mixer/watermark_mixin.py`; preset/config file read/write is in `modules/batch_mixer/config_mixin.py`.

4. `modules/batch_mixer/assets`
   - Move material directory scanning, table population, import, refresh, select all, and count labels.
   - Risk: medium. It affects NAS and recursive material selection.
   - Status: completed. File tab construction is in `modules/batch_mixer/file_tabs_mixin.py`; directory scanning, import, refresh, select-all, and count label logic are in `modules/batch_mixer/asset_mixin.py`.

4a. `modules/batch_mixer/support`
   - Move shared batch mixer support helpers without changing render behavior.
   - Status: completed. Font loading is in `font_mixin.py`; preview reference frame handling is in `preview_mixin.py`; mode switching is in `mode_mixin.py`; reusable tab/style helpers are in `ui_helpers_mixin.py`.

5. `modules/batch_mixer/workbench`
   - Move batch mixer page construction and task preflight.
   - Risk: high. Do this only after the sub-components above are isolated.
   - Status: completed for UI split. Batch mixer page and core parameter panel construction are in `modules/batch_mixer/workbench_mixin.py`; preflight, task startup, task progress, and mixer trace logging are in `modules/batch_mixer/task_mixin.py`.

6. `ui.py` cleanup
   - Remove unused imports.
   - Keep only module registration and global application lifecycle.
   - Status: in progress. `ui.py` has been reduced from 2184 lines to 801 lines while preserving the existing main-window shell.

## Rules

- One module per step.
- Run syntax/import validation after every step.
- Do not change mixed-cut rendering behavior during UI refactor steps.
- Do not build a customer update package unless explicitly requested.
- Keep every refactor compatible with the existing restore point.
