from __future__ import annotations

import os
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Iterable

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pdf2image import convert_from_path

app = FastAPI(title="PDF to JPEG service")

BOUNDARY = "pdf-image-boundary"
CHUNK_SIZE = 1024 * 1024


def _convert_pdf_to_jpeg_paths(pdf_path: str, output_dir: str) -> list[str]:
    try:
        return convert_from_path(
            pdf_path,
            fmt="jpeg",
            output_folder=output_dir,
            output_file="page",
            paths_only=True,
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        raise HTTPException(status_code=500, detail=f"Failed to convert PDF: {exc}") from exc


def _multipart_stream(image_paths: Iterable[str]) -> Iterable[bytes]:
    for index, image_path in enumerate(image_paths, start=1):
        filename = f"page-{index}.jpg"
        content_length = os.path.getsize(image_path)
        headers = (
            f"--{BOUNDARY}\r\n"
            "Content-Type: image/jpeg\r\n"
            f"Content-Disposition: attachment; filename=\"{filename}\"\r\n"
            f"Content-Length: {content_length}\r\n\r\n"
        )
        yield headers.encode("latin-1")
        with open(image_path, "rb") as image_file:
            while True:
                chunk = image_file.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        yield b"\r\n"
    yield f"--{BOUNDARY}--\r\n".encode("latin-1")


async def _write_upload_to_tempfile(upload: UploadFile) -> str:
    with NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
        while True:
            chunk = await upload.read(CHUNK_SIZE)
            if not chunk:
                break
            tmp_file.write(chunk)
        return tmp_file.name


@app.post("/convert")
async def convert_pdf(file: UploadFile = File(...)) -> StreamingResponse:
    if file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    tmp_path = await _write_upload_to_tempfile(file)
    await file.close()

    if os.path.getsize(tmp_path) == 0:
        os.unlink(tmp_path)
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    temp_images = TemporaryDirectory()

    def cleanup() -> None:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        temp_images.cleanup()

    def content() -> Iterable[bytes]:
        try:
            image_paths = _convert_pdf_to_jpeg_paths(tmp_path, temp_images.name)
            yield from _multipart_stream(image_paths)
        finally:
            cleanup()

    media_type = f"multipart/mixed; boundary={BOUNDARY}"
    return StreamingResponse(content(), media_type=media_type)


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
