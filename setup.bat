@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo  Paper Collector セットアップ
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python が見つかりません。Python 3.10 以上を入れてください。
    pause
    exit /b 1
)

echo [1/4] 依存ライブラリをインストール...
python -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo   インストールに失敗しました。
    pause
    exit /b 1
)
echo   OK
echo.

echo [2/4] 朝6時自動更新タスクを登録 (Task Scheduler)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_schedule.ps1"
if errorlevel 1 (
    echo   タスク登録に失敗しました。手動で Task Scheduler から登録するか、PowerShell を管理者で起動してから install_schedule.ps1 を実行してください。
    echo   ※ 続行します。
)
echo.

echo [3/4] 初回論文取得 (cleanup + fetch + render)...
python auto_update.py
echo.

echo [4/4] サーバーを起動します (この窓を閉じるとサーバーが止まります)...
echo.
echo ============================================================
echo  ブラウザに  http://localhost:8770/  が開きます
echo  ★ 必ずこの URL をブックマークしてください
echo    file:// で開かれた古いブックマークは削除推奨
echo ============================================================
echo.

python server.py
pause
