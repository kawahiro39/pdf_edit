# PDF→JPEG ストリーミングサービス

このリポジトリには、アップロードされたPDFをページごとのJPEGに変換し、ストリーミング形式で返却するFastAPIベースのマイクロサービスが含まれています。BubbleのAPI Connectorやワークフローから直接利用できることを前提に設計されています。

## 主な特徴
- `pdf2image` と `Pillow` を利用した高品質なPDF→JPEG変換
- 1ページずつのマルチパートレスポンス、またはZIPアーカイブでの一括ダウンロードに対応
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

## API 仕様
### `POST /convert`
- リクエスト形式: `multipart/form-data`
  - フィールド名: `file`（PDFファイルを添付）
- レスポンス:
  - 既定: `multipart/mixed`、各パートがJPEG画像
  - `Accept: application/zip` もしくは `response_format=zip` 指定時: `application/zip`
- エラー: PDF以外のファイル、または空ファイルを送信した場合はHTTP 400を返します。

### `GET /healthz`
- サービス稼働確認用エンドポイント。`{"status": "ok"}` を返します。

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
- PDF変換には`pdftoppm`を含む `poppler-utils` が必要です（Dockerfileでインストール済み）。
- Cloud Runやローカル環境で長時間稼働させる場合、十分な一時ディスク領域があることを確認してください。

Bubbleをはじめとするノーコードツールからのドキュメント処理フローにご活用ください。
