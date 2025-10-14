# PDF to JPEG microservice

This repository contains a lightweight FastAPI service that converts uploaded PDFs into JPEG images and streams the result as a multipart response.

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Send a PDF via `curl` and receive a multipart response with one JPEG per page:

```bash
curl -X POST \
  -H "Content-Type: application/pdf" \
  --data-binary @example.pdf \
  http://localhost:8080/convert \
  -o output.multipart
```

Each part in `output.multipart` will be a JPEG image. You can use tooling such as [`munpack`](https://linux.die.net/man/1/munpack) or a custom script to extract the images, or update the request headers to specify `Accept: multipart/mixed` to preview in compatible clients.

## Cloud Run deployment

1. Build and push the container image:

   ```bash
   gcloud builds submit --tag gcr.io/PROJECT_ID/pdf-to-jpeg
   ```

2. Deploy to Cloud Run:

   ```bash
   gcloud run deploy pdf-to-jpeg \
     --image gcr.io/PROJECT_ID/pdf-to-jpeg \
     --platform managed \
     --region REGION \
     --allow-unauthenticated \
     --set-env-vars POPPLER_PATH=/usr/bin
   ```

   The `POPPLER_PATH` environment variable is optional when using the provided Dockerfile because `pdf2image` automatically discovers `pdftoppm`. Set it explicitly if deploying on a different base image.

3. Invoke the service:

   ```bash
   curl -X POST \
     -H "Content-Type: application/pdf" \
     --data-binary @example.pdf \
     "https://SERVICE_URL/convert" \
     -o output.multipart
   ```

## Cloud Run configuration file

Alternatively, deploy using the provided `cloudrun.yaml` manifest:

```yaml
service:
  name: pdf-to-jpeg
  image: gcr.io/PROJECT_ID/pdf-to-jpeg
  region: REGION
  env:
    - name: POPPLER_PATH
      value: /usr/bin
  ingress: all
  allow_unauthenticated: true
```

Apply it with:

```bash
gcloud run services replace cloudrun.yaml
```

## Requirements

- [poppler-utils](https://poppler.freedesktop.org/) (`pdftoppm`) must be available at runtime for `pdf2image` to work. The Dockerfile installs it for you.
- The service exposes a health endpoint at `/healthz` returning a simple status payload.
