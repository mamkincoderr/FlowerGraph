@echo off
setlocal
chcp 65001 > nul

echo =========================================
echo  FlowerGraph — сборка инсталлятора
echo =========================================
echo.

cd /d "%~dp0.."

REM --- 1. Проверяем виртуальное окружение ---
if not exist ".venv\Scripts\python.exe" (
    echo ОШИБКА: виртуальное окружение не найдено.
    echo Запустите setup_venv.bat из папки FlowerGraph.
    pause & exit /b 1
)

REM --- 2. PyInstaller ---
echo [1/3] Сборка exe через PyInstaller...
.venv\Scripts\pyinstaller FlowerGraph.spec --clean --noconfirm
if errorlevel 1 (
    echo ОШИБКА: PyInstaller завершился с ошибкой.
    pause & exit /b 1
)
echo     OK — dist\FlowerGraph\

REM --- 3. Inno Setup ---
set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="C:\Program Files\Inno Setup 6\ISCC.exe"

if not exist %ISCC% (
    echo.
    echo [2/3] Inno Setup не найден — пропускаем создание установщика.
    echo       Portable-версия готова в папке dist\FlowerGraph\
    goto :portable
)

echo [2/3] Создание инсталлятора через Inno Setup...
%ISCC% install\flowergraph_setup.iss
if errorlevel 1 (
    echo ОШИБКА: Inno Setup завершился с ошибкой.
    pause & exit /b 1
)
echo     OK — ..\install\FlowerGraph_Setup_0.6.3.exe

:portable
REM --- 4. Portable ZIP (в папку ..\PGN\install) ---
echo [3/3] Создание portable ZIP...
.venv\Scripts\python.exe -c "
import zipfile, os, pathlib
src = pathlib.Path('dist/FlowerGraph')
out = pathlib.Path('../install/FlowerGraph_0.6.3_portable.zip')
out.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for f in src.rglob('*'):
        z.write(f, pathlib.Path('FlowerGraph') / f.relative_to(src))
print(f'ZIP: {out}  ({out.stat().st_size//1024//1024} MB)')
"
echo.
echo =========================================
echo  Готово!
echo    Инсталлятор: ..\install\FlowerGraph_Setup_0.6.3.exe
echo    Portable ZIP: ..\install\FlowerGraph_0.6.3_portable.zip
echo =========================================
pause
