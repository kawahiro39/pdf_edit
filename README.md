# PDF→JPEG ストリーミングサービス

このリポジトリには、アップロードされたPDFをページごとのJPEGに変換し、ストリーミング形式で返却するFastAPIベースのマイクロサービスが含まれています。BubbleのAPI Connectorやワークフローから直接利用できることを前提に設計されています。

## 主な特徴
- `pdf2image` と `Pillow` を利用した高品質なPDF→JPEG変換
- Word / Excel / PowerPoint / 動画（先頭フレーム）のアップロードに対応し、自動的にPDF/画像へ変換
- PlaywrightによるWebページのスクリーンショット取得（JPEG形式）
- 1ページずつのマルチパートレスポンス、ZIPアーカイブでの一括ダウンロード、Base64エンコードされたJSONレスポンスに対応
- DockerfileとCloud Runマニフェストを同梱し、Google Cloud Runへのデプロイを容易に実行可能
- `/healthz` エンドポイントでのヘルスチェックに対応

## ローカル環境での実行方法
1. 依存パッケージのインストールと開発用サーバーの起動
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   uvicorn app.main:app --host 0.0.0.0 --port 8080
   ```

2. 動作確認（マルチパート形式で受け取る例）
   ```bash
   curl -X POST \
     -F "file=@example.pdf;type=application/pdf" \
     http://localhost:8080/convert \
     -D - \
     -o response.multipart
   ```
   `response.multipart` には `multipart/mixed` のパーツが順番に保存されます。各パートはJPEG画像のバイナリで、`Content-Disposition` に `page-1.jpg` のようなファイル名が含まれます。

3. ZIP形式でダウンロードしたい場合は、`Accept` ヘッダーまたはクエリパラメーターを指定します。
   ```bash
   curl -X POST \
     -H "Accept: application/zip" \
     -F "file=@example.pdf;type=application/pdf" \
     "http://localhost:8080/convert?response_format=zip" \
     -o pages.zip
   ```
   ZIPには `page-1.jpg` のようにページごとのファイルが含まれます。

4. JSON形式で受け取りたい場合は、`Accept` ヘッダーまたは `response_format=json` を指定します。各ページはBase64文字列として含まれます。
   ```bash
   curl -X POST \
     -H "Accept: application/json" \
     -F "file=@example.pdf;type=application/pdf" \
     "http://localhost:8080/convert?response_format=json"
   ```
   レスポンスは以下のような配列です。
   ```json
   [
     {
       "page": 1,
       "filename": "page-1.jpg",
       "data": "data:image/jpeg;base64,<Base64エンコードされたJPEG>"
     }
   ]
   ```

5. Webページのスクリーンショットを取得する場合は、URLをクエリまたはJSONで指定します。
   ```bash
   curl -X POST \
     "http://localhost:8080/screenshot?url=https://example.com" \
     -o example.jpg
   ```
   JSONボディで送信する例:
   ```bash
   curl -X POST \
     -H "Content-Type: application/json" \
     -d '{"url": "https://www.wikipedia.org/"}' \
     http://localhost:8080/screenshot \
     -o wikipedia.jpg
   ```
   いずれもJPEGファイルが得られるので、保存された画像を開いて表示を確認してください。

## API 仕様
### `POST /convert`
- リクエスト形式: `multipart/form-data`
  - フィールド名: `file`（PDF / Word / Excel / PowerPoint / 動画ファイルを添付）
- レスポンス:
  - 既定: `multipart/mixed`、各パートがJPEG画像
  - `Accept: application/zip` もしくは `response_format=zip` 指定時: `application/zip`
  - `Accept: application/json` もしくは `response_format=json` 指定時: JSON配列（各要素にページ番号・ファイル名・Base64データを含む）
- エラー: 未対応の拡張子、または空ファイルを送信した場合はHTTP 400を返します。

### `GET /healthz`
- サービス稼働確認用エンドポイント。`{"status": "ok"}` を返します。

### `POST /screenshot`
- リクエスト形式: クエリパラメーター `url` もしくは JSON ボディ `{"url": "https://example.com"}`
- ビューポート: 1920×1080（Chromiumヘッドレス）
- レスポンス: `image/jpeg`（`StreamingResponse` を通じた逐次配信）。`Content-Disposition: inline; filename="screenshot.jpg"`
- 制限事項:
  - `http://` または `https://` で始まるURLのみ対応
  - ページ読み込みがタイムアウト・失敗した場合はHTTP 400を返却
  - 認証が必要なページやボット対策を行っているページでは取得に失敗する場合があります

ローカル確認例:
```bash
curl -X POST \
  "http://localhost:8080/screenshot?url=https://example.com" \
  -o example.jpg
```
JSONボディで指定したい場合:
```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.wikipedia.org/"}' \
  http://localhost:8080/screenshot \
  -o wikipedia.jpg
```
レスポンスはいずれもJPEGバイナリです。取得した画像ファイルを開いて内容を確認してください。

## Bubbleでの利用ガイド
1. **API Connector プラグインを利用**
   - `Add another API` で新しいAPIを作成し、`Use as: Action` を選択
   - `Data type` は空欄で問題ありません（レスポンスはバイナリ）
   - `POST` メソッド、URLに `https://<デプロイしたドメイン>/convert`
   - `Body type` に `multipart/form-data` を選択し、`file` フィールドを追加して `Type: File` を指定

2. **レスポンスの扱い**
   - マルチパートレスポンスはBubble側で直接分解できないため、ワークフロー内でZIP形式を指定することを推奨します。
   - `Use as` をアクションにした状態で、`Headers` に `Accept: application/zip` を追加するか、API URLに `?response_format=zip` を付与してください。
   - 実行結果はファイル（ZIP）として返るので、Bubbleワークフローの「ファイルを保存」アクションや、`Download data as file` ステップに渡せます。

3. **テスト**
   - API Connectorの`Initialize call`を行う際には、サンプルのPDFファイルをアップロードしてレスポンスの形を確認してください。
   - ZIPを受け取る場合は、レスポンスフィールドに`body`と`filename`が含まれるので、ワークフロー内で参照することでユーザーへのダウンロードリンクが作成できます。

## Cloud Run へのデプロイ手順
1. コンテナイメージのビルドと登録
   ```bash
   gcloud builds submit --tag gcr.io/PROJECT_ID/pdf-to-jpeg
   ```

2. Cloud Run へデプロイ
   ```bash
   gcloud run deploy pdf-to-jpeg \
     --image gcr.io/PROJECT_ID/pdf-to-jpeg \
     --platform managed \
     --region REGION \
     --allow-unauthenticated
   ```

3. デプロイ後の利用
   - BubbleのAPI Connectorやローカル環境から、`https://SERVICE_URL/convert` にリクエストを送信します。
   - Cloud Runではポート `8080` が公開されています。

## Cloud Run マニフェストを使ったデプロイ
`cloudrun.yaml` のテンプレートを編集し、以下のコマンドで適用できます。
```bash
gcloud run services replace cloudrun.yaml
```

## 依存関係とランタイム要件
- Python 3.11
- `fastapi`, `uvicorn[standard]`, `pdf2image`, `Pillow`
- Webページのキャプチャには `playwright` と Chromium ランタイムが必要です（Dockerfileで必要なシステムライブラリとフォントをインストールしたうえで `playwright install chromium` を実行します）。
- PDF変換には`pdftoppm`を含む `poppler-utils` が必要です（Dockerfileでインストール済み）。
- Word / Excel / PowerPoint の変換には LibreOffice (`libreoffice` または `soffice`) のコマンドライン実行環境が必要です。
- 動画から静止画を生成するために `ffmpeg` コマンドが必要です。
- Cloud Runやローカル環境で長時間稼働させる場合、十分な一時ディスク領域があることを確認してください。

Bubbleをはじめとするノーコードツールからのドキュメント処理フローにご活用ください。
