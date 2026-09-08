# Резервное копирование и восстановление

Резервная копия PQO содержит:

- PostgreSQL custom-format dump всей базы `query_optimizer`, включая историю в
  схеме `pqo`;
- пользовательские результаты установленного приложения из
  `%LOCALAPPDATA%\PredictiveQueryOptimization\artifacts`;
- экспериментальные результаты из каталога `artifacts` проекта;
- настройки приложения из реестра Windows, если они существуют;
- `manifest.json` с размером и SHA-256 каждого файла.

Снимок сначала создаётся в каталоге `.partial-*` и получает окончательное имя
только после успешного завершения. По умолчанию используются
`%OneDrive%\PQO Backups`, если OneDrive доступен, или каталог `PQO Backups`
рядом с проектом. Путь можно переопределить параметром `-BackupRoot` либо
переменной `PQO_BACKUP_ROOT`.

Незавершённые `.partial-*` каталоги не участвуют в восстановлении и
автоматически удаляются, если им больше суток.

Файл `last-backup-status.json` в корне хранилища показывает результат и время
последнего фонового запуска. Файл `.env` намеренно не копируется: резервные
копии не шифруются, поэтому пароль PostgreSQL не должен попадать в них.

## Первая копия

Docker Desktop и контейнер PostgreSQL должны быть запущены. Выполнить резервное
копирование вручную:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\backup_windows.ps1
```

По умолчанию сохраняется минимум 7 последних снимков. Более старые копии
удаляются после 30 дней. Настройки меняются параметрами `-MinimumCopies` и
`-RetentionDays`.

## Ежедневная задача Windows

Установить ежедневный запуск в 20:00:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_backup_task.ps1
```

Выбрать другое время или каталог:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_backup_task.ps1 `
  -DailyAt "22:30" -BackupRoot "E:\PQO Backups"
```

Задача выполняется от текущего пользователя, запускается при первой
возможности после пропущенного времени и требует работающий Docker Engine.
Удалить её можно параметром `-Remove`.

## Восстановление

Безопасная проверка восстановления создаёт отдельную БД
`query_optimizer_restored` и не затрагивает рабочую:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\restore_windows.ps1
```

Чтобы вернуть основную БД после чистой переустановки Windows и одновременно
восстановить артефакты и настройки:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\restore_windows.ps1 `
  -DatabaseName query_optimizer -ReplaceDatabase `
  -RestoreArtifacts -RestoreSettings
```

Перед изменением PostgreSQL сценарий проверяет наличие всех файлов и их
SHA-256. Существующая база никогда не удаляется без явного
`-ReplaceDatabase`. Артефакты при восстановлении объединяются с существующим
каталогом; файлы с одинаковыми именами заменяются, остальные не удаляются.
