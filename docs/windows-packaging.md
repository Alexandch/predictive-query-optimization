# Сборка и установка в Windows

Проект собирается в автономное 64-битное Windows-приложение с помощью
PyInstaller, а затем упаковывается в установщик Inno Setup. В дистрибутив входят
интерпретатор Python, PyQt6, CPU-версия PyTorch, XGBoost, выбранные модели и
исходные датасеты для повторного обучения.

## Готовое приложение

Локальный установщик создаётся по пути:

`dist\installer\PredictiveQueryOptimization-Setup-0.1.1.exe`

Стандартный каталог установки:

`%LOCALAPPDATA%\Programs\PredictiveQueryOptimization`

Установщик создаёт ярлыки в меню «Пуск» и, если выбран соответствующий пункт,
на рабочем столе. Удалить приложение можно штатно через параметры Windows или
файл `unins000.exe` в каталоге установки.

Само приложение автономно, но для анализа SQL ему нужен доступный PostgreSQL.
Учебную БД проекта можно запустить командой:

```powershell
docker compose up -d --wait
```

## Воспроизводимая сборка

Требуются Windows 10/11, Python с созданным `.venv` и Inno Setup 6. Полная
команда:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

Скрипт устанавливает группы зависимостей `desktop` и `build`, генерирует
иконку, собирает каталог `dist\PredictiveQueryOptimization`, запускает
smoke-тест и формирует установщик. Smoke-тест проверяет не только запуск Qt, но
и реальную десериализацию артефактов XGBoost и DQN. Его журнал находится в
`%LOCALAPPDATA%\PredictiveQueryOptimization\artifacts\smoke-test.log`.

Для повторной сборки без установки зависимостей:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -SkipDependencyInstall
```

Чтобы получить только каталог приложения без установщика, добавьте
`-SkipInstaller`. Скрипт в конце печатает SHA-256 собранных исполняемых файлов.

Пользовательские результаты обучения и экспорта сохраняются отдельно от
неизменяемых ресурсов приложения:

`%LOCALAPPDATA%\PredictiveQueryOptimization\artifacts`
