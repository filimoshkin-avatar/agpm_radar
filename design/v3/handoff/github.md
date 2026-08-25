repo: filimoshkin-avatar/agpm_radar
branch: main

## Last sync
date: 2026-08-25T02:47:00Z

### Updated in this project
- Перечитан v2/apps/web/index.html: перенесены все фичи режима «Радар» в редизайн v3
- Виджет по периоду (сонар/доли/кольцо 30 дней), разрез по дням, календарь-heatmap, хронология, рубрикатор, аналитический разбор
- Анимации виджетов взяты из эталона radar_widgets_export (rdSweep/rdBlip/rdArc, sync-задержки)

### Обновлено 2026-08-25 (газета)
- Прочитан v2/apps/web/gazette-20260803.html — перенесён в оболочку «Газета» v3
- Оболочка: селектор выпуска = хронологический архив (сейчас 1 запись «Август 2026 · № 1»), кнопки Печать + Сохранить PDF (window.print), print-CSS изолирует только газету (#gzPaper) на A4

## Screen map
| Screen | Repo files |
|---|---|
| Радар (лента выпусков) | v2/apps/web/index.html, v2/apps/web/styles.css |
| Агент (база знаний, 8 вкладок) | v2/apps/web/index.html (#agentView), docs/radar-agent-mode-START-HERE.md, design/agent-mode/Main.dc.html |
| Газета | v2/apps/web/gazette-20260803.html (iframe-оболочка) |
