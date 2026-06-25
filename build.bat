@echo off
echo ====================================
echo  Сборка Zapret Manager в .exe
echo ====================================
echo.

REM Проверяем Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Python не найден! Установите Python 3.10+ с python.org
    pause
    exit /b 1
)

echo [1/3] Устанавливаем зависимости...
pip install customtkinter requests pyinstaller --quiet
if errorlevel 1 (
    echo [ОШИБКА] Не удалось установить зависимости
    pause
    exit /b 1
)

echo [2/3] Собираем exe...
pyinstaller zapret_gui.spec --clean --noconfirm

if errorlevel 1 (
    echo.
    echo [ОШИБКА] Сборка не удалась. Попробуйте команду вручную:
    echo pyinstaller --onefile --windowed --uac-admin --name ZapretManager zapret_gui.py
    pause
    exit /b 1
)

echo.
echo [3/3] Готово!
echo.
echo Файл: dist\ZapretManager.exe
echo.
echo ВАЖНО: скопируйте ZapretManager.exe в папку zapret
echo рядом с bin\ и .bat файлами, затем запускайте оттуда.
echo.
pause
