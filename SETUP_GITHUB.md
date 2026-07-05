# GitHub Actions + Pages 構成手順

サーバー無し・PC 無しで動くセットアップ。所要 20〜30 分、月額 **$0**。

## 構成

```
GitHub Actions (cron 21:00 UTC = 06:00 JST)
   ├─ fetch.py + render.py + Groq API
   ├─ GDrive の dismissed.json を読んで既読ID除外
   └─ dist/ を GitHub Pages に自動デプロイ
                    ▲
                    │
              [スマホ PWA]
                    │
   Dismiss → GDrive の paper-collector-dismissed.json に直接書き込み
                    ▼
             翌日の cron が反映
```

---

## 1. GitHub リポジトリを作る

1. https://github.com/new で新規リポジトリを作成
   - Name: `paper-collector-xxxxx` (xxxxx は難読 URL のため任意の乱数)
   - **Private を推奨**(公開でも動くが URL が github.io から辿れる)
2. このプロジェクトを push(初回のみ、以降不要):

   ```powershell
   cd "C:\Users\akifu\OneDrive\デスクトップ\論文収集"
   git init
   git add .
   git commit -m "initial"
   git branch -M main
   git remote add origin https://github.com/USERNAME/paper-collector-xxxxx.git
   git push -u origin main
   ```

## 2. GitHub Pages を有効化

1. リポジトリ → **Settings** → **Pages**
2. **Source: GitHub Actions** を選択(自動ワークフロー用)
3. 初回の Actions ラン後、`https://USERNAME.github.io/paper-collector-xxxxx/` で公開される

## 3. Groq API キーを取得

1. https://console.groq.com → **API Keys** → **Create API Key**
2. `gsk_...` をコピー(表示は1回のみ)

## 4. Google Drive 用 OAuth を作る

1. https://console.cloud.google.com → **プロジェクトを作成**(名前: paper-collector 等)
2. **APIとサービス** → **ライブラリ** → "Google Drive API" を有効化
3. **APIとサービス** → **OAuth 同意画面**:
   - User Type: **External**
   - アプリ名: `Paper Collector`
   - 「テストユーザー」に自分の Gmail を追加
4. **APIとサービス** → **認証情報** → **認証情報を作成** → **OAuth クライアント ID**:
   - タイプ: **Web アプリケーション**
   - 名前: `PWA`
   - **承認済みの JavaScript 生成元**: `https://USERNAME.github.io`
   - **承認済みのリダイレクト URI**: `https://developers.google.com/oauthplayground`
5. 発行された **クライアント ID** と **クライアント シークレット** を控える

## 5. Refresh Token を1回だけ取得(スマホでもOK)

1. https://developers.google.com/oauthplayground を開く
2. 右上の**歯車アイコン** → **Use your own OAuth credentials** → Client ID/Secret を入れる
3. 左側の「Step 1」で `https://www.googleapis.com/auth/drive.file` を入力 → **Authorize APIs**
4. Google 認証画面 → 自分のアカウント選択 → 許可
5. 「Step 2」で **Exchange authorization code for tokens** をクリック
6. 表示された **Refresh token** をコピー(`1//...` で始まる長い文字列)

## 6. GitHub Secrets に登録

リポジトリ → **Settings** → **Secrets and variables** → **Actions** → **New repository secret** で4つ登録:

| Name | Value |
|---|---|
| `GROQ_API_KEY` | 手順3の `gsk_...` |
| `GDRIVE_CLIENT_ID` | 手順4のクライアントID(`.apps.googleusercontent.com` で終わる) |
| `GDRIVE_CLIENT_SECRET` | 手順4のクライアントシークレット |
| `GDRIVE_REFRESH_TOKEN` | 手順5の `1//...` |

## 7. 手動で初回ビルドを実行

1. リポジトリ → **Actions** タブ
2. `Build & Deploy` ワークフロー → **Run workflow** → main → Run

2〜3分待って、Deploy が完了すると Pages URL でアクセス可能に。

## 8. スマホでインストール

1. `https://USERNAME.github.io/paper-collector-xxxxx/` を Safari(iOS) または Chrome(Android) で開く
2. 「ホーム画面に追加」でアプリ化
3. アプリを開いて右上の **"同期"** ボタン → Google 認証 → 完了

以降:
- **06:00 JST に自動で新しい10本に入れ替わる**
- **Read & Dismiss** した論文は GDrive に記録され、翌日以降は絶対に出ない
- スマホ複数台で同期(GDrive 経由)

---

## トラブルシューティング

### Actions が失敗する
- `Actions` タブ → 該当ラン → ログ確認
- Groq API key が正しいか、Groq 側で無料枠を超えていないか
- GDrive の secrets が全部埋まっているか

### 「同期」ボタンで Google 認証ができない
- Google Cloud Console → OAuth 同意画面 → テストユーザーに自分の Gmail が入っているか
- **承認済みの JavaScript 生成元** に Pages URL が入っているか(`https://USERNAME.github.io`)

### `dismissed.json` が GDrive にできない
- GDrive スコープ `drive.file` は「アプリが作った」ファイルにしか触れない
- 初回 dismiss で「同期」してあれば自動生成される
- 手動で見たい時は GDrive で `paper-collector-dismissed.json` を検索

### 論文を再表示させたい(Dismiss を取り消す)
- GDrive で `paper-collector-dismissed.json` を開く → 該当 ID の行を削除 → 保存
- 翌日以降の cron から復活

## 前構成のクリーンアップ(任意)

Fly.io 構成のファイルは不要になったので削除しても OK:

```powershell
rm Dockerfile fly.toml .dockerignore server.py DEPLOY.md
rm start.bat start_tunnel.bat setup.bat install_schedule.ps1 auto_update.py daily.py
```

(残しておいてもワークフローは動く。ローカルで Ollama デバッグしたい時のために `server.py` は残すのがおすすめ)
