@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM Cloudflare Quick Tunnel: aliases http://localhost:8770 to a public
REM https://XXX.trycloudflare.com URL you can bookmark on your phone.
REM
REM 前提: cloudflared がインストール済み。未インストールなら:
REM   winget install --id Cloudflare.cloudflared
REM または https://github.com/cloudflare/cloudflared/releases から入手。
REM
REM 使い方:
REM   1) 先に start.bat で server.py を起動
REM   2) このバッチをダブルクリック
REM   3) "Your quick Tunnel has been created!" の下に出てくる
REM      https://xxxxx.trycloudflare.com/ をスマホでブックマーク
REM
REM セキュリティ: config.yaml の server.auth.password を必ず設定すること
REM (localhost からのアクセスは常に認証不要、Tunnel 経由のみ Basic Auth)

where cloudflared >nul 2>&1
if errorlevel 1 (
  echo.
  echo [!] cloudflared が見つかりません。
  echo     winget install --id Cloudflare.cloudflared
  echo     または https://github.com/cloudflare/cloudflared/releases
  echo.
  pause
  exit /b 1
)

echo Cloudflare Tunnel を起動します...
echo （表示される https://xxx.trycloudflare.com URL をスマホでブックマーク）
echo.
cloudflared tunnel --url http://localhost:8770
pause
