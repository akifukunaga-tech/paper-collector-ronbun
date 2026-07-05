# Cloud deployment (Fly.io + Groq)

PC の電源を切ってもスマホから使えるようにする手順です。所要時間 15〜30 分、月額目安 **$0**。

## 用意するもの

- クレジットカード (Fly.io の登録に必要。無料枠内なら課金なし)
- スマホと同じネットワークじゃなくてよいので、どこからでもアクセス可

## 1. アカウント作成

### Fly.io (サーバーホスト)
1. https://fly.io/app/sign-up でサインアップ
2. 支払い方法を登録 (無料枠内なら請求はゼロ)

### Groq (LLM API)
1. https://console.groq.com にログイン (Google アカウントでOK)
2. **API Keys** → **Create API Key** → 名前を入れて作成
3. `gsk_...` の文字列をコピー (二度と表示されないのでメモ帳へ)

## 2. flyctl インストール

Windows PowerShell を管理者モードで開いて:

```powershell
iwr https://fly.io/install.ps1 -useb | iex
```

インストール完了後、ターミナルを再起動して:

```powershell
fly auth login
```

ブラウザが開いて認証されます。

## 3. デプロイ

プロジェクトフォルダで:

```powershell
cd "C:\Users\akifu\OneDrive\デスクトップ\論文収集"

# 初回のみ: アプリ名を決めて登録 (fly.toml が書き換わる)
fly launch --copy-config --no-deploy
# → App name を聞かれる。例: paper-collector-aki (世界で一意)
# → Region: 東京 = nrt を選択
# → Postgres/Redis は "no"

# 永続ボリューム作成 (SQLite と PDF/図の保存先、3GB まで無料)
fly volumes create paper_data --region nrt --size 1

# API キー・認証情報をシークレットとして登録
fly secrets set GROQ_API_KEY=gsk_xxxxxxxxxxxx
fly secrets set AUTH_USERNAME=aki
fly secrets set AUTH_PASSWORD=$(python -c "import secrets; print(secrets.token_urlsafe(16))")
# 上記の pw を控えておく。スマホでBasic認証プロンプトが出た時に入れる

# config.yaml の provider を groq に切り替えて deploy
fly deploy
```

## 4. アクセス

デプロイが終わると `https://paper-collector-aki.fly.dev/` みたいな URL が出ます。

1. スマホで開く → Basic 認証プロンプト
2. 上で設定した user / password を入力
3. 「保存する」を選ぶと二度と聞かれない (ブラウザが記憶)
4. **ホーム画面に追加** でアイコン化

初回は論文がまだないので welcome ページが出ます。「論文を取得する」を押すと 1〜2 分で 10 本並びます。

## 4-1. アプリとしてインストール(PWA)

デプロイ済みの URL をスマホで開き、以下の手順でホーム画面にアプリ化できます。ブラウザ内タブと別に**アプリアイコン**が並び、フルスクリーンで起動します。

### iPhone (Safari)
1. Safari で `https://xxxx.fly.dev/` を開く
2. 下部の**共有ボタン**(□に↑) をタップ
3. メニューを下にスクロール → **「ホーム画面に追加」**
4. 名前を確認して**「追加」**
5. ホーム画面に "Papers" アイコンが出現 → タップで起動(URL バーなしのフルスクリーン)

> Chrome for iOS では PWA インストールに対応していないので**必ず Safari で**開いてください。

### Android (Chrome)
1. Chrome で URL を開く
2. 訪問直後にページ下部か URL バー右端に**「アプリをインストール」**のバナー/アイコンが出る
3. タップして「インストール」を選択
4. バナーが出ない場合: メニュー(⋮) → **「アプリをインストール」** or 「ホーム画面に追加」

### 動作確認
- アイコンから起動 → 上部にブラウザ URL バーが**出ない**フルスクリーンなら成功
- 初回起動時に Basic 認証プロンプト → 保存すると次回以降不要
- 圏外や機内モードでも直前に取得した論文カードは表示できる(サービスワーカーがキャッシュ)

### アイコンをカスタマイズしたい時
`make_icons.py` の色/数字/レイアウトを編集して `python make_icons.py` → `fly deploy`。スマホ側は**アプリを一旦削除して再インストール**でアイコン差し替え(ブラウザキャッシュのため)。

## 5. 日次自動更新

毎日 07:00 JST に自動でパイプラインが走ります (`DAILY_HOUR_UTC=22` in fly.toml)。

時間を変えたい場合:

```powershell
fly secrets set DAILY_HOUR_UTC=23  # 08:00 JST
# または fly.toml の [env] を書き換えて fly deploy
```

## LLM プロバイダを切り替える

`config.yaml` の `llm.provider` で切り替え可能:

| provider | model 例 | 月額目安 (10論文/日) | 必要な env |
|---|---|---|---|
| `groq` | `gemma2-9b-it` | **$0** (無料枠) | `GROQ_API_KEY` |
| `claude` | `claude-haiku-4-5-20251001` | ~$0.30 | `ANTHROPIC_API_KEY` |
| `ollama` | `gemma4:e2b` | $0 (要ローカルサーバー) | - |

Claude に切り替える場合:

```powershell
fly secrets set ANTHROPIC_API_KEY=sk-ant-xxxxxxxx
# その後、設定モーダルから config.yaml.llm.provider を "claude" に変更、
# もしくは fly ssh console でファイル編集
```

## トラブルシューティング

**論文が更新されない** → `fly logs` でエラー確認。scheduler が動いているか要確認。

**画像が表示されない** → `fly ssh console` して `ls /data/data/figures/` に画像があるか確認。

**Basic 認証がずっと聞かれる** → ブラウザで「Cookieとサイトデータ」を消してから入り直し。

**コスト超過が心配** → Fly.io ダッシュボードで請求上限を設定可能。Groq は使いすぎると 429 が返るだけで課金されない。

## ローカル運用に戻したい時

PC で Ollama を立ち上げて `config.yaml` の `provider` を `ollama` に戻せば、これまで通り localhost:8770 で使えます。クラウドとローカルを同時運用しても問題なし (DB は別々)。

## 完全に停止するとき

```powershell
fly apps destroy paper-collector-aki
fly volumes destroy <volume-id>   # volumes list で ID 確認
```
